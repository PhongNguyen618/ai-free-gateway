from __future__ import annotations

import asyncio
import codecs
import json
import math
import os
import secrets
from pathlib import Path
import pytest
import httpx
from httpx import AsyncClient, ASGITransport

from src.config_loader import AppConfig, get_config, load_yaml_config
from src.discovery.harvester import ModelHarvester, is_strictly_zero_price
from src.main import create_app
from src.router.balancer import ModelBalancer
from src.router.openai_proxy import create_openai_router, make_openai_error
from src.security.approval_gate import ApprovalGate
from src.security.egress_guard import EgressGuard, EgressBlockedError
from src.security.sanitizer import DLPSanitizer, StreamingRestorer, UnsupportedModalityError
from src.storage import db as storage_db


# =========================================================================
# F10 & F01: Test Isolation & Shared Auth & CORS & External Bind
# =========================================================================

@pytest.fixture
async def isolated_env(tmp_path):
    test_db = str(tmp_path / "gateway_test_isolated.db")
    config = get_config()
    config.db_path = test_db
    config.server.gateway_api_key = "secret_master_key_123"
    config.server.cors_allowed_origins = ["https://trusted-dashboard.local"]

    await storage_db.init_db(test_db)
    await storage_db.upsert_model(test_db, {
        "id": "groq/llama-3.3-70b-versatile",
        "provider": "groq",
        "model_name": "llama-3.3-70b-versatile",
        "display_name": "Llama 3.3 70B",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 50.0,
    })

    app = create_app(config)
    yield app, config, test_db


@pytest.mark.asyncio
async def test_f01_auth_protects_dashboard_api_and_proxy(isolated_env):
    app, config, test_db = isolated_env
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Health is open without leaking internals
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

        # 2. Missing or wrong key blocks dashboard API endpoints with 401
        for ep in ["/api/stats", "/api/models", "/api/approvals/pending", "/api/dlp/logs", "/api/quotas", "/api/routing/status"]:
            r = await client.get(ep)
            assert r.status_code == 401, f"{ep} did not require auth"
            r_wrong = await client.get(ep, headers={"Authorization": "Bearer wrong_key"})
            assert r_wrong.status_code == 401

        # 3. Decision & harvest POST endpoints require auth
        r_post = await client.post("/api/approvals/fake-id/decision", json={"decision": "approved"})
        assert r_post.status_code == 401
        r_harv = await client.post("/api/harvester/trigger")
        assert r_harv.status_code == 401

        # 4. Proxy endpoints require auth
        r_v1 = await client.get("/v1/models")
        assert r_v1.status_code == 401

        # 5. Correct Authorization Bearer key grants access
        auth_header = {"Authorization": "Bearer secret_master_key_123"}
        r_auth = await client.get("/api/stats", headers=auth_header)
        assert r_auth.status_code == 200

        # 6. Correct X-Gateway-Key header grants access
        r_xkey = await client.get("/api/stats", headers={"X-Gateway-Key": "secret_master_key_123"})
        assert r_xkey.status_code == 200


@pytest.mark.asyncio
async def test_f01_cors_and_csrf_origin_protection(isolated_env):
    app, config, _ = isolated_env
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Untrusted Origin attempting state change is blocked with 403
        resp = await client.post(
            "/api/approvals/fake-id/decision",
            headers={
                "Authorization": "Bearer secret_master_key_123",
                "Origin": "https://malicious-website.com",
            },
            json={"decision": "approved"},
        )
        assert resp.status_code == 403
        assert "Cross-site state modification blocked" in resp.json()["detail"]

        # Whitelisted Origin is permitted
        resp_trusted = await client.post(
            "/api/approvals/fake-id/decision",
            headers={
                "Authorization": "Bearer secret_master_key_123",
                "Origin": "https://trusted-dashboard.local",
            },
            json={"decision": "approved"},
        )
        # Should pass CORS check and hit domain logic (404 for non-existent approval ID)
        assert resp_trusted.status_code == 404


def test_f01_external_bind_security_check(tmp_path):
    cfg = get_config()
    cfg.db_path = str(tmp_path / "test_bind.db")
    cfg.server.host = "0.0.0.0"
    cfg.server.gateway_api_key = ""
    cfg.server.allow_insecure_external = False

    with pytest.raises(RuntimeError) as exc_info:
        create_app(cfg)
    assert "Binding gateway to external non-loopback host" in str(exc_info.value)

    # Opt-in permits binding
    cfg.server.allow_insecure_external = True
    app = create_app(cfg)
    assert app is not None


