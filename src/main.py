from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from src.config_loader import AppConfig, PROJECT_ROOT, get_config, get_provider_api_key
from src.discovery.harvester import ModelHarvester
from src.hub.event_bus import SharedEventBus
from src.hub.orchestrator import AIAutoOrchestrator
from src.hub.registry import ProjectRegistry
from src.lifecycle import InstanceLock, InstanceAlreadyRunningError
from src.router.balancer import ModelBalancer
from src.router.openai_proxy import create_openai_router, make_openai_error
from src.router.response_cache import ResponseCache
from src.security.approval_gate import ApprovalGate
from src.security.egress_guard import EgressGuard
from src.security.project_quota import ProjectQuotaGuard
from src.storage import db as storage_db

logger = logging.getLogger("ai_free_gateway")


class DecisionRequest(BaseModel):
    decision: str = Field(description="'approved' or 'rejected'")


class HubRegisterRequest(BaseModel):
    project_id: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    tools: Optional[List[Dict[str, Any]]] = None


class HubHeartbeatRequest(BaseModel):
    project_id: str


class HubCommandResultRequest(BaseModel):
    command_id: str
    project_id: str
    status: str
    result: Optional[Any] = None
    error: Optional[str] = None


class HubPublishEventRequest(BaseModel):
    topic: str
    source_project: str
    payload: Dict[str, Any]
    priority: Optional[str] = "normal"


class HubOrchestrateRequest(BaseModel):
    query: str
    model_tier: Optional[str] = "reasoning"


