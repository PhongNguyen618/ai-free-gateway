import asyncio
import json
import pytest
from httpx import AsyncClient, ASGITransport
from src.main import create_app
from src.storage import db as storage_db
from src.config_loader import get_config


@pytest.fixture
async def isolated_app(tmp_path):
    """Provide isolated app instance and database per test (F10)."""
    test_db = str(tmp_path / "test_api_endpoints.db")
    config = get_config()
    config.db_path = test_db

    await storage_db.init_db(test_db)
    # Register mock model in isolated pool
    await storage_db.upsert_model(test_db, {
        "id": "groq/llama-3.3-70b-versatile",
        "provider": "groq",
        "model_name": "llama-3.3-70b-versatile",
        "display_name": "Llama 3.3 70B",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 100.0,
    })

    app = create_app(config)
    yield app, test_db


@pytest.mark.asyncio
async def test_health_and_ui(isolated_app):
    app, _ = isolated_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

        # UI endpoint
        ui_resp = await client.get("/")
        assert ui_resp.status_code == 200
        assert "AI Free Gateway" in ui_resp.text


@pytest.mark.asyncio
async def test_api_models(isolated_app):
    app, _ = isolated_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        assert len(data["data"]) >= 1
        assert any(m["id"] == "groq/llama-3.3-70b-versatile" for m in data["data"])


@pytest.mark.asyncio
async def test_dlp_high_risk_rejection(isolated_app):
    app, _ = isolated_app
    app.state.config.dlp.risk_action.high = "reject"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Prompt containing an AWS Secret Key (High Risk)
        payload = {
            "model": "auto",
            "messages": [
                {"role": "user", "content": "Help me configure this key: AKIAIOSFODNN7EXAMPLE"}
            ]
        }
        resp = await client.post("/v1/chat/completions", json=payload)
        # Should be rejected with 403 Forbidden by Zero-Trust DLP policy
        assert resp.status_code == 403
        data = resp.json()
        assert "dlp_violation" in str(data)


@pytest.mark.asyncio
async def test_dashboard_stats(isolated_app):
    app, _ = isolated_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stats")
        assert resp.status_code == 200
        stats = resp.json()
        assert "total_active_models" in stats
        assert "dlp_incidents_count" in stats
        assert stats["total_active_models"] >= 1


@pytest.mark.asyncio
async def test_routing_status_endpoint(isolated_app):
    app, _ = isolated_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/routing/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "active_model" in data
        assert "fallback_chain" in data
        assert isinstance(data["fallback_chain"], list)
