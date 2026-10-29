from __future__ import annotations

import asyncio
import json
import pytest
import httpx
from httpx import AsyncClient, ASGITransport

from src.config_loader import AppConfig, get_config, get_provider_api_key
from src.discovery.harvester import ModelHarvester
from src.router.balancer import ModelBalancer
from src.router.openai_proxy import create_openai_router
from src.security.approval_gate import ApprovalGate
from src.security.egress_guard import EgressGuard, EgressBlockedError
from src.security.sanitizer import DLPSanitizer, StreamingRestorer
from src.storage import db as storage_db
from src.main import app


# =========================================================================
# 1. TEST SANITIZER EDGE CASES & COMPLEX PATTERNS
# =========================================================================

def test_sanitizer_vietnamese_cccd_phone_and_urls():
    sanitizer = DLPSanitizer(enable_entropy=True)

    google_key = "AIzaSyD" + "A" * 32
    slack_token = "xox" + "b-1234567890-" + "1234567890123-" + "abcdefghijklmnopqrstuvwx"
    text = (
        f"My phone is 0912345678, CCCD is 012345678901, and db uri is "
        f"postgres://postgres:SuperSecretP@ssw0rd!@10.0.0.5:5432/production. "
        f"Also Google key {google_key} and Slack {slack_token}"
    )

    res = sanitizer.sanitize_text(text)
    assert "0912345678" not in res.text
    assert "012345678901" not in res.text
    assert "SuperSecretP@ssw0rd!" not in res.text
    assert "10.0.0.5" not in res.text
    assert google_key not in res.text
    assert slack_token not in res.text

    # Verify placeholder tokens created
    assert "<REDACTED_PHONE_" in res.text
    assert "<REDACTED_CCCD_" in res.text
    assert "<REDACTED_PASSWORD_URL_" in res.text
    assert "<REDACTED_INTERNAL_IP_" in res.text
    assert "<REDACTED_GOOGLE_KEY_" in res.text
    assert "<REDACTED_SLACK_TOKEN_" in res.text

    # Verify 100% accurate reversible restoration
    restored = sanitizer.restore_text(res.text, res.reverse_map)
    assert restored == text


def test_sanitizer_multipart_messages():
    from src.security.sanitizer import UnsupportedModalityError
    sanitizer = DLPSanitizer()

    # 1. Supported multi-part text messages
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Call me at alice@test.com"},
                {"type": "text", "text": "Another text part with IP 192.168.1.1"},
            ]
        }
    ]
    sanitized_msgs, _, findings, reverse_map, max_risk = sanitizer.sanitize_messages(messages)
    assert len(findings) == 2
    assert "<REDACTED_EMAIL_1>" in sanitized_msgs[0]["content"][0]["text"]
    assert "<REDACTED_INTERNAL_IP_1>" in sanitized_msgs[0]["content"][1]["text"]

    # 2. Strict rejection of unsupported binary modalities (image/audio) under Zero-Trust text DLP
    unsupported_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Look at this:"},
                {"type": "image_url", "image_url": {"url": "https://example.com/pic.png"}}
            ]
        }
    ]
    with pytest.raises(UnsupportedModalityError) as exc_info:
        sanitizer.sanitize_messages(unsupported_messages)
    assert "Unsupported modality 'image_url'" in str(exc_info.value)


def test_streaming_restorer_edge_cases():
    reverse_map = {
        "<REDACTED_KEY_1>": "sk-real-super-secret-key-123",
        "<REDACTED_IP_1>": "192.168.1.1",
    }
    restorer = StreamingRestorer(reverse_map)

    # Empty feed
    assert restorer.feed("") == ""

    # Normal text without placeholder
    assert restorer.feed("The answer is 42. ") == "The answer is 42. "

    # Feed split across multiple boundaries: '<', 'REDACTED_', 'KEY_', '1>'
    c1 = restorer.feed("Token: <")
    assert c1 == "Token: "
    c2 = restorer.feed("REDACTED_")
    assert c2 == ""
    c3 = restorer.feed("KEY_")
    assert c3 == ""
    c4 = restorer.feed("1> finished.")
    assert c4 == "sk-real-super-secret-key-123 finished."

    # Test flush with leftover buffer that has partial token
    restorer.feed("Normal trailing text <REDACTED_UNFINISHED")
    flushed = restorer.flush()
    assert "<REDACTED_UNFINISHED" in flushed


# =========================================================================
# 2. TEST EGRESS GUARD NETWORK RESTRICTIONS
# =========================================================================

@pytest.mark.asyncio
async def test_egress_guard_payload_limit_and_schemes():
    guard = EgressGuard(
        whitelist_domains=["openrouter.ai"],
        max_request_bytes=100,  # Tiny limit for testing
    )

    # Invalid scheme
    with pytest.raises(EgressBlockedError) as exc1:
        guard.validate_url("ftp://openrouter.ai/file")
    assert "Invalid URL scheme" in str(exc1.value)

    # Invalid empty hostname
    with pytest.raises(EgressBlockedError):
        guard.validate_url("https:///bad-url")

    # Exceed max payload limit
    with pytest.raises(EgressBlockedError) as exc2:
        await guard.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json_data={"data": "A" * 200}
        )
    assert "exceeds maximum allowed limit" in str(exc2.value)

    await guard.close()


