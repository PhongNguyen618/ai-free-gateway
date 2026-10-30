import asyncio
import json
import time
import pytest
import httpx
from httpx import ASGITransport, AsyncClient

from src.config_loader import get_config
from src.main import create_app
from src.storage import db as storage_db


@pytest.fixture
async def hardening_env(tmp_path):
    test_db = str(tmp_path / "hardening_fixes.db")
    config = get_config().deep_copy()
    config.db_path = test_db
    config.server.gateway_api_key = ""
    config.server.record_pid = False
    config.server.max_concurrent_completions = 1
    config.server.total_completion_timeout_seconds = 2.0

    await storage_db.init_db(test_db)
    await storage_db.upsert_model(test_db, {
        "id": "groq/test-model",
        "provider": "groq",
        "model_name": "test-model",
        "display_name": "Groq Test Model",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 20.0,
    })
    app = create_app(config)
    yield app, config, test_db


# =========================================================================
# Fix 1: P1 — Concurrent Stream Admission Limit
# =========================================================================

@pytest.mark.asyncio
async def test_concurrent_stream_admission_slot_held_until_stream_ends(hardening_env, monkeypatch):
    """
    Verify that when max_concurrent_completions=1, the completion slot is HELD
    for the entire duration of the stream, rejecting second concurrent request with 429.
    """
    app, config, test_db = hardening_env
    egress = app.state.egress_guard

    stream_unblock_event = asyncio.Event()
    first_chunk_emitted = asyncio.Event()

    async def mock_stream_post(*args, **kwargs):
        first_chunk_emitted.set()
        yield b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        await stream_unblock_event.wait()
        yield b'data: [DONE]\n\n'

    monkeypatch.setattr(egress, "stream_post", mock_stream_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Task 1: Start Stream 1
        stream_resp = None
        async def run_stream():
            nonlocal stream_resp
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json={"model": "groq/test-model", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            ) as response:
                stream_resp = response
                async for chunk in response.aiter_bytes():
                    pass

        stream_task = asyncio.create_task(run_stream())

        # Wait until Stream 1 has emitted its first chunk and is actively streaming
        await asyncio.wait_for(first_chunk_emitted.wait(), timeout=2.0)

        # While Stream 1 is STILL running, Client 2 attempts completion -> Must get 429
        resp2 = await client.post(
            "/v1/chat/completions",
            json={"model": "groq/test-model", "messages": [{"role": "user", "content": "second request"}]},
        )
        assert resp2.status_code == 429, f"Expected 429 gateway overloaded, got {resp2.status_code}"
        assert resp2.json()["error"]["code"] == "gateway_overloaded"

        # Now unblock Stream 1 to complete
        stream_unblock_event.set()
        await asyncio.wait_for(stream_task, timeout=2.0)

        # After Stream 1 completes, slot is released -> Client 3 can now enter
        async def mock_post(*args, **kwargs):
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "done"}}]})
        monkeypatch.setattr(egress, "post", mock_post)

        resp3 = await client.post(
            "/v1/chat/completions",
            json={"model": "groq/test-model", "messages": [{"role": "user", "content": "third request"}]},
        )
        assert resp3.status_code == 200


# =========================================================================
# Fix 2: P1 — Total Completion Deadline Enforcement
# =========================================================================

@pytest.mark.asyncio
async def test_total_completion_deadline_enforced_against_slow_upstream(tmp_path, monkeypatch):
    """
    Verify that when total_completion_timeout_seconds=0.05 (50ms) and upstream takes 120ms,
    the gateway aborts with HTTP 504 Gateway Timeout instead of returning 200.
    """
    test_db = str(tmp_path / "deadline.db")
    config = get_config().deep_copy()
    config.db_path = test_db
    config.server.gateway_api_key = ""
    config.server.record_pid = False
    config.server.total_completion_timeout_seconds = 0.05  # 50 ms deadline

    await storage_db.init_db(test_db)
    await storage_db.upsert_model(test_db, {
        "id": "groq/slow-model",
        "provider": "groq",
        "model_name": "slow-model",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 10.0,
    })

    app = create_app(config)
    egress = app.state.egress_guard

    async def slow_post(*args, **kwargs):
        await asyncio.sleep(0.12)  # 120 ms > 50 ms deadline
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "late response"}}]})

    monkeypatch.setattr(egress, "post", slow_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "groq/slow-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        # Must NOT return 200! Must return 504 Gateway Timeout
        assert resp.status_code == 504, f"Expected 504, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["error"]["code"] == "gateway_timeout"


# =========================================================================
# Fix 3: P2 — Tool Arguments Invalid JSON Rejected
# =========================================================================

@pytest.mark.asyncio
async def test_invalid_upstream_tool_arguments_json_rejected_non_streaming(hardening_env, monkeypatch):
    """
    Verify that when upstream returns invalid JSON in tool arguments in non-streaming response,
    the gateway treats it as an error, does NOT return 200, and does NOT record success.
    """
    app, config, test_db = hardening_env
    egress = app.state.egress_guard

    # Upstream returns tool_calls with invalid JSON arguments "not-json"
    async def mock_post(*args, **kwargs):
        return httpx.Response(200, json={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "query_db", "arguments": "not-json"},
                    }],
                }
            }]
        })

    monkeypatch.setattr(egress, "post", mock_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "groq/test-model", "messages": [{"role": "user", "content": "run query"}]},
        )
        # Must fail candidate and return 502 Bad Gateway
        assert resp.status_code == 502
        # Verify success_count was NOT incremented
        m = await storage_db.get_model_by_id(test_db, "groq/test-model")
        assert m["success_count"] == 0


