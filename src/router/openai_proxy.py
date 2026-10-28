from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from src.config_loader import AppConfig
from src.discovery.harvester import ModelHarvester
from src.router.balancer import ModelBalancer
from src.security.approval_gate import ApprovalGate
from src.security.egress_guard import EgressGuard
from src.security.sanitizer import DLPSanitizer, StreamingRestorer
from src.storage import db as storage_db

logger = logging.getLogger("ai_free_gateway.openai_proxy")


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
    )

    async def verify_gateway_auth(authorization: Optional[str] = Header(None)) -> None:
        """Verify Gateway master key if configured."""
        if not config.server.gateway_api_key:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid Authorization header",
            )
        token = authorization.split("Bearer ", 1)[1].strip()
        if token != config.server.gateway_api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid gateway API key",
            )

    @router.get("/models", dependencies=[Depends(verify_gateway_auth)])
    async def list_models() -> Dict[str, Any]:
        """List available free models conforming to OpenAI API specs."""
        models = await storage_db.get_active_models(config.db_path)
        data = []
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
        m = await storage_db.get_model_by_id(config.db_path, model_id)
        if not m:
            raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found in pool")
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
        """
        OpenAI-compatible Chat Completions proxy with Zero-Trust DLP,
        HITL approval gate, and automatic free-tier fallback routing.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Malformed JSON in request body")

        client_ip = request.client.host if request.client else "127.0.0.1"
        requested_model = body.get("model", "auto")
        messages = body.get("messages", [])
        is_stream = bool(body.get("stream", False))

        if not messages:
            raise HTTPException(status_code=400, detail="Field 'messages' is required and cannot be empty")

        # 1. DLP Scan & Reversible Pseudonymization
        sanitized_messages, findings, reverse_map, max_risk = sanitizer.sanitize_messages(messages)
        prompt_snippet = ""
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                prompt_snippet = content[:150]
                break

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

        action_taken = "ALLOWED"
        if findings:
            action_taken = "REDACTED"

        # 2. Risk Action & Human-in-the-Loop Gate
        if max_risk == "HIGH":
            risk_decision = config.dlp.risk_action.high
            if risk_decision == "reject":
                await storage_db.insert_dlp_log(config.db_path, {
                    "client_ip": client_ip,
                    "direction": "request",
                    "risk_level": max_risk,
                    "findings": findings_dict_list,
                    "redaction_count": len(findings),
                    "action_taken": "BLOCKED_HIGH_RISK",
                    "prompt_snippet": prompt_snippet,
                })
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": {
                            "message": "Request blocked by Zero-Trust DLP: High-risk secrets (API keys/private credentials) detected.",
                            "type": "dlp_violation",
                            "findings_count": len(findings),
                        }
                    },
                )
            elif risk_decision == "approve":
                app_id = await approval_gate.create_request(
                    risk_level="HIGH",
                    client_ip=client_ip,
                    model_requested=requested_model,
                    findings=findings_dict_list,
                    sanitized_messages=sanitized_messages,
                    mapping=reverse_map,
                    raw_payload=body,
                    timeout_seconds=config.dlp.approval_timeout_seconds,
                )
                decision, _ = await approval_gate.wait_for_decision(app_id, config.dlp.approval_timeout_seconds)
                if decision != "approved":
                    action_taken = "REJECTED_BY_ADMIN" if decision == "rejected" else "EXPIRED"
                    await storage_db.insert_dlp_log(config.db_path, {
                        "client_ip": client_ip,
                        "direction": "request",
                        "risk_level": max_risk,
                        "findings": findings_dict_list,
                        "redaction_count": len(findings),
                        "action_taken": action_taken,
                        "prompt_snippet": prompt_snippet,
                    })
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "error": {
                                "message": f"Request {decision} by admin approval gate.",
                                "type": "approval_rejected",
                            }
                        },
                    )

        elif max_risk == "MEDIUM":
            risk_decision = config.dlp.risk_action.medium
            if risk_decision == "approve":
                app_id = await approval_gate.create_request(
                    risk_level="MEDIUM",
                    client_ip=client_ip,
                    model_requested=requested_model,
                    findings=findings_dict_list,
                    sanitized_messages=sanitized_messages,
                    mapping=reverse_map,
                    raw_payload=body,
                    timeout_seconds=config.dlp.approval_timeout_seconds,
                )
                decision, _ = await approval_gate.wait_for_decision(app_id, config.dlp.approval_timeout_seconds)
                if decision != "approved":
                    if decision == "expired" and config.dlp.approval_timeout_action == "redact":
                        action_taken = "TIMEOUT_AUTO_REDACTED"
                    else:
                        action_taken = "REJECTED_BY_ADMIN" if decision == "rejected" else "EXPIRED"
                        await storage_db.insert_dlp_log(config.db_path, {
                            "client_ip": client_ip,
                            "direction": "request",
                            "risk_level": max_risk,
                            "findings": findings_dict_list,
                            "redaction_count": len(findings),
                            "action_taken": action_taken,
                            "prompt_snippet": prompt_snippet,
                        })
                        raise HTTPException(
                            status_code=403,
                            detail={"error": {"message": f"Request {decision} by Human-in-the-Loop policy."}},
                        )

        # 3. Log DLP event
        await storage_db.insert_dlp_log(config.db_path, {
            "client_ip": client_ip,
            "direction": "request",
            "risk_level": max_risk,
            "findings": findings_dict_list,
            "redaction_count": len(findings),
            "action_taken": action_taken,
            "prompt_snippet": prompt_snippet,
        })

        # 4. Prepare sanitized payload
        outgoing_payload = dict(body)
        outgoing_payload["messages"] = sanitized_messages

        # 5. Build candidate fallback chain
        prompt_text = " ".join(
            str(m.get("content", "")) for m in messages if isinstance(m.get("content"), str)
        )
        candidates = await balancer.get_candidate_chain(
            requested_model=requested_model,
            prompt_text=prompt_text,
            estimated_tokens=len(prompt_text) // 4,
        )

        if not candidates:
            raise HTTPException(
                status_code=503,
                detail="No available free models found (all providers in cooldown or unconfigured).",
            )

        last_error = None

        # 6. Fallback routing loop
        for candidate in candidates:
            provider = candidate["provider"]
            target_url, headers, provider_payload = balancer.resolve_provider_request(
                candidate, outgoing_payload
            )

            try:
                # --- CASE A: STREAMING (Server-Sent Events) ---
                if is_stream:
                    stream_generator = _handle_streaming_proxy(
                        egress_guard=egress_guard,
                        target_url=target_url,
                        headers=headers,
                        payload=provider_payload,
                        candidate=candidate,
                        reverse_map=reverse_map if config.dlp.pseudonymize_reversible else {},
                        config=config,
                    )
                    return StreamingResponse(stream_generator, media_type="text/event-stream")

                # --- CASE B: STANDARD JSON RESPONSE ---
                resp = await egress_guard.post(target_url, headers=headers, json_data=provider_payload)

                if resp.status_code == 429:
                    cooldown_sec = 300
                    p_cfg = getattr(config.providers, provider, None)
                    if p_cfg:
                        cooldown_sec = p_cfg.cooldown_seconds_on_429
                    await storage_db.set_provider_cooldown(
                        config.db_path, provider, cooldown_sec, 429, "Rate limit exceeded"
                    )
                    await storage_db.update_model_metrics(
                        config.db_path, candidate["id"], getattr(resp, "latency_ms", 500.0), False
                    )
                    last_error = f"Provider {provider} returned 429 Rate Limit"
                    continue

                if resp.status_code >= 400:
                    await storage_db.update_model_metrics(
                        config.db_path, candidate["id"], getattr(resp, "latency_ms", 500.0), False
                    )
                    last_error = f"Provider {provider} returned HTTP {resp.status_code}: {resp.text}"
                    continue

                # Successful execution
                resp_json = resp.json()
                latency_ms = getattr(resp, "latency_ms", 200.0)
                await storage_db.update_model_metrics(config.db_path, candidate["id"], latency_ms, True)

                # Extract token usage for quota tracking
                usage = resp_json.get("usage", {})
                tokens_used = usage.get("total_tokens", 0)
                await storage_db.increment_provider_usage(config.db_path, provider, tokens_used)

                # Reversible pseudonymization restoration in response
                if config.dlp.pseudonymize_reversible and reverse_map:
                    choices = resp_json.get("choices", [])
                    for choice in choices:
                        msg = choice.get("message", {})
                        content = msg.get("content")
                        if isinstance(content, str):
                            msg["content"] = DLPSanitizer.restore_text(content, reverse_map)

                return JSONResponse(content=resp_json)

            except Exception as e:
                logger.warning(f"Error calling candidate {candidate['id']}: {e}")
                last_error = str(e)
                continue

        # All candidates failed
        raise HTTPException(
            status_code=502,
            detail=f"All available free model fallbacks failed. Last error: {last_error}",
        )

    return router


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
    Handles streaming responses with chunk-level reversible pseudonymization restoration.
    """
    restorer = StreamingRestorer(reverse_map)
    provider = candidate["provider"]
    tokens_estimated = 0

    try:
        async for chunk_bytes in egress_guard.stream_post(target_url, headers=headers, json_data=payload):
            chunk_str = chunk_bytes.decode("utf-8", errors="ignore")
            lines = chunk_str.split("\n")
            output_lines = []

            for line in lines:
                if line.startswith("data: "):
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        flushed = restorer.flush()
                        if flushed:
                            done_chunk = {
                                "id": "chatcmpl-stream",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": candidate["model_name"],
                                "choices": [{"index": 0, "delta": {"content": flushed}, "finish_reason": None}],
                            }
                            output_lines.append(f"data: {json.dumps(done_chunk)}\n\n")
                        output_lines.append("data: [DONE]\n\n")
                    else:
                        try:
                            chunk_obj = json.loads(data_str)
                            choices = chunk_obj.get("choices", [])
                            for choice in choices:
                                delta = choice.get("delta", {})
                                content = delta.get("content")
                                if isinstance(content, str):
                                    tokens_estimated += 1
                                    delta["content"] = restorer.feed(content)
                            output_lines.append(f"data: {json.dumps(chunk_obj)}\n\n")
                        except Exception:
                            output_lines.append(line + "\n")
                elif line:
                    output_lines.append(line + "\n")

            if output_lines:
                yield "".join(output_lines).encode("utf-8")

        # Increment quota count after stream completion
        await storage_db.increment_provider_usage(config.db_path, provider, tokens_estimated)

    except Exception as e:
        logger.error(f"Stream error on candidate {candidate['id']}: {e}")
        err_msg = json.dumps({"error": {"message": str(e), "type": "stream_error"}})
        yield f"data: {err_msg}\n\ndata: [DONE]\n\n".encode("utf-8")