# =========================================================================
# F02: Stored XSS Prevention in Dashboard
# =========================================================================

def test_f02_no_inner_html_injection_in_dashboard_ui():
    ui_path = Path("ui/index.html")
    assert ui_path.exists(), "ui/index.html must exist"
    content = ui_path.read_text(encoding="utf-8")

    # Verify no CDN script inclusion (F11 offline requirement)
    assert "cdn.tailwindcss.com" not in content
    # Verify no innerHTML template interpolation sinks with dynamic values
    assert ".innerHTML = `" not in content
    assert "innerHTML =" not in content


# =========================================================================
# F03: DLP Across All Channels (Content, Tools, Schema, Modality)
# =========================================================================

def test_f03_dlp_scans_tool_calls_json_arguments_and_function_call():
    sanitizer = DLPSanitizer()
    fake_aws_key = "AKIAIOSFODNN7EXAMPLE"

    messages = [
        {
            "role": "assistant",
            "content": "Calling tool...",
            "tool_calls": [
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {
                        "name": "configure_aws",
                        "arguments": json.dumps({"access_key": fake_aws_key, "region": "us-east-1"}),
                    },
                }
            ],
            "function_call": {
                "name": "legacy_call",
                "arguments": json.dumps({"token": fake_aws_key}),
            },
        }
    ]

    sanitized, _, findings, reverse_map, max_risk = sanitizer.sanitize_messages(messages)
    assert max_risk == "HIGH"
    assert len(findings) >= 2
    assert any(f.matched_text == fake_aws_key for f in findings)

    # Check arguments JSON remains valid
    args_json = json.loads(sanitized[0]["tool_calls"][0]["function"]["arguments"])
    assert fake_aws_key not in args_json["access_key"]
    assert "<REDACTED_AWS_KEY_" in args_json["access_key"]
    assert args_json["region"] == "us-east-1"


def test_f03_dlp_scans_tools_descriptions_and_schema():
    sanitizer = DLPSanitizer()
    fake_key = "AKIAIOSFODNN7EXAMPLE"
    tools = [
        {
            "type": "function",
            "function": {
                "name": "send_secret",
                "description": f"Function using key {fake_key}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "api_key": {
                            "type": "string",
                            "description": f"Provide secret {fake_key}",
                        }
                    }
                }
            }
        }
    ]

    _, sanitized_tools, findings, _, max_risk = sanitizer.sanitize_messages(messages=[], tools=tools)
    assert max_risk == "HIGH"
    assert len(findings) == 2
    assert fake_key not in sanitized_tools[0]["function"]["description"]
    assert fake_key not in sanitized_tools[0]["function"]["parameters"]["properties"]["api_key"]["description"]


def test_f03_unsupported_binary_modality_strictly_rejected():
    sanitizer = DLPSanitizer()
    messages_with_audio = [
        {
            "role": "user",
            "content": [
                {"type": "input_audio", "input_audio": {"data": "base64audio...", "format": "mp3"}}
            ]
        }
    ]
    with pytest.raises(UnsupportedModalityError):
        sanitizer.sanitize_messages(messages_with_audio)


# =========================================================================
# F04: Egress Policy, DNS, Rebinding & Sizing
# =========================================================================

@pytest.mark.asyncio
async def test_f04_oversized_json_and_stream_blocked_before_network():
    guard = EgressGuard(whitelist_domains=["openrouter.ai"], max_request_bytes=50)

    # JSON post oversized
    with pytest.raises(EgressBlockedError) as exc_p:
        await guard.post("https://openrouter.ai/api/v1/chat/completions", json_data={"content": "A" * 60})
    assert "exceeds maximum allowed limit" in str(exc_p.value)

    # Stream post oversized
    with pytest.raises(EgressBlockedError) as exc_s:
        gen = guard.stream_post("https://openrouter.ai/api/v1/chat/completions", json_data={"content": "A" * 60})
        await gen.__anext__()
    assert "exceeds maximum allowed limit" in str(exc_s.value)

    await guard.close()