# =========================================================================
# 3. TEST MODEL BALANCER & TASK CLASSIFICATION
# =========================================================================

@pytest.mark.asyncio
async def test_balancer_classification_and_scoring(tmp_path):
    config = get_config()
    test_db = str(tmp_path / "test_balancer.db")
    await storage_db.init_db(test_db)

    # Insert test models into DB
    await storage_db.upsert_model(test_db, {
        "id": "groq/deepseek-coder",
        "provider": "groq",
        "model_name": "deepseek-coder",
        "context_length": 64000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 100.0,
    })
    await storage_db.upsert_model(test_db, {
        "id": "openrouter/r1-reasoner",
        "provider": "openrouter",
        "model_name": "r1-reasoner",
        "context_length": 64000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 150.0,
    })

    # Balancer with custom test db
    test_config = config.model_copy()
    test_config.db_path = test_db
    balancer = ModelBalancer(test_config)

    # Test classifications
    assert balancer.classify_task("def calculate_sum(a, b): return a + b") == "code"
    assert balancer.classify_task("Please prove step by step using math logic") == "reasoning"
    assert balancer.classify_task("Hello! Tell me a nice joke.") == "general"

    # Test candidate chain scoring for code
    code_candidates = await balancer.get_candidate_chain("auto", prompt_text="write a python script")
    assert len(code_candidates) >= 1
    # Model with 'deepseek' / 'coder' should be top prioritized
    assert "coder" in code_candidates[0]["model_name"].lower() or "deepseek" in code_candidates[0]["model_name"].lower()

    # Test cooldown exclusion
    await storage_db.set_provider_cooldown(test_db, "groq", cooldown_seconds=60)
    filtered_candidates = await balancer.get_candidate_chain("auto", prompt_text="write code")
    # groq must NOT be in available candidates
    assert all(c["provider"] != "groq" for c in filtered_candidates)

    # Test request resolution
    target_url, headers, req_payload = balancer.resolve_provider_request(
        code_candidates[0], {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert "/chat/completions" in target_url
    assert req_payload["model"] == code_candidates[0]["model_name"]

    # Cleanup
    await asyncio.sleep(0.1)
    import os
    if os.path.exists(test_db):
        try:
            os.remove(test_db)
        except Exception:
            pass


# =========================================================================
# 4. TEST HARVESTER HARVESTING FLOW
# =========================================================================

@pytest.mark.asyncio
async def test_harvester_static_and_openrouter_mock(tmp_path):
    config = get_config()
    test_db = str(tmp_path / "test_harvester.db")
    await storage_db.init_db(test_db)

    test_config = config.model_copy()
    test_config.db_path = test_db

    guard = EgressGuard(whitelist_domains=["openrouter.ai"])
    harvester = ModelHarvester(config=test_config, egress_guard=guard)

    # Register static models (Groq, Gemini, GitHub)
    count = await harvester.register_static_free_models()
    assert count > 0

    active_models = await storage_db.get_active_models(test_db)
    assert len(active_models) >= count

    # Harvest all
    result = await harvester.harvest_all()
    assert result["status"] == "success"
    assert result["static_models_registered"] > 0

    await guard.close()

    # Cleanup
    await asyncio.sleep(0.1)
    import os
    if os.path.exists(test_db):
        try:
            os.remove(test_db)
        except Exception:
            pass


# =========================================================================
# 5. TEST END-TO-END CHAT PROXY (REVERSIBLE DECRYPTION & HITL FLOW)
# =========================================================================

@pytest.mark.asyncio
async def test_full_hitl_approval_and_rejection_flow():
    """
    Test that medium-risk prompts trigger HITL gate, and admin approval
    allows request to continue or reject properly.
    """
    app.state.config.dlp.risk_action.medium = "approve"
    app.state.config.dlp.approval_timeout_action = "reject"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Medium-risk prompt: contains internal IP and CCCD
        payload = {
            "model": "auto",
            "messages": [
                {"role": "user", "content": "Check internal server at 192.168.1.99 with id 012345678901"}
            ]
        }

        # Send request in background task (it will wait for HITL approval)
        async def make_request():
            return await client.post("/v1/chat/completions", json=payload)

        req_task = asyncio.create_task(make_request())

        # Wait briefly for gate request to be created in DB
        await asyncio.sleep(0.3)

        # 2. Admin polls /api/approvals/pending
        approvals_resp = await client.get("/api/approvals/pending")
        assert approvals_resp.status_code == 200
        pending_list = approvals_resp.json()
        assert len(pending_list) >= 1

        app_id = pending_list[0]["id"]
        assert pending_list[0]["risk_level"] == "MEDIUM"

        # 3. Admin rejects request
        decide_resp = await client.post(
            f"/api/approvals/{app_id}/decision",
            json={"decision": "rejected"}
        )
        assert decide_resp.status_code == 200
        assert decide_resp.json()["status"] == "success"

        # 4. Client request task finishes with 403 Forbidden
        resp = await req_task
        assert resp.status_code == 403
        data = resp.json()
        assert "rejected" in str(data).lower()


@pytest.mark.asyncio
async def test_api_quotas_and_logs_endpoints():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Check /api/quotas
        q_resp = await client.get("/api/quotas")
        assert q_resp.status_code == 200
        assert isinstance(q_resp.json(), list)

        # Check /api/dlp/logs
        l_resp = await client.get("/api/dlp/logs?limit=10")
        assert l_resp.status_code == 200
        logs = l_resp.json()
        assert isinstance(logs, list)

        # Check /api/harvester/trigger
        h_resp = await client.post("/api/harvester/trigger")
        assert h_resp.status_code == 200
        assert "status" in h_resp.json()


# =========================================================================
# 6. REGRESSION TESTS FOR PATCHES
# =========================================================================

def test_entropy_ignores_long_code_identifiers():
    sanitizer = DLPSanitizer(enable_entropy=True)
    code_prompt = "def AuthenticationMiddlewareConfiguration(user_authentication_service_provider):"
    res_code = sanitizer.sanitize_text(code_prompt)
    assert res_code.max_risk_level == "NONE"
    assert len(res_code.findings) == 0
    assert res_code.text == code_prompt

    # Real random secret token with mixed digits must be flagged
    secret = "x9Kp2Lm8Qv4Zn7Rt1Yw5Bc3Df6Gh"
    res_secret = sanitizer.sanitize_text(f"Token is {secret}")
    assert res_secret.max_risk_level == "HIGH"
    assert any(f.entity_type == "HIGH_ENTROPY" for f in res_secret.findings)
    assert secret not in res_secret.text


def test_custom_blocklist_redaction_and_restoration():
    blocklist = ["ProjectOmega", "scada-master-01"]
    sanitizer = DLPSanitizer(custom_blocklist=blocklist)
    text = "Deploying ProjectOmega on node scada-master-01 for maintenance."
    res = sanitizer.sanitize_text(text)
    assert "ProjectOmega" not in res.text
    assert "scada-master-01" not in res.text
    assert "<REDACTED_CUSTOM_BLOCK_" in res.text
    assert res.max_risk_level == "MEDIUM"

    # Test restoration
    restored = sanitizer.restore_text(res.text, res.reverse_map)
    assert restored == text


@pytest.mark.asyncio
async def test_streaming_429_failover(monkeypatch, tmp_path):
    """
    Verify that when stream=True and the first provider (groq) raises
    httpx.HTTPStatusError with status 429, the proxy records cooldown for groq
    and transparently streams the response from the second fallback provider
    (openrouter) with HTTP 200.
    """
    from fastapi import FastAPI
    config = get_config()
    test_db = str(tmp_path / "test_stream_429.db")
    await storage_db.init_db(test_db)

    await storage_db.upsert_model(test_db, {
        "id": "groq/llama-3.3-70b-versatile",
        "provider": "groq",
        "model_name": "llama-3.3-70b-versatile",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 40.0,
    })
    await storage_db.upsert_model(test_db, {
        "id": "openrouter/auto",
        "provider": "openrouter",
        "model_name": "auto",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 90.0,
    })

    test_config = config.model_copy()
    test_config.db_path = test_db
    test_config.dlp.risk_action.medium = "redact"

    egress = EgressGuard(whitelist_domains=test_config.egress.whitelist_domains)
    approval_gate = ApprovalGate(db_path=test_db)
    balancer = ModelBalancer(config=test_config)
    harvester = ModelHarvester(config=test_config, egress_guard=egress)

    attempted_streams = []

    async def mock_stream_post(url, headers=None, json_data=None):
        if "api.groq.com" in url:
            attempted_streams.append("groq")
            req = httpx.Request("POST", url)
            resp = httpx.Response(429, text="Rate limit exceeded", request=req)
            raise httpx.HTTPStatusError("Rate limit exceeded", request=req, response=resp)
        elif "openrouter.ai" in url:
            attempted_streams.append("openrouter")
            yield b'data: {"choices":[{"delta":{"content":"Fallback stream successful"}}]}\n\n'
            yield b'data: [DONE]\n\n'

    monkeypatch.setattr(egress, "stream_post", mock_stream_post)

    test_app = FastAPI()
    router = create_openai_router(test_config, egress, approval_gate, balancer, harvester)
    test_app.include_router(router)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "groq/llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": "Ping!"}],
                "stream": True,
            }
        )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        assert "Fallback stream successful" in resp.text
        assert attempted_streams == ["groq", "openrouter"]

        # Verify groq cooldown recorded in DB
        assert await storage_db.is_provider_in_cooldown(test_db, "groq") is True

    await egress.close()
    await asyncio.sleep(0.1)
    import os
    if os.path.exists(test_db):
        try:
            os.remove(test_db)
        except Exception:
            pass
