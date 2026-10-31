from __future__ import annotations

import asyncio
import codecs
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator, Dict, List, Literal, Optional, Set, Tuple, Union
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, ValidationError

from src.config_loader import AppConfig, get_provider_api_key
from src.discovery.harvester import ModelHarvester
from src.router.balancer import (
    ModelBalancer,
    ModelNotFoundError,
    ContextLengthExceededError,
    estimate_payload_tokens,
)
from src.security.approval_gate import ApprovalGate
from src.security.egress_guard import EgressGuard
from src.security.sanitizer import DLPSanitizer, StreamingRestorer, UnsupportedModalityError
from src.router.response_cache import ResponseCache
from src.security.project_quota import ProjectQuotaGuard
from src.storage import db as storage_db

logger = logging.getLogger("ai_free_gateway.openai_proxy")


class AdmissionLimitExceeded(Exception):
    def __init__(self, message: str, retry_after: int = 2):
        super().__init__(message)
        self.retry_after = retry_after


class InvalidUpstreamToolArgumentsError(Exception):
    pass


class CompletionAdmissionController:
    """
    Manages concurrent completion execution slots and pending HITL approval slots.
    Guarantees non-completion endpoints (health, stats, decision) remain unblocked.
    """
    def __init__(self, max_concurrent_completions: int = 8, max_pending_approvals: int = 32):
        self.max_completions = max_concurrent_completions
        self.max_approvals = max_pending_approvals
        self._active_completions = 0
        self._active_approvals = 0
        self._lock = asyncio.Lock()

    @property
    def active_completions(self) -> int:
        return self._active_completions

    @property
    def active_approvals(self) -> int:
        return self._active_approvals

    async def acquire_completion(self) -> None:
        async with self._lock:
            if self._active_completions >= self.max_completions:
                raise AdmissionLimitExceeded(
                    "Gateway overloaded: maximum concurrent completions reached", retry_after=2
                )
            self._active_completions += 1

    async def release_completion(self) -> None:
        async with self._lock:
            if self._active_completions > 0:
                self._active_completions -= 1

    async def enter_approval(self) -> None:
        """
        Transition request from active completion to waiting approval.
        Releases completion slot to prevent deadlock during human deliberation.
        """
        async with self._lock:
            if self._active_approvals >= self.max_approvals:
                raise AdmissionLimitExceeded(
                    "Gateway overloaded: maximum pending approvals reached", retry_after=5
                )
            self._active_approvals += 1
            if self._active_completions > 0:
                self._active_completions -= 1

    async def exit_approval_and_reacquire_completion(self) -> None:
        """
        Human approval granted: release approval slot and re-acquire completion slot.
        """
        async with self._lock:
            if self._active_approvals > 0:
                self._active_approvals -= 1
            if self._active_completions >= self.max_completions:
                raise AdmissionLimitExceeded(
                    "Gateway overloaded: unable to resume completion after approval", retry_after=2
                )
            self._active_completions += 1

    async def exit_approval_finally(self) -> None:
        """Release approval slot when rejected or expired without continuing upstream."""
        async with self._lock:
            if self._active_approvals > 0:
                self._active_approvals -= 1


def restore_json_structure(val: Any, reverse_map: Dict[str, str]) -> Any:
    """Recursively restore string values in parsed JSON structure."""
    if isinstance(val, str):
        return DLPSanitizer.restore_text(val, reverse_map)
    elif isinstance(val, dict):
        return {k: restore_json_structure(v, reverse_map) for k, v in val.items()}
    elif isinstance(val, list):
        return [restore_json_structure(item, reverse_map) for item in val]
    return val


def restore_tool_arguments_json(
    raw_args: str,
    reverse_map: Dict[str, str],
    allow_raw_string: bool = False,
) -> str:
    """
    Parse tool arguments as JSON, restore string values recursively, and re-serialize.
    Ensures quotes/backslashes/newlines in restored secrets never break JSON syntax.
    If allow_raw_string is True and raw_args is not valid JSON (e.g. fragments or mock strings),
    falls back to text restoration.
    """
    if raw_args is None:
        return "{}"
    if not isinstance(raw_args, str) or not raw_args.strip():
        return "{}"
    try:
        parsed = json.loads(raw_args)
        if reverse_map:
            restored = restore_json_structure(parsed, reverse_map)
            return json.dumps(restored, ensure_ascii=False)
        return raw_args
    except Exception as e:
        if allow_raw_string:
            return DLPSanitizer.restore_text(raw_args, reverse_map) if reverse_map else raw_args
        raise InvalidUpstreamToolArgumentsError(f"Upstream tool arguments is not valid JSON: {e}")


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text"]
    text: str


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    arguments: str


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: Literal["function"]
    function: FunctionCall


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: Optional[Union[str, List[ContentPart]]] = None
    name: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None
    reasoning_content: Optional[str] = None

    @model_validator(mode="after")
    def validate_role_contract(self) -> ChatMessage:
        if self.role == "tool":
            if not self.tool_call_id or not self.tool_call_id.strip():
                raise ValueError("Field 'tool_call_id' is required and must be non-empty for role 'tool'")
        elif self.role == "assistant":
            if self.content is None and not self.tool_calls and not self.reasoning_content:
                raise ValueError("Field 'content' cannot be null for role 'assistant' unless 'tool_calls' or 'reasoning_content' are provided")
        return self


class ToolFunctionDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    strict: Optional[bool] = None


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["function"]
    function: ToolFunctionDefinition


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(default="auto")
    messages: List[ChatMessage] = Field(min_length=1)
    stream: Optional[bool] = Field(default=False)
    tools: Optional[List[ToolDefinition]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    parallel_tool_calls: Optional[bool] = None
    max_tokens: Optional[int] = Field(default=None, gt=0)
    max_completion_tokens: Optional[int] = Field(default=None, gt=0)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    stop: Optional[Union[str, List[str]]] = None
    n: Optional[int] = Field(default=None, ge=1, le=128)
    seed: Optional[int] = None
    stream_options: Optional[Dict[str, Any]] = None
    response_format: Optional[Dict[str, Any]] = None
    reasoning_effort: Optional[str] = None

    @field_validator("stream", mode="before")
    @classmethod
    def validate_stream_strict(cls, v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        raise ValueError("Field 'stream' must be a strict boolean (true or false)")


def make_openai_error(
    message: str,
    error_type: str = "invalid_request_error",
    code: Optional[str] = None,
    param: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    err: Dict[str, Any] = {"message": message, "type": error_type}
    if param:
        err["param"] = param
    if code:
        err["code"] = code
    if correlation_id:
        err["correlation_id"] = correlation_id
    return {"error": err}


def create_openai_router(
    config: AppConfig,
    egress_guard: EgressGuard,
    approval_gate: ApprovalGate,
    balancer: ModelBalancer,
    harvester: ModelHarvester,
    response_cache: Optional[ResponseCache] = None,
    project_quota_guard: Optional[ProjectQuotaGuard] = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["OpenAI Compatible Proxy"])
    admission_controller = CompletionAdmissionController(
        max_concurrent_completions=config.server.max_concurrent_completions,
        max_pending_approvals=config.server.max_pending_approvals,
    )
    router.admission_controller = admission_controller  # type: ignore[attr-defined]
    response_cache = response_cache or ResponseCache()
    project_quota_guard = project_quota_guard or ProjectQuotaGuard()
    router.response_cache = response_cache  # type: ignore[attr-defined]
    router.project_quota_guard = project_quota_guard  # type: ignore[attr-defined]
    sanitizer = DLPSanitizer(
        entropy_threshold=config.dlp.entropy_scan.threshold,
        entropy_min_length=config.dlp.entropy_scan.min_length,
        enable_entropy=config.dlp.entropy_scan.enabled,
        custom_blocklist=config.dlp.custom_blocklist,
        token_format=config.dlp.redacted_token_format,
    )

    async def verify_gateway_auth(
        authorization: Optional[str] = Header(None),
        x_gateway_key: Optional[str] = Header(None, alias="X-Gateway-Key"),
    ) -> None:
        """Verify Gateway master key using constant-time comparison."""
        expected_key = config.server.gateway_api_key.strip()
        if not expected_key:
            return

        provided_token = ""
        if authorization and authorization.startswith("Bearer "):
            provided_token = authorization.split("Bearer ", 1)[1].strip()
        elif x_gateway_key:
            provided_token = x_gateway_key.strip()

        try:
            if not provided_token or not secrets.compare_digest(
                provided_token.encode("utf-8", errors="replace"),
                expected_key.encode("utf-8", errors="replace"),
            ):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=make_openai_error("Invalid or missing Gateway API key", "authentication_error"),
                )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=make_openai_error("Invalid or missing Gateway API key", "authentication_error"),
            )

    @router.get("/models", dependencies=[Depends(verify_gateway_auth)])
    async def list_models() -> Dict[str, Any]:
        """List active free models conforming to OpenAI API specs."""
        models = await storage_db.get_active_models(config.db_path)
        data = [
            {
                "id": "auto",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "gateway",
                "permission": [],
                "root": "auto",
                "parent": None,
                "context_length": 128000,
                "display_name": "Auto (Smart Router & Fallback)",
            }
        ]
        for m in models:
            data.append({
                "id": m["id"],
                "object": "model",
                "created": int(time.time()),
                "owned_by": m["provider"],
                "permission": [],
                "root": m["model_name"],
                "parent": None,
                "context_length": m.get("context_length", 128000),
                "display_name": m.get("display_name", m["model_name"]),
            })
        return {"object": "list", "data": data}

    @router.get("/models/{model_id:path}", dependencies=[Depends(verify_gateway_auth)])
    async def retrieve_model(model_id: str) -> Dict[str, Any]:
        """Retrieve details of a specific model."""
        if model_id == "auto":
            return {
                "id": "auto",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "gateway",
                "permission": [],
                "root": "auto",
                "parent": None,
                "context_length": 128000,
                "display_name": "Auto (Smart Router & Fallback)",
            }
        m = await storage_db.get_model_by_id(config.db_path, model_id)
        if not m or not m.get("is_active"):
            raise HTTPException(
                status_code=404,
                detail=make_openai_error(f"Model '{model_id}' not found in active pool", "model_not_found"),
            )
        return {
            "id": m["id"],
            "object": "model",
            "created": int(time.time()),
            "owned_by": m["provider"],
            "permission": [],
            "root": m["model_name"],
            "parent": None,
            "context_length": m.get("context_length", 128000),
        }

    @router.post("/chat/completions", dependencies=[Depends(verify_gateway_auth)])
    async def chat_completions(request: Request) -> Response:
        correlation_id = request.headers.get("X-Correlation-ID") or secrets.token_hex(8)
        project_id = request.headers.get("X-Project-ID", "default").strip()
        requested_priority = request.headers.get("X-Project-Priority", None)

        # A03: Project Quota & Rate Limiting check
        allowed, retry_after, effective_priority = project_quota_guard.check_quota(project_id, requested_priority)
        if not allowed:
            project_quota_guard.record_outcome(project_id, effective_priority, error=True)
            return JSONResponse(
                status_code=429,
                content=make_openai_error(
                    f"Rate limit exceeded for project '{project_id}' (priority: {effective_priority}). Please retry in {retry_after}s.",
                    "rate_limit_error",
                    code="project_rate_limit_exceeded",
                    correlation_id=correlation_id,
                ),
                headers={
                    "Retry-After": str(retry_after),
                    "X-Correlation-ID": correlation_id,
                    "X-Project-ID": project_id,
                    "X-Project-Priority": effective_priority,
                },
            )

        try:
            await admission_controller.acquire_completion()
        except AdmissionLimitExceeded as ale:
            project_quota_guard.record_outcome(project_id, effective_priority, error=True)
            return JSONResponse(
                status_code=429,
                content=make_openai_error(
                    "Gateway overloaded. Please retry later.",
                    "rate_limit_error",
                    code="gateway_overloaded",
                    correlation_id=correlation_id,
                ),
                headers={"Retry-After": str(ale.retry_after), "X-Correlation-ID": correlation_id, "X-Project-ID": project_id},
            )

        held_completion_slot = True
        held_approval_slot = False

        try:
            # 1. Parse and strictly validate request body with size gating (N01)
            cl_header = request.headers.get("content-length")
            if cl_header:
                try:
                    if int(cl_header) > config.server.max_body_bytes:
                        return JSONResponse(
                            status_code=413,
                            content=make_openai_error(
                                "Request body exceeds maximum allowed size",
                                "invalid_request_error",
                                code="payload_too_large",
                            ),
                        )
                except ValueError:
                    pass

            body_chunks: List[bytes] = []
            total_read_bytes = 0
            async for chunk in request.stream():
                total_read_bytes += len(chunk)
                if total_read_bytes > config.server.max_body_bytes:
                    return JSONResponse(
                        status_code=413,
                        content=make_openai_error(
                            "Request body exceeds maximum allowed size",
                            "invalid_request_error",
                            code="payload_too_large",
                        ),
                    )
                body_chunks.append(chunk)

            raw_bytes = b"".join(body_chunks)
            try:
                raw_body = json.loads(raw_bytes)
            except Exception:
                return JSONResponse(
                    status_code=400,
                    content=make_openai_error("Malformed JSON in request body", "invalid_request_error", code="invalid_json"),
                )

            if not isinstance(raw_body, dict):
                return JSONResponse(
                    status_code=400,
                    content=make_openai_error("Request body must be a JSON object", "invalid_request_error"),
                )

            # Explicit rejection for legacy functions to prevent bypass
            if "functions" in raw_body:
                return JSONResponse(
                    status_code=400,
                    content=make_openai_error(
                        "Legacy parameter 'functions' is unsupported; use standard 'tools' schema instead",
                        "invalid_request_error",
                        param="functions",
                        code="unsupported_parameter",
                    ),
                )

            try:
                validated = ChatCompletionRequest(**raw_body)
            except ValidationError as ve:
                errors = ve.errors()
                first_err = errors[0] if errors else {}
                loc_parts = first_err.get("loc", [])
                loc_str = ".".join(str(p) for p in loc_parts)
                msg = first_err.get("msg", "Validation error")
                # Strictly avoid echoing raw input value in error message
                clean_msg = f"Invalid request parameter at '{loc_str}': {msg}"
                return JSONResponse(
                    status_code=400,
                    content=make_openai_error(clean_msg, "invalid_request_error", param=loc_str, code="invalid_value"),
                )
            except Exception as e:
                return JSONResponse(
                    status_code=400,
                    content=make_openai_error(f"Invalid request parameters: {e}", "invalid_request_error"),
                )

            # Control fields and protocol identifiers cannot be safely pseudonymized.
            # Inspect them before any persistence; message/tool text follows the normal policy.
            if config.dlp.enabled:
                def inspect_boundary(value: Any, scan_values: bool = True) -> bool:
                    if isinstance(value, str):
                        return bool(sanitizer.sanitize_text(value).findings) if scan_values else False
                    if isinstance(value, list):
                        return any(inspect_boundary(item, scan_values) for item in value)
                    if isinstance(value, dict):
                        return any(
                            bool(sanitizer.sanitize_text(key).findings)
                            or inspect_boundary(item, scan_values or key in {"name", "id", "tool_call_id"})
                            for key, item in value.items()
                        )
                    return False

                normalized = validated.model_dump(exclude_none=True)
                if any(inspect_boundary(value, key not in {"messages", "tools"})
                       for key, value in normalized.items()):
                    return JSONResponse(
                        status_code=403,
                        content=make_openai_error(
                            "Sensitive data in control fields or protocol identifiers is not permitted.",
                            "dlp_violation", code="policy_violation",
                        ),
                    )

            client_ip = request.client.host if request.client else "127.0.0.1"
            requested_model = validated.model
            is_stream = bool(validated.stream)

            # Prepare normalized message and tool representations preserving protocol IDs (R2-03)
            messages_data: List[Dict[str, Any]] = []
            for m in validated.messages:
                msg_dict: Dict[str, Any] = {"role": m.role}
                if m.content is not None:
                    if isinstance(m.content, str):
                        msg_dict["content"] = m.content
                    else:
                        msg_dict["content"] = [p.model_dump(exclude_none=True) for p in m.content]
                else:
                    msg_dict["content"] = None

                if m.name is not None:
                    msg_dict["name"] = m.name
                if m.tool_calls is not None:
                    msg_dict["tool_calls"] = [tc.model_dump(exclude_none=True) for tc in m.tool_calls]
                if m.tool_call_id is not None:
                    msg_dict["tool_call_id"] = m.tool_call_id
                if m.reasoning_content is not None:
                    msg_dict["reasoning_content"] = m.reasoning_content

                messages_data.append(msg_dict)

            tools_data = (
                [t.model_dump(exclude_none=True) for t in validated.tools]
                if validated.tools is not None
                else None
            )

            # A02: Deterministic Response Cache check
            cache_ttl_header = request.headers.get("X-Cache-TTL")
            cache_disabled = cache_ttl_header == "0"
            temp = validated.temperature if validated.temperature is not None else 1.0
            is_cache_eligible = (not is_stream) and (not cache_disabled) and (temp <= 0.3 or request.headers.get("X-Cache", "").lower() == "true")
            cache_key = None
            cache_ttl = float(cache_ttl_header) if (cache_ttl_header and cache_ttl_header.isdigit() and int(cache_ttl_header) > 0) else None

            if is_cache_eligible:
                resp_fmt = validated.response_format.model_dump(exclude_none=True) if validated.response_format else None
                cache_key = response_cache.compute_cache_key(
                    requested_model,
                    messages_data,
                    tools_data,
                    resp_fmt,
                )
                cached = response_cache.get(cache_key)
                if cached:
                    project_quota_guard.record_outcome(project_id, effective_priority, cache_hit=True)
                    if held_completion_slot:
                        await admission_controller.release_completion()
                        held_completion_slot = False
                    return JSONResponse(
                        content=cached["content"],
                        headers={
                            "X-Correlation-ID": correlation_id,
                            "X-Gateway-Cache": "HIT",
                            "X-Cache-TTL-Remaining": str(cached["remaining_ttl"]),
                            "X-Project-ID": project_id,
                        },
                    )

            # 2. DLP Scan & Dual Projections: Outbound vs Audit (R2-02, R2-03)
            risk_actions = {
                "HIGH": config.dlp.risk_action.high,
                "MEDIUM": config.dlp.risk_action.medium,
                "LOW": config.dlp.risk_action.low,
            }

            try:
                if config.dlp.enabled:
                    res = sanitizer.sanitize_messages(
                        messages=messages_data,
                        tools=tools_data,
                        risk_actions=risk_actions,
                    )
                    outbound_messages = res.outbound_messages
                    audit_messages = res.audit_messages
                    outbound_tools = res.outbound_tools
                    audit_tools = res.audit_tools
                    findings = res.all_findings
                    reverse_map = res.reverse_map
                    max_risk = res.overall_max_risk
                    audit_prompt_snippet = res.audit_prompt_snippet
                else:
                    outbound_messages = messages_data
                    audit_messages = messages_data
                    outbound_tools = tools_data
                    audit_tools = tools_data
                    findings = []
                    reverse_map = {}
                    max_risk = "NONE"
                    audit_prompt_snippet = ""
                    for msg in messages_data:
                        c = msg.get("content")
                        if isinstance(c, str) and c:
                            audit_prompt_snippet = c[:150]
                            break
            except UnsupportedModalityError as ume:
                return JSONResponse(
                    status_code=400,
                    content=make_openai_error(str(ume), "invalid_request_error"),
                )

            # Audit findings representation (never contains raw secret values)
            findings_dict_list = [
                {
                    "rule_name": f.rule_name,
                    "entity_type": f.entity_type,
                    "risk_level": f.risk_level,
                    "redacted_token": f.redacted_token,
                    "masked_value": f.masked_value,
                }
                for f in findings
            ]

            # 3. Policy Coordination & Human-in-the-Loop Approval Gate (R2-08)
            action_taken: Optional[str] = None

            if config.dlp.enabled and findings:
                # Step A: Check for any finding that requires 'reject' -> block immediately
                has_reject = False
                reject_risk = "NONE"
                for f in findings:
                    if risk_actions.get(f.risk_level) == "reject":
                        has_reject = True
                        if f.risk_level == "HIGH" or reject_risk == "NONE":
                            reject_risk = f.risk_level

                if has_reject:
                    block_act = f"BLOCKED_{reject_risk}_RISK"
                    await storage_db.insert_dlp_log(config.db_path, {
                        "client_ip": client_ip,
                        "direction": "request",
                        "risk_level": max_risk,
                        "findings": findings_dict_list,
                        "redaction_count": len(findings),
                        "action_taken": block_act,
                        "prompt_snippet": audit_prompt_snippet,
                    })
                    return JSONResponse(
                        status_code=403,
                        content=make_openai_error(
                            "Request blocked by Zero-Trust DLP policy: sensitive data violation.",
                            "dlp_violation",
                            code="policy_violation",
                        ),
                    )

                # Step B: Check for any finding that requires 'approve' -> exactly one approval request
                findings_needing_approval = [
                    f for f in findings if risk_actions.get(f.risk_level) == "approve"
                ]
                if findings_needing_approval:
                    # Release completion slot and acquire approval slot to prevent deadlocks (N01)
                    held_completion_slot = False
                    held_approval_slot = True
                    try:
                        await admission_controller.enter_approval()
                    except AdmissionLimitExceeded as ale:
                        held_approval_slot = False
                        return JSONResponse(
                            status_code=429,
                            content=make_openai_error(
                                "Gateway overloaded. Too many pending approvals.",
                                "rate_limit_error",
                                code="gateway_overloaded",
                            ),
                            headers={"Retry-After": str(ale.retry_after)},
                        )

                    approval_risk = "HIGH" if any(f.risk_level == "HIGH" for f in findings_needing_approval) else "MEDIUM"
                    app_id = await approval_gate.create_request(
                        risk_level=approval_risk,
                        client_ip=client_ip,
                        model_requested=requested_model,
                        findings=findings_dict_list,
                        sanitized_messages=audit_messages,
                        sanitized_payload={"model": requested_model, "messages": audit_messages},
                        timeout_seconds=config.dlp.approval_timeout_seconds,
                    )
                    async def _watch_client_disconnect() -> bool:
                        while True:
                            try:
                                if await request.is_disconnected():
                                    return True
                            except Exception:
                                return False
                            await asyncio.sleep(0.5)

                    wait_task = asyncio.create_task(
                        approval_gate.wait_for_decision(app_id, config.dlp.approval_timeout_seconds)
                    )
                    disconnect_task = asyncio.create_task(_watch_client_disconnect())

                    try:
                        done, pending = await asyncio.wait(
                            [wait_task, disconnect_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for t in pending:
                            t.cancel()
                            try:
                                await t
                            except (asyncio.CancelledError, Exception):
                                pass

                        if disconnect_task in done and disconnect_task.result() is True:
                            # Client disconnected while waiting: cancel request and clean memory (N03)
                            await approval_gate.cancel_request(app_id)
                            held_approval_slot = False
                            await admission_controller.exit_approval_finally()
                            return Response(status_code=499)

                        decision, _ = wait_task.result()
                    except Exception:
                        wait_task.cancel()
                        disconnect_task.cancel()
                        held_approval_slot = False
                        await admission_controller.exit_approval_finally()
                        raise

                    if decision == "approved":
                        # If client disconnected immediately after approval, halt before upstream (N03)
                        if await request.is_disconnected():
                            logger.info("Client disconnected after approval; halting before upstream.")
                            held_approval_slot = False
                            await admission_controller.exit_approval_finally()
                            return Response(status_code=499)

                        action_taken = "APPROVED_BY_ADMIN"
                        held_approval_slot = False
                        held_completion_slot = True
                        try:
                            await admission_controller.exit_approval_and_reacquire_completion()
                        except AdmissionLimitExceeded as ale:
                            held_completion_slot = False
                            return JSONResponse(
                                status_code=429,
                                content=make_openai_error(
                                    "Gateway overloaded. Please retry later.",
                                    "rate_limit_error",
                                    code="gateway_overloaded",
                                ),
                                headers={"Retry-After": str(ale.retry_after)},
                            )
                    elif decision == "expired" and config.dlp.approval_timeout_action == "redact":
                        action_taken = "TIMEOUT_AUTO_REDACTED"
                        held_approval_slot = False
                        held_completion_slot = True
                        try:
                            await admission_controller.exit_approval_and_reacquire_completion()
                        except AdmissionLimitExceeded as ale:
                            held_completion_slot = False
                            return JSONResponse(
                                status_code=429,
                                content=make_openai_error(
                                    "Gateway overloaded. Please retry later.",
                                    "rate_limit_error",
                                    code="gateway_overloaded",
                                ),
                                headers={"Retry-After": str(ale.retry_after)},
                            )
                        # Forward completely redacted version
                        outbound_messages = audit_messages
                        outbound_tools = audit_tools
                    else:
                        held_approval_slot = False
                        await admission_controller.exit_approval_finally()
                        act_decision = "REJECTED_BY_ADMIN" if decision == "rejected" else "EXPIRED"
                        await storage_db.insert_dlp_log(config.db_path, {
                            "client_ip": client_ip,
                            "direction": "request",
                            "risk_level": max_risk,
                            "findings": findings_dict_list,
                            "redaction_count": len(findings),
                            "action_taken": act_decision,
                            "prompt_snippet": audit_prompt_snippet,
                        })
                        return JSONResponse(
                            status_code=403,
                            content=make_openai_error(
                                f"Request {decision} by Human-in-the-Loop policy gate.",
                                "approval_rejected",
                                code="approval_rejected",
                            ),
                        )

            # Determine audit action label if not set by approval gate
            if action_taken is None:
                if not findings:
                    action_taken = "ALLOWED"
                elif any(risk_actions.get(f.risk_level) == "redact" for f in findings):
                    action_taken = "REDACTED"
                else:
                    action_taken = "ALLOWED"

            # Record audit log (strictly using audit projection with redacted snippet)
            redact_count = len([f for f in findings if risk_actions.get(f.risk_level) == "redact"])
            await storage_db.insert_dlp_log(config.db_path, {
                "client_ip": client_ip,
                "direction": "request",
                "risk_level": max_risk,
                "findings": findings_dict_list,
                "redaction_count": redact_count,
                "action_taken": action_taken,
                "prompt_snippet": audit_prompt_snippet,
            })

            # 4. Construct outbound payload from validated schema and sanitized output (R2-01)
            outgoing_payload: Dict[str, Any] = {
                "model": requested_model,
                "messages": outbound_messages,
            }
            if validated.stream is not None:
                outgoing_payload["stream"] = validated.stream
            if outbound_tools is not None:
                outgoing_payload["tools"] = outbound_tools
            if validated.tool_choice is not None:
                outgoing_payload["tool_choice"] = validated.tool_choice
            if validated.parallel_tool_calls is not None:
                outgoing_payload["parallel_tool_calls"] = validated.parallel_tool_calls
            if validated.max_tokens is not None:
                outgoing_payload["max_tokens"] = validated.max_tokens
            if validated.max_completion_tokens is not None:
                outgoing_payload["max_completion_tokens"] = validated.max_completion_tokens
            if validated.temperature is not None:
                outgoing_payload["temperature"] = validated.temperature
            if validated.top_p is not None:
                outgoing_payload["top_p"] = validated.top_p
            if validated.stop is not None:
                outgoing_payload["stop"] = validated.stop
            if validated.n is not None:
                outgoing_payload["n"] = validated.n
            if validated.seed is not None:
                outgoing_payload["seed"] = validated.seed
            if validated.stream_options is not None:
                outgoing_payload["stream_options"] = validated.stream_options
            if validated.response_format is not None:
                outgoing_payload["response_format"] = validated.response_format
            if validated.reasoning_effort is not None and validated.reasoning_effort.lower() in ("low", "medium", "high"):
                outgoing_payload["reasoning_effort"] = validated.reasoning_effort.lower()

            # 5. Routing context budget & candidate chain lookup (R2-09)
            estimated_tokens = estimate_payload_tokens(outgoing_payload)
            has_tools = bool(validated.tools or validated.tool_choice)

            try:
                candidates = await balancer.get_candidate_chain(
                    requested_model=requested_model,
                    prompt_text=audit_prompt_snippet,
                    estimated_tokens=estimated_tokens,
                    has_tools=has_tools,
                )
            except ModelNotFoundError as mne:
                return JSONResponse(
                    status_code=404,
                    content=make_openai_error(str(mne), "model_not_found", code="model_not_found"),
                )
            except ContextLengthExceededError as clee:
                return JSONResponse(
                    status_code=400,
                    content=make_openai_error(str(clee), "invalid_request_error", code="context_length_exceeded"),
                )

            if not candidates:
                return JSONResponse(
                    status_code=503,
                    content=make_openai_error(
                        "No available free models found in pool (all providers unconfigured, disabled, or in cooldown).",
                        "service_unavailable",
                    ),
                )

            last_error = None
            failed_attempts: List[Dict[str, Any]] = []
            cooldown_providers_in_request: Set[str] = set()
            failed_auth_providers: Set[str] = set()

            start_candidates_time = time.perf_counter()
            total_completion_deadline = config.server.total_completion_timeout_seconds
            max_attempts = getattr(config.server, "max_attempts", 3)
            attempt_count = 0
            deadline_exceeded = False

            # 6. Fallback routing loop with instant 429/401/403 skipping, attempt budget & total deadline (N01, N06)
            for candidate in candidates:
                elapsed_so_far = time.perf_counter() - start_candidates_time
                remaining_time = total_completion_deadline - elapsed_so_far
                if remaining_time <= 0:
                    deadline_exceeded = True
                    logger.warning("Total completion deadline exceeded before candidate execution.")
                    break

                if attempt_count >= max_attempts:
                    logger.warning("Maximum fallback attempts reached (%d). Halting candidate loop.", max_attempts)
                    break

                provider = candidate["provider"]

                if provider in cooldown_providers_in_request or provider in failed_auth_providers:
                    continue

                if await storage_db.is_provider_in_cooldown(config.db_path, provider):
                    cooldown_providers_in_request.add(provider)
                    continue

                # Upfront credential check (N06):
                # Candidate must have credential. Missing key must not send request without auth.
                pcfg = getattr(config.providers, provider, None)
                if pcfg and pcfg.api_key_env and not get_provider_api_key(provider, config):
                    logger.debug("Provider %s requires API key %s but none is set. Skipping upfront.", provider, pcfg.api_key_env)
                    failed_attempts.append({
                        "id": candidate["id"],
                        "provider": provider,
                        "error": f"Provider {provider} API key ({pcfg.api_key_env}) is not configured",
                    })
                    continue

                target_url, headers, provider_payload = balancer.resolve_provider_request(
                    candidate, outgoing_payload
                )

                attempt_count += 1

                try:
                    # --- CASE A: STREAMING (Server-Sent Events) (R2-06, N01, N06) ---
                    if is_stream:
                        stream_gen = _handle_streaming_proxy(
                            egress_guard=egress_guard,
                            target_url=target_url,
                            headers=headers,
                            payload=provider_payload,
                            candidate=candidate,
                            reverse_map=reverse_map if config.dlp.pseudonymize_reversible else {},
                            config=config,
                            overall_deadline_timestamp=start_candidates_time + total_completion_deadline,
                        )

                        # Probe first item to verify provider readiness before emitting HTTP 200, bounded by deadline
                        try:
                            first_chunk = await asyncio.wait_for(stream_gen.__anext__(), timeout=remaining_time)
                        except StopAsyncIteration:
                            first_chunk = None
                        except asyncio.TimeoutError:
                            deadline_exceeded = True
                            logger.warning("Stream probe for candidate %s timed out against total completion deadline.", candidate["id"])
                            await storage_db.update_model_metrics(config.db_path, candidate["id"], total_completion_deadline * 1000.0, False)
                            last_error = f"Total completion deadline ({total_completion_deadline}s) exceeded"
                            failed_attempts.append({"id": candidate["id"], "provider": provider, "error": last_error})
                            break
                        except httpx.HTTPStatusError as e:
                            logger.error("Stream provider %s returned HTTP %s", provider, e.response.status_code)
                            await storage_db.update_model_metrics(
                                config.db_path, candidate["id"], 500.0, False
                            )
                            if e.response.status_code == 429:
                                cooldown_providers_in_request.add(provider)
                                cooldown_sec = _extract_cooldown_seconds(e.response, config, provider)
                                await storage_db.set_provider_cooldown(
                                    config.db_path, provider, cooldown_sec, 429, "Rate limit exceeded"
                                )
                                last_error = f"Stream provider {provider} returned HTTP 429"
                                failed_attempts.append({"id": candidate["id"], "provider": provider, "status_code": 429, "error": last_error})
                                continue
                            elif e.response.status_code in (401, 403):
                                failed_auth_providers.add(provider)
                                await storage_db.set_provider_cooldown(
                                    config.db_path, provider, 300, e.response.status_code, "Authentication or permission failed"
                                )
                                last_error = f"Stream provider {provider} authentication failed (HTTP {e.response.status_code})"
                                failed_attempts.append({"id": candidate["id"], "provider": provider, "status_code": e.response.status_code, "error": last_error})
                                continue
                            elif e.response.status_code in (400, 422):
                                last_error = f"Stream provider {provider} rejected request parameters (HTTP {e.response.status_code})"
                                failed_attempts.append({"id": candidate["id"], "provider": provider, "status_code": e.response.status_code, "error": last_error})
                                # N06: 400/422 parameter/schema error: halt immediately without blind retries
                                break
                            else:
                                last_error = f"Stream provider {provider} returned HTTP {e.response.status_code}"
                                failed_attempts.append({"id": candidate["id"], "provider": provider, "status_code": e.response.status_code, "error": last_error})
                                continue
                        except Exception as e:
                            logger.error("Stream provider %s connection failed (%s)", provider, type(e).__name__)
                            await storage_db.update_model_metrics(
                                config.db_path, candidate["id"], 500.0, False
                            )
                            last_error = f"Stream provider {provider} connection failed: {e.__class__.__name__}"
                            failed_attempts.append({"id": candidate["id"], "provider": provider, "error": last_error})
                            continue

                        if (time.perf_counter() - start_candidates_time) > total_completion_deadline:
                            deadline_exceeded = True
                            logger.warning("Stream probe for candidate %s completed after total completion deadline expired.", candidate["id"])
                            await storage_db.update_model_metrics(config.db_path, candidate["id"], 500.0, False)
                            last_error = f"Total completion deadline ({total_completion_deadline}s) exceeded"
                            failed_attempts.append({"id": candidate["id"], "provider": provider, "error": last_error})
                            break

                        if first_chunk is None:
                            logger.warning("Stream candidate %s yielded empty stream before first chunk.", candidate["id"])
                            await storage_db.update_model_metrics(config.db_path, candidate["id"], 500.0, False)
                            last_error = "Empty upstream stream"
                            failed_attempts.append({"id": candidate["id"], "provider": provider, "error": last_error})
                            continue

                        # Stream succeeded initial probe: commit active model & return streaming response
                        balancer.record_active_model(candidate, candidates, failed_attempts)

                        async def stream_chained_wrapper(initial_item: Optional[bytes], rest_gen: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
                            try:
                                if initial_item is not None:
                                    yield initial_item
                                async for item in rest_gen:
                                    yield item
                            finally:
                                try:
                                    await rest_gen.aclose()
                                finally:
                                    await admission_controller.release_completion()

                        # Ownership of completion slot is transferred to the streaming response generator (N01)
                        held_completion_slot = False

                        return StreamingResponse(
                            stream_chained_wrapper(first_chunk, stream_gen),
                            media_type="text/event-stream",
                            headers={
                                "Cache-Control": "no-cache",
                                "Connection": "keep-alive",
                                "X-Accel-Buffering": "no",
                                "X-Correlation-ID": correlation_id,
                            },
                        )

                    # --- CASE B: STANDARD JSON RESPONSE (N01, N06) ---
                    try:
                        resp = await asyncio.wait_for(
                            egress_guard.post(target_url, headers=headers, json_data=provider_payload),
                            timeout=remaining_time,
                        )
                    except asyncio.TimeoutError:
                        deadline_exceeded = True
                        logger.warning("Upstream call for candidate %s timed out against total completion deadline.", candidate["id"])
                        await storage_db.update_model_metrics(config.db_path, candidate["id"], total_completion_deadline * 1000.0, False)
                        last_error = f"Total completion deadline ({total_completion_deadline}s) exceeded"
                        failed_attempts.append({"id": candidate["id"], "provider": provider, "error": last_error})
                        break

                    if (time.perf_counter() - start_candidates_time) > total_completion_deadline:
                        deadline_exceeded = True
                        logger.warning("Candidate %s completed after total completion deadline expired.", candidate["id"])
                        await storage_db.update_model_metrics(config.db_path, candidate["id"], getattr(resp, "latency_ms", 500.0), False)
                        last_error = f"Total completion deadline ({total_completion_deadline}s) exceeded"
                        failed_attempts.append({"id": candidate["id"], "provider": provider, "error": last_error})
                        break

                    if resp.status_code == 429:
                        cooldown_providers_in_request.add(provider)
                        cooldown_sec = _extract_cooldown_seconds(resp, config, provider)
                        await storage_db.set_provider_cooldown(
                            config.db_path, provider, cooldown_sec, 429, "Rate limit exceeded"
                        )
                        await storage_db.update_model_metrics(
                            config.db_path, candidate["id"], getattr(resp, "latency_ms", 500.0), False
                        )
                        last_error = f"Provider {provider} returned HTTP 429"
                        failed_attempts.append({"id": candidate["id"], "provider": provider, "status_code": 429, "error": last_error})
                        continue

                    if resp.status_code in (401, 403):
                        failed_auth_providers.add(provider)
                        await storage_db.set_provider_cooldown(
                            config.db_path, provider, 300, resp.status_code, "Authentication or permission failed"
                        )
                        await storage_db.update_model_metrics(
                            config.db_path, candidate["id"], getattr(resp, "latency_ms", 500.0), False
                        )
                        last_error = f"Provider {provider} authentication failed (HTTP {resp.status_code})"
                        failed_attempts.append({"id": candidate["id"], "provider": provider, "status_code": resp.status_code, "error": last_error})
                        continue

                    if resp.status_code in (400, 422):
                        await storage_db.update_model_metrics(
                            config.db_path, candidate["id"], getattr(resp, "latency_ms", 500.0), False
                        )
                        last_error = f"Provider {provider} rejected request parameters (HTTP {resp.status_code})"
                        failed_attempts.append({"id": candidate["id"], "provider": provider, "status_code": resp.status_code, "error": last_error})
                        # N06: 400/422 parameter/schema error: halt immediately without blind retries
                        break

                    if resp.status_code >= 400:
                        await storage_db.update_model_metrics(
                            config.db_path, candidate["id"], getattr(resp, "latency_ms", 500.0), False
                        )
                        # Strictly sanitize upstream error: do not leak raw response text (R2-07)
                        last_error = f"Provider {provider} returned HTTP {resp.status_code}"
                        failed_attempts.append({"id": candidate["id"], "provider": provider, "status_code": resp.status_code, "error": last_error})
                        continue

                    # Validate response JSON body and ChatCompletion structure before success
                    try:
                        resp_json = resp.json()
                    except Exception as e:
                        logger.error("Upstream candidate %s returned invalid non-JSON body: %s", candidate["id"], e)
                        await storage_db.update_model_metrics(config.db_path, candidate["id"], getattr(resp, "latency_ms", 500.0), False)
                        last_error = "Upstream returned non-JSON body"
                        failed_attempts.append({"id": candidate["id"], "provider": provider, "error": last_error})
                        continue

                    choices = resp_json.get("choices") if isinstance(resp_json, dict) else None
                    if not isinstance(choices, list) or len(choices) == 0:
                        logger.error("Upstream candidate %s returned invalid completion structure (missing or empty choices)", candidate["id"])
                        await storage_db.update_model_metrics(config.db_path, candidate["id"], getattr(resp, "latency_ms", 500.0), False)
                        last_error = "Upstream returned invalid completion structure (missing or empty choices)"
                        failed_attempts.append({"id": candidate["id"], "provider": provider, "error": last_error})
                        continue

                    valid_choices = True
                    for choice in choices:
                        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                            valid_choices = False
                            break

                    if not valid_choices:
                        logger.error("Upstream candidate %s returned malformed choice in completion", candidate["id"])
                        await storage_db.update_model_metrics(config.db_path, candidate["id"], getattr(resp, "latency_ms", 500.0), False)
                        last_error = "Upstream returned malformed choice in completion"
                        failed_attempts.append({"id": candidate["id"], "provider": provider, "error": last_error})
                        continue

                    # Validate tool arguments strictly as JSON and restore DLP pseudonymization (Bug 3 / N02)
                    for choice in choices:
                        msg = choice.get("message", {})
                        if isinstance(msg.get("content"), str) and config.dlp.pseudonymize_reversible and reverse_map:
                            msg["content"] = DLPSanitizer.restore_text(msg["content"], reverse_map)
                        if isinstance(msg.get("reasoning_content"), str) and config.dlp.pseudonymize_reversible and reverse_map:
                            msg["reasoning_content"] = DLPSanitizer.restore_text(msg["reasoning_content"], reverse_map)
                        if "tool_calls" in msg and isinstance(msg["tool_calls"], list):
                            for tc in msg["tool_calls"]:
                                if isinstance(tc, dict):
                                    fn = tc.get("function", {})
                                    raw_args = fn.get("arguments")
                                    if isinstance(raw_args, str):
                                        fn["arguments"] = restore_tool_arguments_json(
                                            raw_args,
                                            reverse_map if (config.dlp.pseudonymize_reversible and reverse_map) else {},
                                            allow_raw_string=False,
                                        )

                    latency_ms = getattr(resp, "latency_ms", 200.0)
                    # Extract token usage for quota tracking
                    usage = resp_json.get("usage", {})
                    tokens_used = usage.get("total_tokens", 0) if isinstance(usage, dict) else 0
                    await storage_db.increment_provider_usage(config.db_path, provider, tokens_used)
                    await storage_db.update_model_metrics(config.db_path, candidate["id"], latency_ms, True, tokens_used=tokens_used)
                    balancer.record_active_model(candidate, candidates, failed_attempts, tokens_used=tokens_used)

                    if is_cache_eligible and cache_key:
                        response_cache.set(cache_key, resp_json, ttl_seconds=cache_ttl)

                    project_quota_guard.record_outcome(project_id, effective_priority, tokens_estimated=tokens_used)

                    return JSONResponse(
                        content=resp_json,
                        headers={
                            "X-Correlation-ID": correlation_id,
                            "X-Gateway-Cache": "MISS",
                            "X-Project-ID": project_id,
                        },
                    )

                except InvalidUpstreamToolArgumentsError as itae:
                    logger.error("Upstream candidate %s returned invalid tool arguments JSON: %s", candidate["id"], itae)
                    await storage_db.update_model_metrics(config.db_path, candidate["id"], 500.0, False)
                    last_error = "Invalid upstream tool arguments JSON"
                    failed_attempts.append({"id": candidate["id"], "provider": provider, "error": last_error})
                    continue
                except Exception as e:
                    logger.warning(f"Error calling candidate {candidate['id']}: {e.__class__.__name__}")
                    last_error = f"Candidate error: {e.__class__.__name__}"
                    failed_attempts.append({"id": candidate["id"], "provider": provider, "error": last_error})
                    continue

            # Check if total completion deadline was exceeded
            if deadline_exceeded or (time.perf_counter() - start_candidates_time) >= total_completion_deadline:
                project_quota_guard.record_outcome(project_id, effective_priority, error=True)
                return JSONResponse(
                    status_code=504,
                    content=make_openai_error(
                        f"Gateway timeout: total completion deadline of {total_completion_deadline}s exceeded",
                        "timeout_error",
                        code="gateway_timeout",
                        correlation_id=correlation_id,
                    ),
                    headers={"X-Correlation-ID": correlation_id, "X-Project-ID": project_id},
                )

            # All candidates failed (R2-07: Sanitized error response without leaking upstream payload)
            project_quota_guard.record_outcome(project_id, effective_priority, error=True)
            return JSONResponse(
                status_code=502,
                content=make_openai_error(
                    "All free model fallbacks failed. Upstream providers unavailable.",
                    "bad_gateway",
                    code="bad_gateway",
                    correlation_id=correlation_id,
                ),
                headers={"X-Correlation-ID": correlation_id, "X-Project-ID": project_id},
            )
        finally:
            if held_approval_slot:
                await admission_controller.exit_approval_finally()
            elif held_completion_slot:
                await admission_controller.release_completion()

    return router


def _extract_cooldown_seconds(resp: httpx.Response, config: AppConfig, provider: str) -> int:
    """Parse Retry-After header (seconds or HTTP date), bounded between 1 and 3600 seconds (N06)."""
    cooldown_sec = 300
    p_cfg = getattr(config.providers, provider, None)
    if p_cfg:
        cooldown_sec = p_cfg.cooldown_seconds_on_429

    retry_after = resp.headers.get("retry-after")
    if retry_after:
        raw_val = retry_after.strip()
        try:
            val = int(raw_val)
            return max(1, min(3600, val))
        except ValueError:
            try:
                dt = parsedate_to_datetime(raw_val)
                now = datetime.now(timezone.utc)
                diff = int((dt - now).total_seconds())
                return max(1, min(3600, diff))
            except Exception:
                pass
    return cooldown_sec


async def _handle_streaming_proxy(
    egress_guard: EgressGuard,
    target_url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    candidate: Dict[str, Any],
    reverse_map: Dict[str, str],
    config: AppConfig,
    overall_deadline_timestamp: Optional[float] = None,
) -> AsyncIterator[bytes]:
    """
    Handles streaming responses with incremental UTF-8 decoding, per-choice and per-channel
    StreamingRestorer, per (choice_idx, tool_idx) arguments buffering, multi-line SSE grouping,
    strict terminal completion lifecycle, line/event/tool bounds, and configurable deadlines (N01, N02).
    """
    provider = candidate["provider"]
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    # Independent restorers per (choice_idx, channel, tool_idx)
    restorers: Dict[Tuple[int, str, int], StreamingRestorer] = {}

    def get_restorer(choice_idx: int, channel: str, tool_idx: int = 0) -> StreamingRestorer:
        key = (choice_idx, channel, tool_idx)
        if key not in restorers:
            restorers[key] = StreamingRestorer(reverse_map, config.dlp.redacted_token_format)
        return restorers[key]

    line_buffer = ""
    current_data_lines: List[str] = []
    tokens_estimated = 0
    actual_tokens_reported = None
    has_seen_done = False
    has_yielded = False
    has_received_valid_completion = False
    stream_failed = False
    finished_choices: Set[int] = set()

    # Per (choice_idx, tool_idx) tool calls state buffer (preserving id, name, type, and arguments)
    tool_calls_state: Dict[Tuple[int, int], Dict[str, Any]] = {}

    def flush_all_remaining() -> List[Dict[str, Any]]:
        chunks_to_emit: List[Dict[str, Any]] = []
        for (c_idx, chan, _), r in list(restorers.items()):
            flushed = r.flush()
            if flushed:
                if chan == "content":
                    chunks_to_emit.append({
                        "id": "chatcmpl-stream",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": candidate["model_name"],
                        "choices": [{"index": c_idx, "delta": {"content": flushed}, "finish_reason": None}],
                    })
                elif chan == "reasoning_content":
                    chunks_to_emit.append({
                        "id": "chatcmpl-stream",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": candidate["model_name"],
                        "choices": [{"index": c_idx, "delta": {"reasoning_content": flushed}, "finish_reason": None}],
                    })

        if tool_calls_state:
            by_choice: Dict[int, List[Tuple[int, Dict[str, Any]]]] = {}
            for (c_idx, t_idx), state in sorted(tool_calls_state.items()):
                by_choice.setdefault(c_idx, []).append((t_idx, state))
            tool_calls_state.clear()

            for c_idx, tools in by_choice.items():
                tool_deltas = []
                for t_idx, state in tools:
                    raw_args = state.get("arguments", "")
                    if not raw_args and state.get("name"):
                        raw_args = "{}"
                    restored_args = restore_tool_arguments_json(raw_args, reverse_map, allow_raw_string=False)
                    tc_entry: Dict[str, Any] = {
                        "index": t_idx,
                        "type": state.get("type") or "function",
                        "function": {"arguments": restored_args},
                    }
                    if state.get("id"):
                        tc_entry["id"] = state["id"]
                    if state.get("name"):
                        tc_entry["function"]["name"] = state["name"]
                    tool_deltas.append(tc_entry)
                chunks_to_emit.append({
                    "id": "chatcmpl-stream",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": candidate["model_name"],
                    "choices": [{
                        "index": c_idx,
                        "delta": {"tool_calls": tool_deltas},
                        "finish_reason": None,
                    }],
                })
        return chunks_to_emit

    start_time = time.perf_counter()
    first_event_deadline = start_time + config.server.first_event_timeout_seconds
    max_duration_deadline = start_time + config.server.max_stream_duration_seconds
    if overall_deadline_timestamp is not None:
        max_duration_deadline = min(max_duration_deadline, overall_deadline_timestamp)
        first_event_deadline = min(first_event_deadline, overall_deadline_timestamp)

    upstream = egress_guard.stream_post(target_url, headers=headers, json_data=payload)
    upstream_iter = upstream.__aiter__()

    try:
        while True:
            now = time.perf_counter()
            if now >= max_duration_deadline:
                logger.error("Stream total completion deadline or max duration exceeded")
                stream_failed = True
                await storage_db.update_model_metrics(config.db_path, candidate["id"], 500.0, False)
                if not has_yielded:
                    raise TimeoutError("Stream total completion deadline exceeded before first event")
                err_payload = make_openai_error("Total completion deadline exceeded", "stream_timeout")
                yield f"data: {json.dumps(err_payload)}\n\ndata: [DONE]\n\n".encode("utf-8")
                return

            if not has_received_valid_completion:
                remaining_first = first_event_deadline - now
                if remaining_first <= 0:
                    stream_failed = True
                    await storage_db.update_model_metrics(config.db_path, candidate["id"], 500.0, False)
                    raise TimeoutError(f"First event timeout exceeded ({config.server.first_event_timeout_seconds}s)")
                chunk_timeout = min(remaining_first, max_duration_deadline - now)
            else:
                chunk_timeout = min(config.server.idle_timeout_seconds, max_duration_deadline - now)

            if chunk_timeout <= 0:
                logger.error("Stream total completion deadline exceeded waiting for next chunk")
                stream_failed = True
                await storage_db.update_model_metrics(config.db_path, candidate["id"], 500.0, False)
                if not has_yielded:
                    raise TimeoutError("Stream total completion deadline exceeded")
                err_payload = make_openai_error("Total completion deadline exceeded", "stream_timeout")
                yield f"data: {json.dumps(err_payload)}\n\ndata: [DONE]\n\n".encode("utf-8")
                return

            try:
                chunk_bytes = await asyncio.wait_for(upstream_iter.__anext__(), timeout=chunk_timeout)
            except (asyncio.TimeoutError, TimeoutError):
                stream_failed = True
                await storage_db.update_model_metrics(config.db_path, candidate["id"], 500.0, False)
                if not has_received_valid_completion:
                    raise TimeoutError("First event timeout waiting for valid completion chunk")
                else:
                    logger.error("Stream total completion deadline exceeded during stream transmission")
                    err_payload = make_openai_error("Total completion deadline exceeded", "stream_timeout")
                    yield f"data: {json.dumps(err_payload)}\n\ndata: [DONE]\n\n".encode("utf-8")
                    return
            except StopAsyncIteration:
                break

            chunk_str = decoder.decode(chunk_bytes, final=False)
            line_buffer += chunk_str

            # Check line buffer ceiling (N01)
            if len(line_buffer.encode("utf-8")) > config.server.max_sse_line_bytes:
                logger.error("SSE line exceeded max_sse_line_bytes limit")
                stream_failed = True
                await storage_db.update_model_metrics(config.db_path, candidate["id"], 500.0, False)
                err_payload = make_openai_error("SSE line exceeded maximum allowed buffer size", "stream_error")
                yield f"data: {json.dumps(err_payload)}\n\ndata: [DONE]\n\n".encode("utf-8")
                return

            # Process lines split by newline
            while "\n" in line_buffer:
                line, line_buffer = line_buffer.split("\n", 1)
                line = line.rstrip("\r")

                # Blank line indicates end of an SSE event
                if not line:
                    if current_data_lines:
                        # Check event size ceiling (N01)
                        event_size = sum(len(l.encode("utf-8")) for l in current_data_lines)
                        if event_size > config.server.max_sse_event_bytes:
                            logger.error("SSE event exceeded max_sse_event_bytes limit")
                            stream_failed = True
                            await storage_db.update_model_metrics(config.db_path, candidate["id"], 500.0, False)
                            err_payload = make_openai_error("SSE event exceeded maximum allowed buffer size", "stream_error")
                            yield f"data: {json.dumps(err_payload)}\n\ndata: [DONE]\n\n".encode("utf-8")
                            return

                        raw_data = "\n".join(current_data_lines)
                        current_data_lines = []

                        if raw_data.strip() == "[DONE]":
                            has_seen_done = True
                            try:
                                chunks_to_emit = flush_all_remaining()
                            except Exception as e:
                                stream_failed = True
                                logger.error("Invalid upstream tool arguments JSON on flush: %s", e)
                                await storage_db.update_model_metrics(config.db_path, candidate["id"], 500.0, False)
                                err_payload = make_openai_error("Invalid upstream tool arguments JSON", "stream_error")
                                yield f"data: {json.dumps(err_payload)}\n\ndata: [DONE]\n\n".encode("utf-8")
                                return
                            for chk in chunks_to_emit:
                                has_yielded = True
                                yield f"data: {json.dumps(chk)}\n\n".encode("utf-8")
                            has_yielded = True
                            yield b"data: [DONE]\n\n"
                            break

                        try:
                            chunk_obj = json.loads(raw_data)
                        except Exception:
                            # Malformed JSON event in stream: terminate failed immediately without recording success (N02)
                            logger.error("Malformed JSON received in stream event")
                            stream_failed = True
                            has_yielded = True
                            err_payload = make_openai_error("Malformed JSON received in stream event", "stream_error")
                            yield f"data: {json.dumps(err_payload)}\n\ndata: [DONE]\n\n".encode("utf-8")
                            return

                        if not isinstance(chunk_obj, dict):
                            stream_failed = True
                            has_yielded = True
                            err_payload = make_openai_error("Invalid SSE event structure", "stream_error")
                            yield f"data: {json.dumps(err_payload)}\n\ndata: [DONE]\n\n".encode("utf-8")
                            return

                        if "usage" in chunk_obj and isinstance(chunk_obj["usage"], dict):
                            actual_tokens_reported = chunk_obj["usage"].get("total_tokens")

                        choices = chunk_obj.get("choices", [])
                        for choice in choices:
                            c_idx = choice.get("index", 0)
                            if c_idx in finished_choices:
                                choice.get("delta", {}).clear()
                                continue

                            delta = choice.get("delta", {})
                            finish_reason = choice.get("finish_reason")

                            # Restore content channel
                            content = delta.get("content")
                            if isinstance(content, str):
                                has_received_valid_completion = True
                                tokens_estimated += 1
                                r_content = get_restorer(c_idx, "content", 0)
                                delta["content"] = r_content.feed(content)

                            # Restore reasoning_content channel
                            reasoning = delta.get("reasoning_content")
                            if isinstance(reasoning, str):
                                has_received_valid_completion = True
                                r_reasoning = get_restorer(c_idx, "reasoning_content", 0)
                                delta["reasoning_content"] = r_reasoning.feed(reasoning)

                            # Buffer tool_calls arguments channel per (choice_idx, tool_idx) (N02, U01)
                            if "tool_calls" in delta and isinstance(delta["tool_calls"], list):
                                has_received_valid_completion = True
                                for tc in delta["tool_calls"]:
                                    t_idx = tc.get("index")
                                    if type(t_idx) is not int or t_idx < 0:
                                        t_idx = 0
                                    key = (c_idx, t_idx)
                                    state = tool_calls_state.setdefault(key, {
                                        "id": None,
                                        "type": "function",
                                        "name": None,
                                        "arguments": "",
                                    })
                                    if tc.get("id"):
                                        state["id"] = tc["id"]
                                    if tc.get("type"):
                                        state["type"] = tc["type"]
                                    fn = tc.get("function", {})
                                    if fn.get("name"):
                                        state["name"] = fn["name"]
                                    args = fn.get("arguments")
                                    if isinstance(args, str):
                                        state["arguments"] += args
                                        # Tool buffer ceiling (N01)
                                        total_tool_bytes = sum(len(v.get("arguments", "").encode("utf-8")) for v in tool_calls_state.values())
                                        if total_tool_bytes > config.server.max_tool_buffer_bytes:
                                            stream_failed = True
                                            err_payload = make_openai_error("Tool arguments buffer exceeded maximum allowed size", "stream_error")
                                            yield f"data: {json.dumps(err_payload)}\n\ndata: [DONE]\n\n".encode("utf-8")
                                            return
                                        fn["arguments"] = ""

                            # If finish_reason encountered, flush remaining buffer for this choice BEFORE finish_reason (N02, U01)
                            if finish_reason:
                                r_content = restorers.get((c_idx, "content", 0))
                                if r_content:
                                    flushed = r_content.flush()
                                    if flushed:
                                        delta["content"] = (delta.get("content") or "") + flushed

                                r_reasoning = restorers.get((c_idx, "reasoning_content", 0))
                                if r_reasoning:
                                    flushed_reas = r_reasoning.flush()
                                    if flushed_reas:
                                        delta["reasoning_content"] = (delta.get("reasoning_content") or "") + flushed_reas

                                # Flush tool arguments for this choice
                                tools_for_choice = [k for k in list(tool_calls_state.keys()) if k[0] == c_idx]
                                if tools_for_choice:
                                    tool_deltas = []
                                    for (_, t_idx) in sorted(tools_for_choice, key=lambda x: x[1]):
                                        state = tool_calls_state.pop((c_idx, t_idx))
                                        raw_args = state.get("arguments", "")
                                        if not raw_args and state.get("name"):
                                            raw_args = "{}"
                                        try:
                                            restored_args = restore_tool_arguments_json(raw_args, reverse_map, allow_raw_string=False)
                                        except Exception as e:
                                            stream_failed = True
                                            logger.error(f"Invalid upstream tool arguments JSON: {e}")
                                            await storage_db.update_model_metrics(config.db_path, candidate["id"], 500.0, False)
                                            err_payload = make_openai_error("Invalid upstream tool arguments JSON", "stream_error")
                                            yield f"data: {json.dumps(err_payload)}\n\ndata: [DONE]\n\n".encode("utf-8")
                                            return

                                        tc_entry: Dict[str, Any] = {
                                            "index": t_idx,
                                            "type": state.get("type") or "function",
                                            "function": {"arguments": restored_args},
                                        }
                                        if state.get("id"):
                                            tc_entry["id"] = state["id"]
                                        if state.get("name"):
                                            tc_entry["function"]["name"] = state["name"]
                                        tool_deltas.append(tc_entry)

                                    # Emit flushed tool arguments chunk before finish_reason
                                    flush_chunk = {
                                        "id": chunk_obj.get("id", "chatcmpl-stream"),
                                        "object": "chat.completion.chunk",
                                        "created": chunk_obj.get("created", int(time.time())),
                                        "model": chunk_obj.get("model", candidate["model_name"]),
                                        "choices": [{
                                            "index": c_idx,
                                            "delta": {"tool_calls": tool_deltas},
                                            "finish_reason": None,
                                        }],
                                    }
                                    has_yielded = True
                                    yield f"data: {json.dumps(flush_chunk)}\n\n".encode("utf-8")

                                finished_choices.add(c_idx)
                                delta.pop("tool_calls", None)

                        has_yielded = True
                        yield f"data: {json.dumps(chunk_obj)}\n\n".encode("utf-8")
                    continue

                # Collect data lines
                if line.startswith("data:"):
                    data_val = line[5:].lstrip()
                    current_data_lines.append(data_val)
                # Ignore comments (: ping) or other SSE fields

            if has_seen_done:
                break

        # Final decode flush
        trailing = decoder.decode(b"", final=True)
        if trailing:
            line_buffer += trailing
        if line_buffer.startswith("data:"):
            data_val = line_buffer[5:].lstrip()
            current_data_lines.append(data_val)

        if current_data_lines and not has_seen_done:
            raw_data = "\n".join(current_data_lines)
            if raw_data.strip() == "[DONE]":
                has_seen_done = True
                try:
                    chunks_to_emit = flush_all_remaining()
                except Exception as e:
                    stream_failed = True
                    logger.error("Invalid upstream tool arguments JSON on flush: %s", e)
                    await storage_db.update_model_metrics(config.db_path, candidate["id"], 500.0, False)
                    err_payload = make_openai_error("Invalid upstream tool arguments JSON", "stream_error")
                    yield f"data: {json.dumps(err_payload)}\n\ndata: [DONE]\n\n".encode("utf-8")
                    return
                for chk in chunks_to_emit:
                    has_yielded = True
                    yield f"data: {json.dumps(chk)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"

        # Check stream completeness contract (R2-06)
        if not has_seen_done:
            logger.error(f"Stream from provider {provider} terminated prematurely without [DONE]")
            stream_failed = True
            await storage_db.update_model_metrics(config.db_path, candidate["id"], 500.0, False)
            try:
                chunks_to_emit = flush_all_remaining()
            except Exception as e:
                chunks_to_emit = []
            for chk in chunks_to_emit:
                has_yielded = True
                yield f"data: {json.dumps(chk)}\n\n".encode("utf-8")
            has_yielded = True
            err_incomplete = make_openai_error("Stream connection closed prematurely by upstream", "upstream_incomplete")
            yield f"data: {json.dumps(err_incomplete)}\n\ndata: [DONE]\n\n".encode("utf-8")
            return

        # Record token usage for quota tracking strictly upon clean completion (N02)
        if has_seen_done and not stream_failed:
            final_tokens = actual_tokens_reported if actual_tokens_reported is not None else max(1, tokens_estimated)
            await storage_db.increment_provider_usage(config.db_path, provider, final_tokens)
            await storage_db.update_model_metrics(
                config.db_path, candidate["id"], (time.perf_counter() - start_time) * 1000.0,
                True, tokens_used=final_tokens,
            )

    except (GeneratorExit, asyncio.CancelledError):
        raise
    except Exception as e:
        if not has_yielded:
            raise
        logger.error(f"Stream error on candidate {candidate['id']}: {e.__class__.__name__}")
        err_msg = json.dumps(make_openai_error("Stream transmission error occurred", "stream_error"))
        yield f"data: {err_msg}\n\ndata: [DONE]\n\n".encode("utf-8")

    finally:
        await upstream.aclose()