@pytest.mark.asyncio
async def test_f04_dns_private_ip_resolution_blocked():
    # Mock DNS resolver returning private IP for an allowed domain
    async def mock_resolver(host, port):
        return [
            (2, 1, 6, "", ("127.0.0.1", port)),
        ]

    guard = EgressGuard(
        whitelist_domains=["openrouter.ai"],
        dns_resolver=mock_resolver,
    )
    with pytest.raises(EgressBlockedError) as exc:
        await guard.verify_dns("openrouter.ai", 443)
    assert "resolves to private/non-public IP" in str(exc.value)

    await guard.close()


def test_f04_url_validation_subdomain_and_userinfo():
    guard = EgressGuard(whitelist_domains=["openrouter.ai", "*.groq.com"], allow_http=False)

    # Userinfo blocked
    with pytest.raises(EgressBlockedError):
        guard.validate_url("https://user:password@openrouter.ai/api")

    # HTTP blocked
    with pytest.raises(EgressBlockedError):
        guard.validate_url("http://openrouter.ai/api")

    # Exact domain matches
    h1, _, _ = guard.validate_url("https://openrouter.ai/api")
    assert h1 == "openrouter.ai"

    # Wildcard allows subdomain
    h2, _, _ = guard.validate_url("https://api.groq.com/openai")
    assert h2 == "api.groq.com"

    # Exact domain does NOT allow unwhitelisted subdomains
    with pytest.raises(EgressBlockedError):
        guard.validate_url("https://sub.openrouter.ai/api")


# =========================================================================
# F05: Verified Free Pricing Matrix & Reconciliation
# =========================================================================

def test_f05_is_strictly_zero_price_matrix():
    # Valid 0 prices
    assert is_strictly_zero_price(0) is True
    assert is_strictly_zero_price(0.0) is True
    assert is_strictly_zero_price("0") is True
    assert is_strictly_zero_price("0.0") is True
    assert is_strictly_zero_price(" 0 ") is True

    # Invalid / non-free prices
    assert is_strictly_zero_price(None) is False
    assert is_strictly_zero_price("") is False
    assert is_strictly_zero_price(-1) is False
    assert is_strictly_zero_price(0.00001) is False
    assert is_strictly_zero_price(float("nan")) is False
    assert is_strictly_zero_price(float("inf")) is False
    assert is_strictly_zero_price("free") is False
    assert is_strictly_zero_price("abc") is False


@pytest.mark.asyncio
async def test_f05_harvest_openrouter_strict_pricing_reconciliation(monkeypatch, tmp_path):
    test_db = str(tmp_path / "test_f05.db")
    await storage_db.init_db(test_db)

    config = get_config()
    config.db_path = test_db
    egress = EgressGuard(whitelist_domains=["openrouter.ai"])
    harvester = ModelHarvester(config=config, egress_guard=egress)

    # Seed an old model that should be pruned after reconciliation
    await storage_db.upsert_model(test_db, {
        "id": "openrouter/old-paid-model",
        "provider": "openrouter",
        "model_name": "old-paid-model",
        "is_free": True,
        "is_active": True,
    })

    # Mock OpenRouter payload containing:
    # 1. Truly free model (pricing 0/0)
    # 2. Fake free model (:free suffix but pricing > 0) -> must be pruned!
    # 3. Corrupted record -> should not crash harvest
    mock_payload = {
        "data": [
            {
                "id": "meta-llama/llama-3-8b:free",
                "pricing": {"prompt": "0", "completion": "0"},
                "context_length": 8192,
            },
            {
                "id": "sneaky/fake-free:free",
                "pricing": {"prompt": "0.001", "completion": "0.002"},
                "context_length": 8192,
            },
            {"corrupted": True},
        ]
    }

    async def mock_get(url, headers=None):
        r = httpx.Response(200, json=mock_payload)
        r.latency_ms = 40.0
        return r

    monkeypatch.setattr(egress, "get", mock_get)

    count = await harvester.harvest_openrouter()
    assert count == 1  # Only truly 0 price model harvested

    # Verify active models in DB
    active = await storage_db.get_active_models(test_db, provider="openrouter")
    active_ids = {m["id"] for m in active}
    assert "openrouter/meta-llama/llama-3-8b:free" in active_ids
    assert "openrouter/sneaky/fake-free:free" not in active_ids
    assert "openrouter/old-paid-model" not in active_ids  # Pruned by reconciliation

    await egress.close()


