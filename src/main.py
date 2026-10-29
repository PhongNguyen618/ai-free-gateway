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

from src.config_loader import AppConfig, PROJECT_ROOT, get_config
from src.discovery.harvester import ModelHarvester
from src.router.balancer import ModelBalancer
from src.router.openai_proxy import create_openai_router, make_openai_error
from src.security.approval_gate import ApprovalGate
from src.security.egress_guard import EgressGuard
from src.storage import db as storage_db

logger = logging.getLogger("ai_free_gateway")


class DecisionRequest(BaseModel):
    decision: str = Field(description="'approved' or 'rejected'")


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
        block_private_ips=config.egress.block_private_ips,
        allow_http=config.egress.allow_http,
    )
    approval_gate = ApprovalGate(db_path=config.db_path)
    balancer = ModelBalancer(config=config)
    harvester = ModelHarvester(config=config, egress_guard=egress_guard)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Initializing Local Zero-Trust Database...")
        await storage_db.init_db(config.db_path)

        # F11: Write current process PID to project root for safe stop verification
        pid_file = PROJECT_ROOT / ".gateway.pid"
        should_record_pid = config.server.record_pid and (os.environ.get("GATEWAY_TEST_MODE", "").lower() not in ("1", "true"))
        if should_record_pid:
            try:
                pid_file.write_text(str(os.getpid()), encoding="utf-8")
            except Exception as e:
                logger.warning(f"Could not record .gateway.pid: {e}")

        # F09: Clean up any orphan pending approvals from prior crashes/restarts
        cleaned = await storage_db.cleanup_orphan_pending(config.db_path)
        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} orphan pending approvals from previous session.")

        logger.info("Performing initial model harvesting and provider registration...")
        try:
            await harvester.harvest_all()
        except Exception as e:
            logger.error(f"Initial harvest encountered an issue (will retry in background): {e}")

        logger.info("Starting background model discovery task...")
        harvester.start_background()

        yield

        logger.info("Shutting down background tasks & connections...")
        await harvester.stop()
        await egress_guard.close()

        # Clean up PID file on graceful shutdown
        if should_record_pid:
            try:
                if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    pid_file.unlink(missing_ok=True)
            except Exception:
                pass
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

    # F01: Strict CORS configuration (defaults to same-origin)
    allowed_origins = list(config.server.cors_allowed_origins)
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

    # F01 & CSRF Protection Middleware
    @app.middleware("http")
    async def origin_and_csrf_protection_middleware(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and request.method in ("POST", "PUT", "DELETE", "PATCH"):
            # Check if origin is allowed or matches host
            parsed_origin = urlparse(origin)
            origin_host = parsed_origin.hostname or ""
            origin_port = parsed_origin.port or (443 if parsed_origin.scheme == "https" else 80)

            # Local same-host check
            req_host = request.url.hostname or "127.0.0.1"
            req_port = request.url.port or 80

            is_same_host = (origin_host == req_host) and (origin_port == req_port)
            is_whitelisted = origin in allowed_origins

            if not is_same_host and not is_whitelisted:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Cross-site state modification blocked by Zero-Trust Origin policy."},
                )

        return await call_next(request)

    # F01: Shared Authentication Dependency
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

        if not provided or not secrets.compare_digest(provided, master_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Missing or invalid Gateway API key.",
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

    @app.get("/api/stats", tags=["Dashboard"], dependencies=[Depends(verify_auth)])
    async def get_stats() -> Dict[str, Any]:
        return await storage_db.get_dashboard_stats(config.db_path)

    @app.get("/api/models", tags=["Dashboard"], dependencies=[Depends(verify_auth)])
    async def get_dashboard_models() -> List[Dict[str, Any]]:
        return await storage_db.get_active_models(config.db_path)

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

    # --- Mount OpenAI Compatible Proxy Router ---
    openai_router = create_openai_router(
        config=config,
        egress_guard=egress_guard,
        approval_gate=approval_gate,
        balancer=balancer,
        harvester=harvester,
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