def create_app(app_config: Optional[AppConfig] = None) -> FastAPI:
    """
    Application Factory for AI Free Gateway.
    Instantiates isolated dependencies per app instance (F10 App Isolation).
    """
    config = (app_config or get_config()).deep_copy()

    # F01: External host bind security check
    host_lower = config.server.host.lower().strip()
    is_loopback = host_lower in ("127.0.0.1", "localhost", "::1")
    if not is_loopback and not config.server.gateway_api_key and not config.server.allow_insecure_external:
        raise RuntimeError(
            f"Binding gateway to external non-loopback host '{config.server.host}' is prohibited without "
            "authentication. Configure 'gateway_api_key' or explicitly set 'allow_insecure_external: true'."
        )

    # Initialize isolated components
    egress_guard = EgressGuard(
        whitelist_domains=config.egress.whitelist_domains,
        timeout_seconds=config.egress.timeout_seconds,
        max_request_bytes=config.egress.max_request_bytes,
        max_response_bytes=config.egress.max_response_bytes,
        block_private_ips=config.egress.block_private_ips,
        allow_http=config.egress.allow_http,
    )
    approval_gate = ApprovalGate(db_path=config.db_path)
    balancer = ModelBalancer(config=config)
    harvester = ModelHarvester(config=config, egress_guard=egress_guard)
    response_cache = ResponseCache()
    project_quota_guard = ProjectQuotaGuard()
    project_registry = ProjectRegistry()
    shared_event_bus = SharedEventBus(
        persistence_callback=lambda evt: storage_db.save_hub_event(config.db_path, evt)
    )

    async def internal_ai_chat(messages: List[Dict[str, str]], model_tier: str = "reasoning") -> str:
        # S02: 1. Deterministic Cache Check
        cache_key = ResponseCache.compute_cache_key(model_tier, messages)
        cached = response_cache.get(cache_key)
        if cached and "content" in cached:
            content_obj = cached["content"]
            if isinstance(content_obj, dict) and "choices" in content_obj:
                return content_obj["choices"][0]["message"]["content"]
            elif isinstance(content_obj, str):
                return content_obj

        # S02: 2. Fallback candidate chain loop
        candidates = await balancer.get_candidate_chain(model_tier)
        if not candidates:
            raise RuntimeError("No available AI model candidate in current catalog.")

        last_error: Optional[Exception] = None
        for candidate in candidates:
            target_url = candidate["endpoint_url"]
            provider_name = candidate["provider"]
            api_key = get_provider_api_key(provider_name, config)
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {
                "model": candidate["model_name"],
                "messages": messages,
                "temperature": 0.2,
            }
            try:
                resp = await egress_guard.post(target_url, headers=headers, json_data=payload)
                if resp.status_code >= 400:
                    raise RuntimeError(f"Provider {provider_name} returned HTTP {resp.status_code}: {resp.text[:150]}")
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                # Save into response cache
                response_cache.set(cache_key, content, ttl_seconds=180.0)
                return content
            except Exception as exc:
                last_error = exc
                logger.warning(f"Internal AI candidate {candidate.get('id')} failed: {exc}. Trying fallback...")
                await balancer.record_error(candidate.get("id", ""))
                continue

        raise RuntimeError(f"All AI candidates for tier '{model_tier}' failed. Last error: {last_error}")

    orchestrator = AIAutoOrchestrator(
        registry=project_registry,
        event_bus=shared_event_bus,
        chat_fn=internal_ai_chat,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # N05: Acquire OS-backed instance lock before cleanup or startup
        instance_lock: Optional[InstanceLock] = None
        should_lock = config.server.record_pid and (os.environ.get("GATEWAY_TEST_MODE", "").lower() not in ("1", "true"))
        if should_lock:
            instance_lock = InstanceLock(db_path=config.db_path, project_root=PROJECT_ROOT)
            instance_lock.acquire()

        try:
            logger.info("Initializing Local Zero-Trust Database...")
            await storage_db.init_db(config.db_path)

            # F09: Clean up any orphan pending approvals from prior crashes/restarts
            cleaned = await storage_db.cleanup_orphan_pending(config.db_path)
            if cleaned > 0:
                logger.info(f"Cleaned up {cleaned} orphan pending approvals from previous session.")

            # S04: Load persisted hub events into SharedEventBus
            try:
                past_events = await storage_db.load_recent_hub_events(config.db_path, limit=100)
                shared_event_bus.init_from_history(past_events)
            except Exception as e_ev:
                logger.warning(f"Could not load historical hub events: {e_ev}")

            logger.info("Performing initial model harvesting and provider registration...")
            try:
                await harvester.harvest_all()
            except Exception as e:
                logger.error(f"Initial harvest encountered an issue (will retry in background): {e}")

            logger.info("Starting background model discovery task...")
            harvester.start_background()

            active_models = await storage_db.get_active_models(config.db_path)
            app.state.initialized = True
            app.state.catalog_usable = len(active_models) > 0

            yield

        finally:
            logger.info("Shutting down background tasks & connections...")
            app.state.initialized = False
            app.state.catalog_usable = False
            await harvester.stop()
            await egress_guard.close()

            # Clean up instance lock and metadata on graceful shutdown (N05)
            if instance_lock:
                instance_lock.release()

            logger.info("Shutdown complete.")

    app = FastAPI(
        title="AI Free Gateway",
        description="Local Zero-Trust OpenAI-Compatible Proxy & Free-Tier AI Harvester",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Store components on app.state for testing / inspection
    app.state.config = config
    app.state.egress_guard = egress_guard
    app.state.approval_gate = approval_gate
    app.state.balancer = balancer
    app.state.harvester = harvester
    app.state.response_cache = response_cache
    app.state.project_quota_guard = project_quota_guard
    app.state.project_registry = project_registry
    app.state.shared_event_bus = shared_event_bus
    app.state.orchestrator = orchestrator
    app.state.initialized = False
    app.state.catalog_usable = False

    # F01: Strict CORS configuration (defaults to same-origin)
    allowed_origins = list(config.server.cors_allowed_origins)
    if allowed_origins:
        allow_cred = "*" not in allowed_origins
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=allow_cred,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

    # Host header allowlist (N04)
    trusted_hosts = {
        "127.0.0.1",
        "localhost",
        "::1",
        "[::1]",
        "testserver",
        "test",
        f"127.0.0.1:{config.server.port}",
        f"localhost:{config.server.port}",
        f"[::1]:{config.server.port}",
    }
    configured_host = config.server.host.lower().strip()
    trusted_hosts.add(configured_host)
    trusted_hosts.add(f"{configured_host}:{config.server.port}")
    for h in config.server.allowed_hosts:
        trusted_hosts.add(h.lower().strip())
    if "*" in config.server.allowed_hosts or "*" in trusted_hosts:
        trusted_hosts.add("*")

    # F01 & CSRF Protection Middleware (N04, N08)
    @app.middleware("http")
    async def origin_and_csrf_protection_middleware(request: Request, call_next):
        # 0. Correlation ID injection (N08)
        correlation_id = request.headers.get("X-Correlation-ID") or secrets.token_hex(8)
        request.state.correlation_id = correlation_id

        # 1. Host header validation (N04)
        host_header = request.headers.get("host")
        if host_header and "*" not in trusted_hosts:
            clean_host = host_header.lower().strip()
            clean_host_no_port = clean_host.split(":")[0]
            if clean_host not in trusted_hosts and clean_host_no_port not in trusted_hosts:
                res = JSONResponse(
                    status_code=400,
                    content={"detail": f"Invalid or untrusted Host header: {host_header}"},
                )
                res.headers["X-Correlation-ID"] = correlation_id
                return res

        # 2. Origin & CSRF validation for state-changing methods (N04)
        origin = request.headers.get("origin")
        if origin and request.method in ("POST", "PUT", "DELETE", "PATCH"):
            if origin.strip().lower() == "null":
                res = JSONResponse(
                    status_code=403,
                    content={"detail": "Null origin is not permitted for state-modifying operations."},
                )
                res.headers["X-Correlation-ID"] = correlation_id
                return res

            try:
                parsed_origin = urlparse(origin)
                origin_scheme = (parsed_origin.scheme or "").lower()
                origin_host = (parsed_origin.hostname or "").lower()
                origin_port = parsed_origin.port or (443 if origin_scheme == "https" else 80)
                if not origin_scheme or not origin_host:
                    raise ValueError("Missing scheme or host in Origin")
            except Exception:
                res = JSONResponse(
                    status_code=400,
                    content={"detail": "Malformed Origin header."},
                )
                res.headers["X-Correlation-ID"] = correlation_id
                return res

            # Strict same-origin check including scheme, host, and port (N04)
            req_scheme = (request.url.scheme or "http").lower()
            req_host = (request.url.hostname or "127.0.0.1").lower()
            req_port = request.url.port or (443 if req_scheme == "https" else 80)

            is_same_origin = (
                origin_scheme == req_scheme
                and origin_host == req_host
                and origin_port == req_port
            )
            is_whitelisted = origin in allowed_origins or "*" in allowed_origins

            if not is_same_origin and not is_whitelisted:
                res = JSONResponse(
                    status_code=403,
                    content={"detail": "Cross-site state modification blocked by Zero-Trust Origin policy."},
                )
                res.headers["X-Correlation-ID"] = correlation_id
                return res

        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        return response

    # F01: Shared Authentication Dependency (N04 Safe Unicode/ASCII Handling)
    async def verify_auth(
        authorization: Optional[str] = Header(None),
        x_gateway_key: Optional[str] = Header(None, alias="X-Gateway-Key"),
    ) -> None:
        """Enforces constant-time key validation across dashboard /api/* and proxy /v1/*."""
        master_key = config.server.gateway_api_key.strip()
        if not master_key:
            return

        provided = ""
        if authorization and authorization.startswith("Bearer "):
            provided = authorization.split("Bearer ", 1)[1].strip()
        elif x_gateway_key:
            provided = x_gateway_key.strip()

        try:
            if not provided or not secrets.compare_digest(
                provided.encode("utf-8", errors="replace"),
                master_key.encode("utf-8", errors="replace"),
            ):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Unauthorized: Missing or invalid Gateway API key.",
                )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Invalid authorization format.",
            )

    # --- UI & Dashboard Routes ---

    @app.get("/", include_in_schema=False)
    @app.get("/ui", include_in_schema=False)
    async def serve_dashboard():
        """Serve the single-file dark mode monitoring and HITL approval dashboard."""
        ui_path = PROJECT_ROOT / "ui" / "index.html"
        if not ui_path.exists():
            # Fallback to relative
            ui_path = Path("ui/index.html")
        if not ui_path.exists():
            raise HTTPException(status_code=404, detail="Dashboard UI not found")
        return FileResponse(ui_path, media_type="text/html")

    @app.get("/health", tags=["System"])
    async def health_check() -> Dict[str, str]:
        """Minimal health check without leaking internal metadata."""
        return {"status": "ok"}

    @app.get("/ready", tags=["System"])
    async def readiness_check() -> Response:
        """Readiness check reporting startup completion and catalog status without network calls (N08)."""
        is_init = getattr(app.state, "initialized", False)
        is_usable = getattr(app.state, "catalog_usable", False)

        status_str = "ready" if (is_init and is_usable) else "not_ready"
        status_code = 200 if (is_init and is_usable) else 503

        return JSONResponse(
            status_code=status_code,
            content={
                "status": status_str,
                "initialized": is_init,
                "catalog_usable": is_usable,
                "is_harvesting": harvester.is_harvesting,
                "is_stale": harvester.is_stale,
            },
        )

    @app.get("/api/stats", tags=["Dashboard"], dependencies=[Depends(verify_auth)])
    async def get_stats() -> Dict[str, Any]:
        return await storage_db.get_dashboard_stats(config.db_path)

    @app.get("/api/models", tags=["Dashboard"], dependencies=[Depends(verify_auth)])
    async def get_dashboard_models() -> List[Dict[str, Any]]:
        all_models = await storage_db.get_active_models(config.db_path)
        try:
            candidates = await balancer.get_candidate_chain("auto")
            candidate_ids = [c["id"] for c in candidates]
            rank_map = {cid: idx + 1 for idx, cid in enumerate(candidate_ids)}

            def sort_key(m):
                mid = m.get("id")
                if mid in rank_map:
                    return (0, rank_map[mid], float(m.get("latency_ms", 0.0) or 0.0))
                return (1, int(m.get("error_count", 0) or 0), float(m.get("latency_ms", 0.0) or 0.0))

            sorted_models = sorted(all_models, key=sort_key)
            for m in sorted_models:
                m["priority_rank"] = rank_map.get(m.get("id"), None)
            return sorted_models
        except Exception:
            return all_models

    @app.get("/api/approvals/pending", tags=["Dashboard"], dependencies=[Depends(verify_auth)])
    async def get_pending_approvals() -> List[Dict[str, Any]]:
        return await storage_db.get_pending_approvals(config.db_path)

    @app.post("/api/approvals/{approval_id}/decision", tags=["Dashboard"], dependencies=[Depends(verify_auth)])
    async def submit_approval_decision(approval_id: str, payload: DecisionRequest) -> Dict[str, Any]:
        dec = payload.decision.lower().strip()
        if dec not in ("approved", "rejected"):
            raise HTTPException(status_code=422, detail="Decision must be 'approved' or 'rejected'")

        existing = await storage_db.get_approval_by_id(config.db_path, approval_id)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Approval request '{approval_id}' not found")
        if existing.get("status") != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"Approval request '{approval_id}' is already finalized (status: {existing.get('status')})",
            )

        success = await approval_gate.submit_decision(approval_id, dec)
        if not success:
            raise HTTPException(
                status_code=409,
                detail=f"Approval request '{approval_id}' could not be decided (already finalized or expired)",
            )
        return {"status": "success", "approval_id": approval_id, "decision": dec}

    @app.get("/api/dlp/logs", tags=["Dashboard"], dependencies=[Depends(verify_auth)])
    async def get_dlp_logs(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        return await storage_db.get_dlp_logs(config.db_path, limit=limit, offset=offset)

    @app.get("/api/quotas", tags=["Dashboard"], dependencies=[Depends(verify_auth)])
    async def get_quotas() -> List[Dict[str, Any]]:
        return await storage_db.get_all_quota_states(config.db_path)

    @app.get("/api/routing/status", tags=["Dashboard"], dependencies=[Depends(verify_auth)])
    async def get_routing_status() -> Dict[str, Any]:
        return await balancer.get_routing_status()

    @app.post("/api/harvester/trigger", tags=["Dashboard"], dependencies=[Depends(verify_auth)])
    async def trigger_harvester() -> Dict[str, Any]:
        return await harvester.harvest_all()

    @app.get("/api/providers/ping", tags=["Dashboard"], dependencies=[Depends(verify_auth)])
    async def ping_providers() -> Dict[str, Any]:
        results = []
        providers_list = ["groq", "openrouter", "gemini", "github"]
        all_models = await storage_db.get_active_models(config.db_path)
        for p_name in providers_list:
            pcfg = getattr(config.providers, p_name, None)
            if not pcfg or not pcfg.enabled:
                results.append({
                    "provider": p_name,
                    "status": "disabled",
                    "message": "Đã tắt trong cấu hình",
                    "models_count": 0,
                })
                continue
            api_key = get_provider_api_key(p_name, config)
            if not api_key:
                results.append({
                    "provider": p_name,
                    "status": "no_key",
                    "message": f"Thiếu API Key ({pcfg.api_key_env or 'chưa điền'})",
                    "models_count": 0,
                })
                continue
            is_cd = await storage_db.is_provider_in_cooldown(config.db_path, p_name)
            if is_cd:
                results.append({
                    "provider": p_name,
                    "status": "cooldown",
                    "message": "Đang trong Cooldown 429",
                    "models_count": 0,
                })
                continue
            p_models = [m for m in all_models if m.get("provider") == p_name]
            results.append({
                "provider": p_name,
                "status": "ready",
                "message": f"Sẵn sàng ({len(p_models)} model)",
                "models_count": len(p_models),
            })
        return {"providers": results}

    @app.get("/api/projects/stats", tags=["Dashboard"], dependencies=[Depends(verify_auth)])
    async def get_project_stats() -> Dict[str, Any]:
        """Thống kê RPM, Request Count và Vi phạm Rate Limit theo Project ID."""
        return project_quota_guard.get_all_stats()

    @app.get("/api/cache/stats", tags=["Dashboard"], dependencies=[Depends(verify_auth)])
    async def get_cache_stats() -> Dict[str, Any]:
        """Thống kê Hits, Misses, Hit Ratio và Số lượng entry trong Response Cache."""
        return response_cache.get_stats()

    # --- Agent Hub (All-in-One) Endpoints (S01: Protected by verify_auth) ---

    @app.post("/api/hub/register", tags=["Agent Hub"], dependencies=[Depends(verify_auth)])
    async def hub_register(payload: HubRegisterRequest) -> Dict[str, Any]:
        """Đăng ký dự án và công cụ MCP-Lite kết nối vào Hub."""
        return project_registry.register_project(
            project_id=payload.project_id,
            display_name=payload.display_name,
            description=payload.description,
            tools=payload.tools,
        )

    @app.post("/api/hub/heartbeat", tags=["Agent Hub"], dependencies=[Depends(verify_auth)])
    async def hub_heartbeat(payload: HubHeartbeatRequest) -> Dict[str, Any]:
        """Nhận nhịp tim từ node dự án và trả về các lệnh điều phối đang chờ chạy."""
        cmds = project_registry.heartbeat(payload.project_id)
        return {"status": "ok", "commands": cmds}

    @app.post("/api/hub/command/result", tags=["Agent Hub"], dependencies=[Depends(verify_auth)])
    async def hub_command_result(payload: HubCommandResultRequest) -> Dict[str, Any]:
        """Node dự án trả về kết quả sau khi thực thi lệnh công cụ."""
        success = project_registry.submit_command_result(
            command_id=payload.command_id,
            project_id=payload.project_id,
            status=payload.status,
            result=payload.result,
            error=payload.error,
        )
        return {"status": "accepted" if success else "ignored"}

    @app.get("/api/hub/projects", tags=["Agent Hub"], dependencies=[Depends(verify_auth)])
    async def hub_get_projects() -> List[Dict[str, Any]]:
        """Lấy danh sách các dự án đang kết nối (Online / Offline)."""
        return project_registry.get_all_projects()

    @app.get("/api/hub/tools", tags=["Agent Hub"], dependencies=[Depends(verify_auth)])
    async def hub_get_tools() -> List[Dict[str, Any]]:
        """Lấy danh sách tất cả công cụ (MCP Tools) của các dự án đang online."""
        return project_registry.get_all_tools()

    @app.post("/api/hub/events/publish", tags=["Agent Hub"], dependencies=[Depends(verify_auth)])
    async def hub_publish_event(payload: HubPublishEventRequest) -> Dict[str, Any]:
        """Phát sự kiện lên Bảng Tin Chung (Shared Event Bus)."""
        return shared_event_bus.publish(
            topic=payload.topic,
            source_project=payload.source_project,
            payload=payload.payload,
            priority=payload.priority or "normal",
        )

    @app.get("/api/hub/events/poll", tags=["Agent Hub"], dependencies=[Depends(verify_auth)])
    async def hub_poll_events(topic: Optional[str] = None, since_sequence: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        """Lấy các sự kiện mới nhất từ Bảng Tin Chung."""
        topics = [topic] if topic else None
        return shared_event_bus.poll(topics=topics, since_sequence=since_sequence, limit=limit)

    @app.get("/api/hub/events/history", tags=["Agent Hub"], dependencies=[Depends(verify_auth)])
    async def hub_event_history(limit: int = 100, topic: Optional[str] = None) -> List[Dict[str, Any]]:
        """Xem lịch sử các thông điệp trao đổi trên Bảng Tin Chung."""
        return shared_event_bus.get_history(limit=limit, topic=topic)

    @app.post("/api/hub/orchestrate", tags=["Agent Hub"], dependencies=[Depends(verify_auth)])
    async def hub_orchestrate(payload: HubOrchestrateRequest) -> Dict[str, Any]:
        """AI Trưởng Phòng nhận câu lệnh tiếng Việt, tự động chọn công cụ dự án để điều phối."""
        return await orchestrator.orchestrate(
            user_query=payload.query,
            model_tier=payload.model_tier or "reasoning",
        )

    # --- Mount OpenAI Compatible Proxy Router ---
    openai_router = create_openai_router(
        config=config,
        egress_guard=egress_guard,
        approval_gate=approval_gate,
        balancer=balancer,
        harvester=harvester,
        response_cache=response_cache,
        project_quota_guard=project_quota_guard,
    )
    app.include_router(openai_router)

    return app


# Default singleton app for uvicorn run
app: FastAPI = create_app()


def run():
    cfg = get_config()
    uvicorn.run(
        "src.main:app",
        host=cfg.server.host,
        port=cfg.server.port,
        reload=cfg.server.debug,
    )


if __name__ == "__main__":
    run()