@pytest.mark.asyncio
async def test_f05_empty_db_returns_503_zero_outbound_calls(tmp_path, monkeypatch):
    test_db = str(tmp_path / "test_empty.db")
    await storage_db.init_db(test_db)
    config = get_config()
    config.db_path = test_db

    app = create_app(config)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert resp.status_code == 503
        data = resp.json()
        assert "service_unavailable" in str(data)


# =========================================================================
# F06: Alias Dedup & Immediate 429 Provider Skip
# =========================================================================

@pytest.mark.asyncio
async def test_f06_candidate_chain_deduplicates_aliases(tmp_path):
    test_db = str(tmp_path / "test_dedup.db")
    await storage_db.init_db(test_db)
    config = get_config()
    config.db_path = test_db

    # Insert both namespaced ID and alias for the same provider+model
    await storage_db.upsert_model(test_db, {
        "id": "groq/llama-3.3-70b-versatile",
        "provider": "groq",
        "model_name": "llama-3.3-70b-versatile",
        "is_free": True,
        "is_active": True,
    })
    await storage_db.upsert_model(test_db, {
        "id": "llama-3.3-70b-versatile",
        "provider": "groq",
        "model_name": "llama-3.3-70b-versatile",
        "is_free": True,
        "is_active": True,
    })

    balancer = ModelBalancer(config=config)
    chain = await balancer.get_candidate_chain("auto")
    # Should be deduplicated: exactly 1 candidate
    assert len(chain) == 1
    assert chain[0]["model_name"] == "llama-3.3-70b-versatile"


@pytest.mark.asyncio
async def test_f06_immediate_429_provider_skip_in_single_request(monkeypatch, tmp_path):
    test_db = str(tmp_path / "test_skip429.db")
    await storage_db.init_db(test_db)
    config = get_config()
    config.db_path = test_db

    # Insert two models for groq, and one for openrouter
    await storage_db.upsert_model(test_db, {
        "id": "groq/model-1",
        "provider": "groq",
        "model_name": "model-1",
        "is_free": True,
        "is_active": True,
    })
    await storage_db.upsert_model(test_db, {
        "id": "groq/model-2",
        "provider": "groq",
        "model_name": "model-2",
        "is_free": True,
        "is_active": True,
    })
    await storage_db.upsert_model(test_db, {
        "id": "openrouter/model-3",
        "provider": "openrouter",
        "model_name": "model-3",
        "is_free": True,
        "is_active": True,
    })

    app = create_app(config)
    egress = app.state.egress_guard

    calls = []

    async def mock_post(url, headers=None, json_data=None, content=None):
        if "groq.com" in url:
            calls.append(url)
            # First groq call returns 429
            r = httpx.Response(429, headers={"retry-after": "60"}, json={"error": "rate limit"})
            r.latency_ms = 10.0
            return r
        elif "openrouter.ai" in url:
            calls.append(url)
            r = httpx.Response(200, json={
                "id": "cmpl-success",
                "choices": [{"message": {"role": "assistant", "content": "Resolved via OpenRouter!"}}],
                "usage": {"total_tokens": 15},
            })
            r.latency_ms = 20.0
            return r

    monkeypatch.setattr(egress, "post", mock_post)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [{"role": "user", "content": "Hi"}]},
        )
        assert resp.status_code == 200
        # Groq received only 1 call (not 2!), then immediately skipped to OpenRouter
        assert len([c for c in calls if "groq.com" in c]) == 1
        assert len([c for c in calls if "openrouter.ai" in c]) == 1


# =========================================================================
# F07: SSE Streaming Unicode Integrity & Multi-Choice Separation
# =========================================================================

def test_f07_streaming_restorer_incremental_utf8_vietnamese_and_emojis():
    # Byte-by-byte feed of multi-byte UTF-8 Vietnamese & Emoji
    text = "Xin chào Việt Nam! 🚀✨ <REDACTED_SECRET_1> test."
    reverse_map = {"<REDACTED_SECRET_1>": "SUPER_SECRET_KEY"}
    restorer = StreamingRestorer(reverse_map)

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    raw_bytes = text.encode("utf-8")

    out_chunks = []
    # Feed 1 byte at a time
    for b in raw_bytes:
        chunk_str = decoder.decode(bytes([b]), final=False)
        if chunk_str:
            out_chunks.append(restorer.feed(chunk_str))

    final_str = decoder.decode(b"", final=True)
    if final_str:
        out_chunks.append(restorer.feed(final_str))
    out_chunks.append(restorer.flush())

    result = "".join(out_chunks)
    assert result == "Xin chào Việt Nam! 🚀✨ SUPER_SECRET_KEY test."


