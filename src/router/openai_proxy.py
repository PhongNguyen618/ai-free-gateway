from __future__ import annotations

import asyncio
import codecs
import json
import logging
import secrets
import time
from typing import Any, AsyncIterator, Dict, List, Literal, Optional, Set, Tuple, Union
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, ValidationError

from src.config_loader import AppConfig
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
from src.storage import db as storage_db

logger = logging.getLogger("ai_free_gateway.openai_proxy")


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
) -> Dict[str, Any]:
    err: Dict[str, Any] = {"message": message, "type": error_type}
    if param:
        err["param"] = param
    if code:
        err["code"] = code
    return {"error": err}


def create_openai_router(
    config: AppConfig,
    egress_guard: EgressGuard,
    approval_gate: ApprovalGate,
    balancer: ModelBalancer,
    harvester: ModelHarvester,
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["OpenAI Compatible Proxy"])
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

        if not provided_token or not secrets.compare_digest(provided_token, expected_key):
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
        # 1. Parse and strictly validate request body (R2-01, R2-04)
        try:
            raw_body = await request.json()
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
                decision, _ = await approval_gate.wait_for_decision(app_id, config.dlp.approval_timeout_seconds)

                if decision == "approved":
                    action_taken = "APPROVED_BY_ADMIN"
                elif decision == "expired" and config.dlp.approval_timeout_action == "redact":
                    action_taken = "TIMEOUT_AUTO_REDACTED"
                    # Forward completely redacted version
                    outbound_messages = audit_messages
                    outbound_tools = audit_tools
                else:
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

        try:
            candidates = await balancer.get_candidate_chain(
                requested_model=requested_model,
                prompt_text=audit_prompt_snippet,
                estimated_tokens=estimated_tokens,
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

        # 6. Fallback routing loop with instant 429 provider skipping
        for candidate in candidates:
            provider = candidate["provider"]

            if provider in cooldown_providers_in_request:
                continue

            if await storage_db.is_provider_in_cooldown(config.db_path, provider):
                cooldown_providers_in_request.add(provider)
                continue

            target_url, headers, provider_payload = balancer.resolve_provider_request(
                candidate, outgoing_payload
            )

            try:
                # --- CASE A: STREAMING (Server-Sent Events) (R2-06) ---
                if is_stream:
                    stream_gen = _handle_streaming_proxy(
                        egress_guard=egress_guard,
                        target_url=target_url,
                        headers=headers,
                        payload=provider_payload,
                        candidate=candidate,
                        reverse_map=reverse_map if config.dlp.pseudonymize_reversible else {},
                        config=config,
                    )

                    # Probe first item to verify provider readiness before emitting HTTP 200
                    try:
                        first_chunk = await stream_gen.__anext__()
                    except StopAsyncIteration:
                        first_chunk = None
                    except httpx.HTTPStatusError as e:
                        try:
                            err_body = e.response.text if hasattr(e.response, "text") else str(e)
                            Path("storage").mkdir(exist_ok=True)
                            with open("storage/last_upstream_error.log", "a", encoding="utf-8") as ef:
                                ef.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] HTTPStatusError {candidate['id']} ({provider}) HTTP {e.response.status_code}:\n{err_body}\n")
                        except Exception:
                            pass
                        logger.error(f"Stream provider {provider} ({candidate['id']}) returned HTTP {e.response.status_code}: {e}")
                        if e.response.status_code == 429:
                            cooldown_providers_in_request.add(provider)
                            cooldown_sec = _extract_cooldown_seconds(e.response, config, provider)
                            await storage_db.set_provider_cooldown(
                                config.db_path, provider, cooldown_sec, 429, "Rate limit exceeded"
                            )
                        await storage_db.update_model_metrics(
                            config.db_path, candidate["id"], 500.0, False
                        )
                        last_error = f"Stream provider {provider} returned HTTP {e.response.status_code}"
                        failed_attempts.append({"id": candidate["id"], "provider": provider, "status_code": e.response.status_code, "error": last_error})
                        continue
                    except Exception as e:
                        try:
                            Path("storage").mkdir(exist_ok=True)
                            with open("storage/last_upstream_error.log", "a", encoding="utf-8") as ef:
                                ef.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Exception {candidate['id']} ({provider}) {type(e).__name__}: {e}\n")
                        except Exception:
                            pass
                        logger.error(f"Stream provider {provider} ({candidate['id']}) connection failed: {type(e).__name__}: {e}")
                        await storage_db.update_model_metrics(
                            config.db_path, candidate["id"], 500.0, False
                        )
                        last_error = f"Stream provider {provider} connection failed: {e.__class__.__name__}"
                        failed_attempts.append({"id": candidate["id"], "provider": provider, "error": last_error})
                        continue

                    # Stream succeeded initial probe: commit active model & return streaming response
                    await storage_db.update_model_metrics(config.db_path, candidate["id"], 150.0, True)
                    balancer.record_active_model(candidate, candidates, failed_attempts)

                    async def stream_chained_wrapper(initial_item: Optional[bytes], rest_gen: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
                        try:
                            if initial_item is not None:
                                yield initial_item
                            async for item in rest_gen:
                                yield item
                        except (GeneratorExit, asyncio.CancelledError):
                            pass

                    return StreamingResponse(
                        stream_chained_wrapper(first_chunk, stream_gen),
                        media_type="text/event-stream",
                        headers={
                            "Cache-Control": "no-cache",
                            "Connection": "keep-alive",
                            "X-Accel-Buffering": "no",
                        },
                    )

                # --- CASE B: STANDARD JSON RESPONSE ---
                resp = await egress_guard.post(target_url, headers=headers, json_data=provider_payload)

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

                if resp.status_code >= 400:
                    try:
                        err_body = resp.text if hasattr(resp, "text") else ""
                        Path("storage").mkdir(exist_ok=True)
                        with open("storage/last_upstream_error.log", "a", encoding="utf-8") as ef:
                            ef.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] JSON HTTP {resp.status_code} {candidate['id']} ({provider}):\n{err_body}\n")
                    except Exception:
                        pass
                    await storage_db.update_model_metrics(
                        config.db_path, candidate["id"], getattr(resp, "latency_ms", 500.0), False
                    )
                    # Strictly sanitize upstream error: do not leak raw response text (R2-07)
                    last_error = f"Provider {provider} returned HTTP {resp.status_code}"
                    failed_attempts.append({"id": candidate["id"], "provider": provider, "status_code": resp.status_code, "error": last_error})
                    continue

                # Successful JSON execution
                resp_json = resp.json()
                latency_ms = getattr(resp, "latency_ms", 200.0)
                await storage_db.update_model_metrics(config.db_path, candidate["id"], latency_ms, True)
                # Extract token usage for quota tracking
                usage = resp_json.get("usage", {})
                tokens_used = usage.get("total_tokens", 0)
                await storage_db.increment_provider_usage(config.db_path, provider, tokens_used)
                await storage_db.update_model_metrics(config.db_path, candidate["id"], latency_ms, True, tokens_used=tokens_used)
                balancer.record_active_model(candidate, candidates, failed_attempts, tokens_used=tokens_used)

                # Reversible pseudonymization restoration in response (content + tool_calls arguments) (R2-03)
                if config.dlp.pseudonymize_reversible and reverse_map:
                    choices = resp_json.get("choices", [])
                    for choice in choices:
                        msg = choice.get("message", {})
                        if isinstance(msg.get("content"), str):
                            msg["content"] = DLPSanitizer.restore_text(msg["content"], reverse_map)
                        if isinstance(msg.get("reasoning_content"), str):
                            msg["reasoning_content"] = DLPSanitizer.restore_text(msg["reasoning_content"], reverse_map)
                        if "tool_calls" in msg and isinstance(msg["tool_calls"], list):
                            for tc in msg["tool_calls"]:
                                fn = tc.get("function", {})
                                if "arguments" in fn and isinstance(fn["arguments"], str):
                                    fn["arguments"] = DLPSanitizer.restore_text(fn["arguments"], reverse_map)

                return JSONResponse(content=resp_json)

            except Exception as e:
                logger.warning(f"Error calling candidate {candidate['id']}: {e.__class__.__name__}")
                last_error = f"Candidate error: {e.__class__.__name__}"
                failed_attempts.append({"id": candidate["id"], "provider": provider, "error": last_error})
                continue

        # All candidates failed (R2-07: Sanitized error response without leaking upstream payload)
        return JSONResponse(
            status_code=502,
            content=make_openai_error(
                "All free model fallbacks failed. Upstream providers unavailable.",
                "bad_gateway",
                code="bad_gateway",
            ),
        )

    return router


