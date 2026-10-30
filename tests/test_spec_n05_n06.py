import asyncio
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
import pytest
import httpx
from httpx import ASGITransport, AsyncClient

from src.config_loader import get_config, AppConfig, get_provider_api_key
from src.lifecycle import InstanceLock, InstanceAlreadyRunningError
from src.main import create_app
from src.router.balancer import prune_messages_for_groq
from src.router.openai_proxy import _extract_cooldown_seconds
from src.storage import db as storage_db


@pytest.fixture
async def n05_n06_env(tmp_path):
    test_db = str(tmp_path / "n05_n06.db")
    config = get_config().deep_copy()
    config.db_path = test_db
    config.server.gateway_api_key = ""
    config.server.record_pid = False
    config.server.max_attempts = 3

    await storage_db.init_db(test_db)
    app = create_app(config)
    yield app, config, test_db


# =========================================================================
# N05: Instance Lifecycle & Lock Tests
# =========================================================================

def test_n05_instance_lock_prevents_second_instance_same_db(tmp_path):
    """Test that two locks on the same DB path conflict; releasing allows re-acquisition."""
    db_path = str(tmp_path / "test_lifecycle.db")
    lock1 = InstanceLock(db_path=db_path)
    lock2 = InstanceLock(db_path=db_path)

    # 1. First acquisition succeeds
    meta1 = lock1.acquire()
    assert meta1 is not None
    assert lock1.is_acquired is True

    # 2. Second acquisition on same DB fails with InstanceAlreadyRunningError
    with pytest.raises(InstanceAlreadyRunningError):
        lock2.acquire()

    # 3. Release first lock
    lock1.release()
    assert lock1.is_acquired is False

    # 4. Now second lock can acquire
    meta2 = lock2.acquire()
    assert meta2 is not None
    lock2.release()


def test_n05_instance_lock_different_db_allows_concurrent(tmp_path):
    """Test that two different DB paths can be locked concurrently without conflict."""
    db1 = str(tmp_path / "db1.db")
    db2 = str(tmp_path / "db2.db")

    lock1 = InstanceLock(db_path=db1)
    lock2 = InstanceLock(db_path=db2)

    meta1 = lock1.acquire()
    meta2 = lock2.acquire()
    assert meta1 is not None
    assert meta2 is not None

    lock1.release()
    lock2.release()


# =========================================================================
# N06: Retry Budget, Provider Capability & Error Boundaries Tests
# =========================================================================

def test_n06_retry_after_parsing_and_clamping(tmp_path):
    """Verify Retry-After header parsing for integer seconds and HTTP dates, clamped to 1..3600."""
    config = get_config().deep_copy()

    # Case 1: Simple integer seconds
    resp1 = httpx.Response(429, headers={"Retry-After": "45"})
    assert _extract_cooldown_seconds(resp1, config, "groq") == 45

    # Case 2: Negative/zero clamped to 1
    resp2 = httpx.Response(429, headers={"Retry-After": "0"})
    assert _extract_cooldown_seconds(resp2, config, "groq") == 1

    # Case 3: Oversized clamped to 3600
    resp3 = httpx.Response(429, headers={"Retry-After": "10000"})
    assert _extract_cooldown_seconds(resp3, config, "groq") == 3600

    # Case 4: HTTP date format (e.g. 60 seconds in the future)
    future_time = datetime.now(timezone.utc) + timedelta(seconds=60)
    http_date_str = format_datetime(future_time, usegmt=True)
    resp4 = httpx.Response(429, headers={"Retry-After": http_date_str})
    parsed_sec = _extract_cooldown_seconds(resp4, config, "groq")
    assert 55 <= parsed_sec <= 65

    # Case 5: Missing header falls back to provider default
    resp5 = httpx.Response(429)
    assert _extract_cooldown_seconds(resp5, config, "groq") == config.providers.groq.cooldown_seconds_on_429


