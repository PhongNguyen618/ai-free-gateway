from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Any, AsyncIterator, Dict, List, Optional
import pytest
import httpx
from httpx import AsyncClient, ASGITransport

from src.config_loader import get_config
from src.main import create_app
from src.security.approval_gate import ApprovalGate
from src.storage import db as storage_db


@pytest.fixture
async def spec_b_env(tmp_path):
    test_db = str(tmp_path / "spec_b_test.db")
    config = get_config().deep_copy()
    config.db_path = test_db
    config.server.gateway_api_key = "test-gateway-secret-key"
    config.server.record_pid = False
    config.server.host = "127.0.0.1"
    config.server.port = 8000
    config.server.cors_allowed_origins = ["http://trusted-partner.internal"]
    config.server.allowed_hosts = ["mygateway.local:8000", "mygateway.local"]
    config.dlp.approval_timeout_seconds = 2

    await storage_db.init_db(test_db)
    await storage_db.upsert_model(test_db, {
        "id": "groq/primary-model",
        "provider": "groq",
        "model_name": "primary-model",
        "display_name": "Groq Primary",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 30.0,
    })

    app = create_app(config)
    yield app, config, test_db


# =========================================================================
# N03: HITL Cancellation & Consistent DB State
# =========================================================================

@pytest.mark.asyncio
async def test_n03_atomic_expiry_predicate(tmp_path):
    test_db = str(tmp_path / "expiry_test.db")
    await storage_db.init_db(test_db)

    now = datetime.now(timezone.utc)
    app_id = "test-app-id-1"
    # Create request with expiration in future
    future_exp = (now + timedelta(seconds=10)).isoformat()
    await storage_db.insert_pending_approval(test_db, {
        "id": app_id,
        "created_at": now.isoformat(),
        "expires_at": future_exp,
        "status": "pending",
        "client_ip": "127.0.0.1",
        "risk_level": "HIGH",
        "model_requested": "auto",
        "findings": [],
        "sanitized_messages": [],
        "sanitized_payload": {},
    })

    # 1. Attempting to expire before deadline MUST fail
    expired_early = await storage_db.expire_pending_approval(test_db, app_id)
    assert not expired_early
    row = await storage_db.get_approval_by_id(test_db, app_id)
    assert row["status"] == "pending"

    # 2. Update expires_at to past and expire
    past_exp = (now - timedelta(seconds=2)).isoformat()
    async with storage_db.get_db(test_db) as conn:
        await conn.execute("UPDATE pending_approvals SET expires_at = ? WHERE id = ?", (past_exp, app_id))
        await conn.commit()

    expired_ok = await storage_db.expire_pending_approval(test_db, app_id)
    assert expired_ok
    row_after = await storage_db.get_approval_by_id(test_db, app_id)
    assert row_after["status"] == "expired"


@pytest.mark.asyncio
async def test_n03_admin_approval_wins_race_over_timeout(tmp_path):
    test_db = str(tmp_path / "race_test.db")
    gate = ApprovalGate(db_path=test_db)
    await storage_db.init_db(test_db)

    app_id = await gate.create_request(
        risk_level="HIGH",
        client_ip="127.0.0.1",
        model_requested="auto",
        findings=[],
        sanitized_messages=[{"role": "user", "content": "hello"}],
        timeout_seconds=1,
    )

    # Admin approves immediately
    submitted = await gate.submit_decision(app_id, "approved")
    assert submitted

    # Waiter resolves authoritative decision
    decision, item = await gate.wait_for_decision(app_id, timeout_seconds=1)
    assert decision == "approved"
    assert gate.get_active_count() == 0

    # Ensure DB record is approved and was not overwritten by timeout
    row = await storage_db.get_approval_by_id(test_db, app_id)
    assert row["status"] == "approved"