# =========================================================================
# F08: Pydantic Validation & OpenAI Error Schemas
# =========================================================================

@pytest.mark.asyncio
async def test_f08_invalid_input_schemas_return_400_openai_format(isolated_env):
    app, _, _ = isolated_env
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Non-dict body
        r1 = await client.post("/v1/chat/completions", content=b"[]", headers={"Content-Type": "application/json", "Authorization": "Bearer secret_master_key_123"})
        assert r1.status_code == 400
        assert r1.json()["error"]["type"] == "invalid_request_error"

        # 2. Empty messages list
        r2 = await client.post("/v1/chat/completions", json={"model": "auto", "messages": []}, headers={"Authorization": "Bearer secret_master_key_123"})
        assert r2.status_code == 400
        assert r2.json()["error"]["type"] == "invalid_request_error"

        # 3. Invalid stream boolean string
        r3 = await client.post("/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}], "stream": "invalid_bool"}, headers={"Authorization": "Bearer secret_master_key_123"})
        assert r3.status_code == 400
        assert r3.json()["error"]["type"] == "invalid_request_error"


# =========================================================================
# F09: Approval Gate Atomic State Transitions
# =========================================================================

@pytest.mark.asyncio
async def test_f09_concurrent_decision_atomic_once(tmp_path):
    test_db = str(tmp_path / "test_f09.db")
    await storage_db.init_db(test_db)
    gate = ApprovalGate(db_path=test_db)

    app_id = await gate.create_request(
        risk_level="HIGH",
        client_ip="127.0.0.1",
        model_requested="auto",
        findings=[],
        sanitized_messages=[],
        timeout_seconds=10,
    )

    # Fire two concurrent submit decisions
    res1, res2 = await asyncio.gather(
        gate.submit_decision(app_id, "approved"),
        gate.submit_decision(app_id, "rejected"),
    )

    # Exactly one must succeed!
    assert (res1 is True and res2 is False) or (res1 is False and res2 is True)

    # Third decision must fail
    res3 = await gate.submit_decision(app_id, "approved")
    assert res3 is False


@pytest.mark.asyncio
async def test_f09_expired_approval_cannot_be_decided(tmp_path):
    test_db = str(tmp_path / "test_f09_expired.db")
    await storage_db.init_db(test_db)
    gate = ApprovalGate(db_path=test_db)

    app_id = await gate.create_request(
        risk_level="HIGH",
        client_ip="127.0.0.1",
        model_requested="auto",
        findings=[],
        sanitized_messages=[],
        timeout_seconds=0,  # Expires immediately
    )
    await asyncio.sleep(0.01)

    # Attempt decision after expiry
    res = await gate.submit_decision(app_id, "approved")
    assert res is False


# =========================================================================
# F11: Quota Reset, Stop Script & Config Path
# =========================================================================

@pytest.mark.asyncio
async def test_f11_atomic_daily_quota_reset(tmp_path):
    test_db = str(tmp_path / "test_quota_reset.db")
    await storage_db.init_db(test_db)

    # Manually seed an older date usage
    async with storage_db.get_db(test_db) as conn:
        await conn.execute("""
            INSERT INTO quota_state (provider, requests_today, tokens_today, usage_date, updated_at)
            VALUES ('groq', 50, 10000, '2020-01-01', '2020-01-01T00:00:00Z')
        """)
        await conn.commit()

    # Increment usage today
    await storage_db.increment_provider_usage(test_db, "groq", tokens=500)

    # Verify atomic reset happened
    quotas = await storage_db.get_all_quota_states(test_db)
    g = next(q for q in quotas if q["provider"] == "groq")
    assert g["requests_today"] == 1
    assert g["tokens_today"] == 500


def test_f11_invalid_config_path_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        get_config("non_existent_config_file_12345.yaml")

