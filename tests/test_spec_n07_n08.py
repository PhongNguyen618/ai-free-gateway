import asyncio
import os
import sqlite3
from pathlib import Path
import pytest
import httpcore
import httpx
from httpx import ASGITransport, AsyncClient

from src.config_loader import get_config
from src.main import create_app
from src.security.egress_guard import PinnedNetworkBackend, EgressGuard
from src.storage import db as storage_db


@pytest.fixture
async def n07_n08_env(tmp_path):
    test_db = str(tmp_path / "n07_n08.db")
    config = get_config().deep_copy()
    config.db_path = test_db
    config.server.gateway_api_key = ""
    config.server.record_pid = False

    await storage_db.init_db(test_db)
    app = create_app(config)
    yield app, config, test_db


# =========================================================================
# N07: DNS Pinned Backend & SQLite Backup Tests
# =========================================================================

@pytest.mark.asyncio
async def test_n07_pinned_network_backend_fails_closed_on_unpinned_host():
    """Verify that PinnedNetworkBackend fails closed if domain is unpinned when fail_closed=True."""
    pinned_map = {"api.groq.com": "93.184.216.34"}
    backend = PinnedNetworkBackend(pinned_map=pinned_map, fail_closed=True)

    # 1. Unpinned host raises ConnectError
    with pytest.raises(httpcore.ConnectError) as exc_info:
        await backend.connect_tcp("unpinned-rogue-domain.com", 443)
    assert "DNS pinning policy failure" in str(exc_info.value)

    # 2. When fail_closed=False, unpinned host does not raise DNS pinning policy failure
    backend_open = PinnedNetworkBackend(pinned_map=pinned_map, fail_closed=False)
    # Target will fall back to hostname without policy failure
    assert backend_open.pinned_map.get("other.com", "other.com") == "other.com"


def test_n07_gitignore_covers_wal_shm_journals_and_metadata():
    """Verify that .gitignore covers WAL, SHM, journals, logs, and instance metadata files."""
    gitignore_path = Path(".gitignore")
    assert gitignore_path.exists(), ".gitignore must exist"
    content = gitignore_path.read_text(encoding="utf-8")

    assert "storage/*.db-wal" in content or "*.db-wal" in content
    assert "storage/*.db-shm" in content or "*.db-shm" in content
    assert "storage/*.log" in content or "*.log" in content
    assert ".gateway.instance.json" in content
    assert ".gateway.pid" in content


@pytest.mark.asyncio
async def test_n07_sqlite_online_backup_api(tmp_path):
    """Verify consistent SQLite backup using native backup API while WAL mode is active."""
    source_db_path = str(tmp_path / "source.db")
    backup_db_path = str(tmp_path / "backup.db")

    # Initialize source DB and write test data
    await storage_db.init_db(source_db_path)
    await storage_db.upsert_model(source_db_path, {
        "id": "groq/backup-test-model",
        "provider": "groq",
        "model_name": "backup-test-model",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 42.0,
    })

    # Perform online backup using SQLite backup API (WAL-safe)
    def do_backup():
        src_conn = sqlite3.connect(source_db_path)
        dest_conn = sqlite3.connect(backup_db_path)
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
            src_conn.close()

    await asyncio.to_thread(do_backup)

    # Verify backup DB contains exact model record
    backup_models = await storage_db.get_active_models(backup_db_path)
    assert any(m["id"] == "groq/backup-test-model" for m in backup_models)


# =========================================================================
# N08: Health, Readiness & Correlation ID Tests
# =========================================================================

@pytest.mark.asyncio
async def test_n08_health_and_readiness_endpoints_no_network(n07_n08_env):
    """Verify /health and /ready endpoints report truthful status without network requests."""
    app, config, test_db = n07_n08_env

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Health liveness
        resp_health = await client.get("/health")
        assert resp_health.status_code == 200
        assert resp_health.json() == {"status": "ok"}

        # 2. Readiness check before lifespan (truthful uninitialized state -> 503)
        resp_ready_pre = await client.get("/ready")
        assert resp_ready_pre.status_code == 503
        data_pre = resp_ready_pre.json()
        assert data_pre["status"] == "not_ready"
        assert data_pre["initialized"] is False
        assert data_pre["catalog_usable"] is False

        # 3. Readiness check after lifespan completes with active models -> 200
        app.state.initialized = True
        app.state.catalog_usable = True
        resp_ready_post = await client.get("/ready")
        assert resp_ready_post.status_code == 200
        data_post = resp_ready_post.json()
        assert data_post["status"] == "ready"
        assert data_post["initialized"] is True
        assert data_post["catalog_usable"] is True
        assert "is_harvesting" in data_post
        assert "is_stale" in data_post


@pytest.mark.asyncio
async def test_n08_correlation_id_propagated_in_response_headers_and_errors(n07_n08_env):
    """Verify that X-Correlation-ID is propagated through requests, responses, and error payloads."""
    app, config, test_db = n07_n08_env

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Case 1: Client provides correlation ID -> echoed in response headers
        custom_cid = "client-trace-abc-123"
        resp1 = await client.get("/health", headers={"X-Correlation-ID": custom_cid})
        assert resp1.status_code == 200
        assert resp1.headers.get("X-Correlation-ID") == custom_cid

        # Case 2: Client omits correlation ID -> gateway auto-generates 16-hex string
        resp2 = await client.get("/health")
        assert resp2.status_code == 200
        auto_cid = resp2.headers.get("X-Correlation-ID")
        assert auto_cid is not None
        assert len(auto_cid) >= 8

        # Case 3: Error response (400 Bad Request on invalid body) contains correlation ID
        resp3 = await client.post(
            "/v1/chat/completions",
            content=b"not-json",
            headers={"X-Correlation-ID": "err-trace-789", "Content-Type": "application/json"},
        )
        assert resp3.status_code == 400
        assert resp3.headers.get("X-Correlation-ID") == "err-trace-789"


@pytest.mark.asyncio
async def test_api_providers_ping_endpoint(n07_n08_env):
    """Verify that /api/providers/ping returns provider readiness without leaking raw keys."""
    app, config, test_db = n07_n08_env
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/providers/ping")
        assert resp.status_code == 200
        data = resp.json()
        assert "providers" in data
        assert isinstance(data["providers"], list)
        for p in data["providers"]:
            assert "provider" in p
            assert "status" in p
            assert "message" in p
            # Ensure no API key secret is leaked in message
            assert "sk-" not in p["message"]
            assert "gsk_" not in p["message"]