@pytest.mark.asyncio
async def test_invalid_upstream_tool_arguments_json_rejected_streaming(hardening_env, monkeypatch):
    """
    Verify that when upstream returns invalid JSON in tool arguments during streaming,
    the gateway terminates with an error event and does NOT record success in metrics.
    """
    app, config, test_db = hardening_env
    egress = app.state.egress_guard

    async def mock_stream_post(*args, **kwargs):
        chunk = {
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"arguments": "not-json"},
                    }]
                },
                "finish_reason": "tool_calls",
            }]
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr(egress, "stream_post", mock_stream_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "groq/test-model", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
        assert resp.status_code == 200  # Streaming connection started
        body_text = resp.text
        # Error event must be emitted
        assert "stream_error" in body_text or "Invalid upstream tool arguments JSON" in body_text
        # Metrics must NOT count as success
        m = await storage_db.get_model_by_id(test_db, "groq/test-model")
        assert m["success_count"] == 0


# =========================================================================
# Fix 4: P2 — Invalid Completion Response (Empty Dict) Rejected
# =========================================================================

@pytest.mark.asyncio
async def test_empty_or_malformed_upstream_json_not_treated_as_success(hardening_env, monkeypatch):
    """
    Verify that when upstream returns HTTP 200 with {} (empty json) or missing choices,
    the gateway rejects the completion, does not return 200 to client, and does not record success.
    """
    app, config, test_db = hardening_env
    egress = app.state.egress_guard

    async def mock_post(*args, **kwargs):
        return httpx.Response(200, json={})  # Upstream returns {}

    monkeypatch.setattr(egress, "post", mock_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "groq/test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        # Must NOT return 200 with {}! Must return 502 Bad Gateway
        assert resp.status_code == 502
        # Verify success_count was NOT incremented
        m = await storage_db.get_model_by_id(test_db, "groq/test-model")
        assert m["success_count"] == 0


# =========================================================================
# Fix 5: P2 — Readiness Reports Truthful Lifecycle State
# =========================================================================

@pytest.mark.asyncio
async def test_readiness_reports_not_ready_before_lifespan(tmp_path):
    """
    Verify that before lifespan runs, /ready reports 503 not_ready (initialized=False, catalog_usable=False).
    After lifespan runs with models, /ready reports 200 ready (initialized=True, catalog_usable=True).
    """
    test_db = str(tmp_path / "ready_lifecycle.db")
    config = get_config().deep_copy()
    config.db_path = test_db
    config.server.gateway_api_key = ""
    config.server.record_pid = False

    app = create_app(config)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Before lifespan: must report 503 not_ready
        resp_pre = await client.get("/ready")
        assert resp_pre.status_code == 503
        data_pre = resp_pre.json()
        assert data_pre["status"] == "not_ready"
        assert data_pre["initialized"] is False
        assert data_pre["catalog_usable"] is False

        # 2. Simulate lifespan startup (or active models in DB)
        await storage_db.init_db(test_db)
        await storage_db.upsert_model(test_db, {
            "id": "groq/ready-model",
            "provider": "groq",
            "model_name": "ready-model",
            "context_length": 128000,
            "is_free": True,
            "is_active": True,
            "latency_ms": 10.0,
        })
        app.state.initialized = True
        app.state.catalog_usable = True

        # 3. After lifespan startup: must report 200 ready
        resp_post = await client.get("/ready")
        assert resp_post.status_code == 200
        data_post = resp_post.json()
        assert data_post["status"] == "ready"
        assert data_post["initialized"] is True
        assert data_post["catalog_usable"] is True