def _extract_cooldown_seconds(resp: httpx.Response, config: AppConfig, provider: str) -> int:
    """Parse Retry-After header if available, bounded between 1 and 3600 seconds."""
    cooldown_sec = 300
    p_cfg = getattr(config.providers, provider, None)
    if p_cfg:
        cooldown_sec = p_cfg.cooldown_seconds_on_429

    retry_after = resp.headers.get("retry-after")
    if retry_after:
        try:
            val = int(retry_after.strip())
            return max(1, min(3600, val))
        except ValueError:
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
) -> AsyncIterator[bytes]:
    """
    Handles streaming responses with incremental UTF-8 decoding, per-choice and per-channel
    StreamingRestorer, multi-line SSE grouping, and strict completion lifecycle verification.
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
    start_time = time.perf_counter()

    try:
        async for chunk_bytes in egress_guard.stream_post(target_url, headers=headers, json_data=payload):
            chunk_str = decoder.decode(chunk_bytes, final=False)
            line_buffer += chunk_str

            # Process lines split by newline
            while "\n" in line_buffer:
                line, line_buffer = line_buffer.split("\n", 1)
                line = line.rstrip("\r")

                # Blank line indicates end of an SSE event
                if not line:
                    if current_data_lines:
                        raw_data = "\n".join(current_data_lines)
                        current_data_lines = []

                        if raw_data.strip() == "[DONE]":
                            has_seen_done = True
                            # Flush all remaining buffers across all channels
                            for (c_idx, chan, t_idx), r in restorers.items():
                                flushed = r.flush()
                                if flushed:
                                    if chan == "content":
                                        flush_chunk = {
                                            "id": "chatcmpl-stream",
                                            "object": "chat.completion.chunk",
                                            "created": int(time.time()),
                                            "model": candidate["model_name"],
                                            "choices": [{"index": c_idx, "delta": {"content": flushed}, "finish_reason": None}],
                                        }
                                        has_yielded = True
                                        yield f"data: {json.dumps(flush_chunk)}\n\n".encode("utf-8")
                                    elif chan == "tool_arguments":
                                        flush_chunk = {
                                            "id": "chatcmpl-stream",
                                            "object": "chat.completion.chunk",
                                            "created": int(time.time()),
                                            "model": candidate["model_name"],
                                            "choices": [{
                                                "index": c_idx,
                                                "delta": {
                                                    "tool_calls": [{
                                                        "index": t_idx,
                                                        "function": {"arguments": flushed},
                                                    }]
                                                },
                                                "finish_reason": None,
                                            }],
                                        }
                                        has_yielded = True
                                        yield f"data: {json.dumps(flush_chunk)}\n\n".encode("utf-8")
                            has_yielded = True
                            yield b"data: [DONE]\n\n"
                        else:
                            try:
                                chunk_obj = json.loads(raw_data)
                                if "usage" in chunk_obj and isinstance(chunk_obj["usage"], dict):
                                    actual_tokens_reported = chunk_obj["usage"].get("total_tokens")

                                choices = chunk_obj.get("choices", [])
                                for choice in choices:
                                    c_idx = choice.get("index", 0)
                                    delta = choice.get("delta", {})
                                    content = delta.get("content")
                                    finish_reason = choice.get("finish_reason")

                                    # Restore content channel
                                    if isinstance(content, str):
                                        tokens_estimated += 1
                                        r_content = get_restorer(c_idx, "content", 0)
                                        delta["content"] = r_content.feed(content)

                                    # Restore reasoning_content channel
                                    reasoning = delta.get("reasoning_content")
                                    if isinstance(reasoning, str):
                                        r_reasoning = get_restorer(c_idx, "reasoning_content", 0)
                                        delta["reasoning_content"] = r_reasoning.feed(reasoning)

                                    # Restore tool_calls arguments channel
                                    if "tool_calls" in delta and isinstance(delta["tool_calls"], list):
                                        for t_idx, tc in enumerate(delta["tool_calls"]):
                                            fn = tc.get("function", {})
                                            args = fn.get("arguments")
                                            if isinstance(args, str):
                                                r_arg = get_restorer(c_idx, "tool_arguments", t_idx)
                                                fn["arguments"] = r_arg.feed(args)

                                    # If finish_reason encountered, flush remaining buffer for this choice
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

                                has_yielded = True
                                yield f"data: {json.dumps(chunk_obj)}\n\n".encode("utf-8")
                            except json.JSONDecodeError:
                                # Malformed JSON event in stream: emit controlled error event
                                has_yielded = True
                                err_payload = make_openai_error("Malformed JSON received in stream event", "stream_error")
                                yield f"data: {json.dumps(err_payload)}\n\n".encode("utf-8")
                    continue

                # Collect data lines
                if line.startswith("data:"):
                    data_val = line[5:].lstrip()
                    current_data_lines.append(data_val)
                # Ignore comments (: ping) or other SSE fields

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
                yield b"data: [DONE]\n\n"

        # Check stream completeness contract (R2-06)
        if not has_seen_done:
            logger.error(f"Stream from provider {provider} terminated prematurely without [DONE]")
            has_yielded = True
            err_incomplete = make_openai_error("Stream connection closed prematurely by upstream", "upstream_incomplete")
            yield f"data: {json.dumps(err_incomplete)}\n\ndata: [DONE]\n\n".encode("utf-8")
            return

        # Record token usage for quota tracking
        final_tokens = actual_tokens_reported if actual_tokens_reported is not None else max(1, tokens_estimated)
        await storage_db.increment_provider_usage(config.db_path, provider, final_tokens)
        await storage_db.update_model_metrics(config.db_path, candidate["id"], 150.0, True, tokens_used=final_tokens)

    except (GeneratorExit, asyncio.CancelledError):
        # Clean client disconnection
        pass
    except Exception as e:
        if not has_yielded:
            raise
        logger.error(f"Stream error on candidate {candidate['id']}: {e.__class__.__name__}")
        err_msg = json.dumps(make_openai_error("Stream transmission error occurred", "stream_error"))
        yield f"data: {err_msg}\n\ndata: [DONE]\n\n".encode("utf-8")