@pytest.mark.asyncio
async def test_n03_client_cancellation_marks_cancelled_and_cleans_memory(tmp_path):
    test_db = str(tmp_path / "cancel_test.db")
    gate = ApprovalGate(db_path=test_db)
    await storage_db.init_db(test_db)

    app_id = await gate.create_request(
        risk_level="HIGH",
        client_ip="127.0.0.1",
        model_requested="auto",
        findings=[],
        sanitized_messages=[{"role": "user", "content": "hello"}],
        timeout_seconds=30,
    )
    assert gate.get_active_count() == 1

    # Client cancels request (e.g. disconnect)
    await gate.cancel_request(app_id)
    assert gate.get_active_count() == 0

    row = await storage_db.get_approval_by_id(test_db, app_id)
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_n03_cancellation_does_not_overwrite_committed_admin_decision(tmp_path):
    test_db = str(tmp_path / "committed_test.db")
    gate = ApprovalGate(db_path=test_db)
    await storage_db.init_db(test_db)

    app_id = await gate.create_request(
        risk_level="HIGH",
        client_ip="127.0.0.1",
        model_requested="auto",
        findings=[],
        sanitized_messages=[{"role": "user", "content": "hello"}],
        timeout_seconds=30,
    )

    # Admin commits decision to approve
    await gate.submit_decision(app_id, "approved")

    # Client subsequently tries to cancel (e.g. disconnect right after approval)
    await gate.cancel_request(app_id)

    # Authorized admin decision MUST NOT be overwritten in DB history!
    row = await storage_db.get_approval_by_id(test_db, app_id)
    assert row["status"] == "approved"


# =========================================================================
# N04: Host & Origin Security & Unicode Authorization Headers
# =========================================================================

@pytest.mark.asyncio
async def test_n04_evil_host_header_rejected_with_400(spec_b_env):
    app, config, test_db = spec_b_env

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Attack with spoofed Host header
        resp = await client.get("/health", headers={"Host": "evil.attacker.com"})
        assert resp.status_code == 400
        assert "Invalid or untrusted Host" in resp.text

        # Even if Origin matches the evil Host, it MUST still be rejected!
        resp2 = await client.post(
            "/api/decision/fake-id",
            json={"decision": "approved"},
            headers={"Host": "evil.attacker.com", "Origin": "http://evil.attacker.com"},
        )
        assert resp2.status_code == 400


@pytest.mark.asyncio
async def test_n04_scheme_mismatch_cross_origin_blocked_with_403(spec_b_env):
    app, config, test_db = spec_b_env

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Client connects to http, but Origin claims https://127.0.0.1:8000
        resp = await client.post(
            "/api/decision/fake-id",
            json={"decision": "approved"},
            headers={
                "Host": "127.0.0.1:8000",
                "Origin": "https://127.0.0.1:8000",  # Different scheme!
                "Authorization": f"Bearer {config.server.gateway_api_key}",
            },
        )
        assert resp.status_code == 403
        assert "Origin policy" in resp.text


@pytest.mark.asyncio
async def test_n04_malformed_and_null_origin_handled_without_500(spec_b_env):
    app, config, test_db = spec_b_env

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. null Origin (sandboxed iframe)
        resp1 = await client.post(
            "/api/decision/fake-id",
            json={"decision": "approved"},
            headers={"Host": "127.0.0.1:8000", "Origin": "null"},
        )
        assert resp1.status_code in (400, 403)

        # 2. Malformed Origin
        resp2 = await client.post(
            "/api/decision/fake-id",
            json={"decision": "approved"},
            headers={"Host": "127.0.0.1:8000", "Origin": "http://invalid:port:bad/"},
        )
        assert resp2.status_code in (400, 403)


@pytest.mark.asyncio
async def test_n04_unicode_authorization_header_safe_401_no_500(spec_b_env):
    app, config, test_db = spec_b_env

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Header with unicode/emoji and non-ascii raw bytes
        unicode_bytes = "Bearer 🔑_secret_dữ_liệu_khóa_123".encode("utf-8")
        resp = await client.get("/api/stats", headers={"Authorization": unicode_bytes, "Host": "127.0.0.1:8000"})
        assert resp.status_code == 401
        assert "Unauthorized" in resp.text
