from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from src.config_loader import AppConfig, get_config
from src.discovery.harvester import ModelHarvester
from src.router.balancer import ModelBalancer
from src.router.openai_proxy import create_openai_router
from src.security.approval_gate import ApprovalGate
from src.security.egress_guard import EgressGuard
from src.storage import db as storage_db

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("ai_free_gateway")

# Load application configuration
config: AppConfig = get_config()

# Initialize core security, storage, routing, and discovery modules
egress_guard = EgressGuard(
    whitelist_domains=config.egress.whitelist_domains,
    timeout_seconds=config.egress.timeout_seconds,
    max_request_bytes=config.egress.max_request_bytes,
    block_private_ips=config.egress.block_private_ips,
)
approval_gate = ApprovalGate(db_path=config.db_path)
balancer = ModelBalancer(config=config)
harvester = ModelHarvester(config=config, egress_guard=egress_guard)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle management."""
    logger.info("Initializing Local Zero-Trust Database...")
    await storage_db.init_db(config.db_path)

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
    logger.info("Shutdown complete.")


app = FastAPI(
    title="AI Free Gateway",
    description="Local Zero-Trust OpenAI-Compatible Proxy & Free-Tier AI Harvester",
    version="1.0.0",
    lifespan=lifespan,
)

# Enable CORS for local dashboards, development tools, and extensions
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- UI & Dashboard Routes ---

@app.get("/", include_in_schema=False)
@app.get("/ui", include_in_schema=False)
async def serve_dashboard():
    """Serve the single-file dark mode monitoring and HITL approval dashboard."""
    ui_path = Path("ui/index.html")
    if not ui_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard UI not found")
    return FileResponse(ui_path, media_type="text/html")


@app.get("/health", tags=["System"])
async def health_check() -> Dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/stats", tags=["Dashboard"])
async def get_stats() -> Dict[str, Any]:
    """Retrieve top-level gateway performance and DLP metrics."""
    return await storage_db.get_dashboard_stats(config.db_path)


@app.get("/api/models", tags=["Dashboard"])
async def get_dashboard_models() -> List[Dict[str, Any]]:
    """Retrieve all discovered free models in the pool."""
    return await storage_db.get_active_models(config.db_path)


@app.get("/api/approvals/pending", tags=["Dashboard"])
async def get_pending_approvals() -> List[Dict[str, Any]]:
    """Retrieve pending Human-in-the-Loop review items."""
    return await storage_db.get_pending_approvals(config.db_path)


class DecisionRequest(BaseModel):
    decision: str  # "approved" or "rejected"


@app.post("/api/approvals/{approval_id}/decision", tags=["Dashboard"])
async def submit_approval_decision(approval_id: str, payload: DecisionRequest) -> Dict[str, Any]:
    """Submit human approval or rejection decision for a pending request."""
    success = await approval_gate.submit_decision(approval_id, payload.decision)
    if not success:
        raise HTTPException(status_code=404, detail="Approval request not found or already decided")
    return {"status": "success", "approval_id": approval_id, "decision": payload.decision}


@app.get("/api/dlp/logs", tags=["Dashboard"])
async def get_dlp_logs(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Retrieve offline DLP scan and redaction audit records."""
    return await storage_db.get_dlp_logs(config.db_path, limit=limit, offset=offset)


@app.get("/api/quotas", tags=["Dashboard"])
async def get_quotas() -> List[Dict[str, Any]]:
    """Retrieve provider quota consumption and 429 cooldown status."""
    return await storage_db.get_all_quota_states(config.db_path)


@app.get("/api/routing/status", tags=["Dashboard"])
async def get_routing_status() -> Dict[str, Any]:
    """Retrieve current active model in use and upcoming fallback chain."""
    return await balancer.get_routing_status()


@app.post("/api/harvester/trigger", tags=["Dashboard"])
async def trigger_harvester() -> Dict[str, Any]:
    """Manually trigger free-tier model discovery."""
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


def run():
    uvicorn.run(
        "src.main:app",
        host=config.server.host,
        port=config.server.port,
        reload=config.server.debug,
    )


if __name__ == "__main__":
    run()