@pytest.mark.asyncio
async def test_n06_missing_provider_api_key_skips_upfront(n05_n06_env, monkeypatch):
    """Verify that models on providers without configured API keys are skipped upfront without network calls."""
    app, config, test_db = n05_n06_env
    egress = app.state.egress_guard

    # Add a model on github (which has no GITHUB_TOKEN configured in env)
    await storage_db.upsert_model(test_db, {
        "id": "github/gpt-4o",
        "provider": "github",
        "model_name": "gpt-4o",
        "display_name": "GitHub GPT-4o",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 50.0,
    })

    # Ensure GITHUB_TOKEN is explicitly empty in environment
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    called_urls = []
    async def mock_post(url, headers=None, json_data=None, content=None):
        called_urls.append(url)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    monkeypatch.setattr(egress, "post", mock_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "github/gpt-4o",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        # Because github has no key, it is skipped upfront, leading to 502 Bad Gateway
        assert resp.status_code == 502
        assert len(called_urls) == 0, "No network request should have been sent to provider without API key"


@pytest.mark.asyncio
async def test_n06_upstream_401_403_skips_all_models_on_that_provider(n05_n06_env, monkeypatch):
    """Verify that an HTTP 401/403 auth error skips all subsequent models on that provider in the request."""
    app, config, test_db = n05_n06_env
    egress = app.state.egress_guard

    # Provider 1: groq has 2 models
    await storage_db.upsert_model(test_db, {
        "id": "groq/model-1",
        "provider": "groq",
        "model_name": "model-1",
        "display_name": "Groq Model 1",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 10.0,
    })
    await storage_db.upsert_model(test_db, {
        "id": "groq/model-2",
        "provider": "groq",
        "model_name": "model-2",
        "display_name": "Groq Model 2",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 20.0,
    })

    # Provider 2: openrouter has 1 model
    await storage_db.upsert_model(test_db, {
        "id": "openrouter/free-model",
        "provider": "openrouter",
        "model_name": "free-model",
        "display_name": "OpenRouter Model",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 30.0,
    })

    attempted_models = []
    async def mock_post(url, headers=None, json_data=None, content=None):
        m = json_data.get("model")
        attempted_models.append(m)
        if "groq.com" in url:
            # First groq model returns 401 unauthorized
            return httpx.Response(401, json={"error": "Invalid API key"})
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "Success from OpenRouter"}}]
        })

    monkeypatch.setattr(egress, "post", mock_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
        # groq/model-1 failed 401 -> groq/model-2 must be skipped immediately -> fallback to openrouter
        assert "model-1" in attempted_models
        assert "model-2" not in attempted_models
        assert "free-model" in attempted_models


@pytest.mark.asyncio
async def test_n06_upstream_400_422_halts_immediately_without_blind_retries(n05_n06_env, monkeypatch):
    """Verify that upstream 400/422 (invalid schema or client params) halts immediately with 400 Bad Request."""
    app, config, test_db = n05_n06_env
    egress = app.state.egress_guard

    # Add 2 models
    await storage_db.upsert_model(test_db, {
        "id": "groq/primary",
        "provider": "groq",
        "model_name": "primary",
        "display_name": "Groq Primary",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 10.0,
    })
    await storage_db.upsert_model(test_db, {
        "id": "openrouter/secondary",
        "provider": "openrouter",
        "model_name": "secondary",
        "display_name": "OpenRouter Secondary",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 20.0,
    })

    call_count = 0
    async def mock_post(url, headers=None, json_data=None, content=None):
        nonlocal call_count
        call_count += 1
        return httpx.Response(400, json={"error": "Unsupported tool schema parameter"})

    monkeypatch.setattr(egress, "post", mock_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 502
        assert call_count == 1, "Must halt after first 400 without retrying fallback models blindly"
        err_json = resp.json()
        assert err_json["error"]["type"] == "bad_gateway"
        # Does not leak raw upstream error body
        assert "Unsupported tool schema parameter" not in str(err_json)


@pytest.mark.asyncio
async def test_n06_max_attempts_budget_stops_fallback(tmp_path, monkeypatch):
    """Verify that fallback stops after max_attempts (e.g. 2), not looping indefinitely through all models."""
    test_db = str(tmp_path / "n06_budget.db")
    config = get_config().deep_copy()
    config.db_path = test_db
    config.server.gateway_api_key = ""
    config.server.record_pid = False
    config.server.max_attempts = 2  # Set budget to 2

    await storage_db.init_db(test_db)
    app = create_app(config)
    egress = app.state.egress_guard

    # Add 4 models on OpenRouter
    for i in range(1, 5):
        await storage_db.upsert_model(test_db, {
            "id": f"openrouter/model-{i}",
            "provider": "openrouter",
            "model_name": f"model-{i}",
            "display_name": f"OpenRouter Model {i}",
            "context_length": 128000,
            "is_free": True,
            "is_active": True,
            "latency_ms": 10.0 * i,
        })

    call_count = 0
    async def mock_post(url, headers=None, json_data=None, content=None):
        nonlocal call_count
        call_count += 1
        return httpx.Response(500, json={"error": "Internal server error"})

    monkeypatch.setattr(egress, "post", mock_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 502
        assert call_count == 2, f"Expected exactly 2 attempts bounded by max_attempts, got {call_count}"


def test_n06_prune_messages_for_groq_preserves_tool_pairs():
    """Verify that prune_messages_for_groq treats tool calls and their tool outputs as atomic blocks."""
    # Build conversation where older messages must be pruned, but an assistant with tool_calls
    # and 2 tool responses are in history.
    huge_text = "x" * 2000

    conversation = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Turn 1: " + huge_text},
        {"role": "assistant", "content": "Turn 1 response: " + huge_text},
        {"role": "user", "content": "Turn 2: Run tools"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "f1", "arguments": "{}"}},
                {"id": "call_2", "type": "function", "function": {"name": "f2", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "Result 1: " + ("a" * 100)},
        {"role": "tool", "tool_call_id": "call_2", "content": "Result 2: " + ("b" * 100)},
        {"role": "assistant", "content": "All tools finished."},
        {"role": "user", "content": "Latest query: what is the weather?"},
    ]

    # Prune with budget that forces dropping Turn 1
    pruned = prune_messages_for_groq(conversation, max_chars=4000)

    # 1. System prompt is kept
    assert pruned[0]["role"] == "system"
    # 2. Latest query is kept
    assert pruned[-1]["content"] == "Latest query: what is the weather?"

    # 3. Find if assistant with tool_calls is in pruned
    assistant_tool_idx = None
    for idx, m in enumerate(pruned):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            assistant_tool_idx = idx
            break

    if assistant_tool_idx is not None:
        # If assistant with tool_calls is kept, ALL its matching tool responses must also be immediately present
        tool_msgs = [m for m in pruned if m.get("role") == "tool"]
        tool_call_ids = {t["tool_call_id"] for t in tool_msgs}
        assert "call_1" in tool_call_ids
        assert "call_2" in tool_call_ids
    else:
        # If dropped, NO orphan tool messages should exist
        tool_msgs = [m for m in pruned if m.get("role") == "tool"]
        assert len(tool_msgs) == 0, "Orphan tool messages without preceding assistant tool_calls are strictly prohibited"
