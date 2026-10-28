import asyncio
import os
import pytest
from src.config_loader import get_config
from src.security.sanitizer import (
    DLPSanitizer,
    StreamingRestorer,
    calculate_shannon_entropy,
    luhn_checksum_valid,
)
from src.security.egress_guard import EgressGuard, EgressBlockedError
from src.security.approval_gate import ApprovalGate
from src.router.balancer import ModelBalancer
from src.storage import db as storage_db


def test_shannon_entropy():
    # Repetitive string has low entropy
    low_ent = calculate_shannon_entropy("aaaaaaaaaaaaaaaaaaaa")
    assert low_ent == 0.0

    # High entropy random base64 secret string
    high_ent = calculate_shannon_entropy("dGhpcy1pcy1hLXJhbmRvbS1zZWNyZXQta2V5LTIwMjY=")
    assert high_ent > 3.5


def test_luhn_validator():
    # Valid test card (Visa test)
    assert luhn_checksum_valid("4532015112830366") is True
    # Invalid card
    assert luhn_checksum_valid("4532015112830367") is False


def test_dlp_sanitizer_regex_and_pseudonymization():
    sanitizer = DLPSanitizer(enable_entropy=True)

    text = (
        "Here is my secret AWS key: AKIAIOSFODNN7EXAMPLE and OpenAI: "
        "sk-abc1234567890abcdef1234567890abcdef. "
        "Contact me at admin@corp-internal.com or 192.168.1.100."
    )

    res = sanitizer.sanitize_text(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in res.text
    assert "sk-abc1234567890abcdef1234567890abcdef" not in res.text
    assert "admin@corp-internal.com" not in res.text
    assert "192.168.1.100" not in res.text

    assert "<REDACTED_AWS_KEY_" in res.text
    assert "<REDACTED_OPENAI_KEY_" in res.text
    assert "<REDACTED_EMAIL_" in res.text
    assert "<REDACTED_INTERNAL_IP_" in res.text
    assert res.max_risk_level == "HIGH"

    # Test reversible restoration
    restored = sanitizer.restore_text(res.text, res.reverse_map)
    assert restored == text


def test_streaming_restorer():
    reverse_map = {
        "<REDACTED_EMAIL_1>": "alice@company.com",
        "<REDACTED_KEY_1>": "sk-secret-token-123456",
    }
    restorer = StreamingRestorer(reverse_map)

    # Chunk 1 splits <REDACTED_EMAIL_1> in half: '<REDACTED_EM'
    c1 = restorer.feed("Hello <REDACTED_EM")
    assert c1 == "Hello "

    # Chunk 2 supplies the other half: 'AIL_1> how are you?'
    c2 = restorer.feed("AIL_1> how are you?")
    assert c2 == "alice@company.com how are you?"

    flushed = restorer.flush()
    assert flushed == ""


def test_egress_guard_validation():
    guard = EgressGuard(whitelist_domains=["openrouter.ai", "api.groq.com"])

    # Valid whitelisted domains
    guard.validate_url("https://openrouter.ai/api/v1/models")
    guard.validate_url("https://api.groq.com/openai/v1/chat/completions")

    # Non-whitelisted domain
    with pytest.raises(EgressBlockedError) as exc_info:
        guard.validate_url("https://evil-hacker.com/exfiltrate")
    assert "not in the egress whitelist" in str(exc_info.value)

    # SSRF: localhost / loopback
    with pytest.raises(EgressBlockedError):
        guard.validate_url("http://127.0.0.1:8080/admin")

    with pytest.raises(EgressBlockedError):
        guard.validate_url("http://192.168.1.1/router")


@pytest.mark.asyncio
async def test_storage_db_lifecycle():
    test_db = "storage/test_gateway.db"
    await storage_db.init_db(test_db)

    # Upsert model
    await storage_db.upsert_model(test_db, {
        "id": "groq/test-model",
        "provider": "groq",
        "model_name": "test-model",
        "display_name": "Test Model",
        "context_length": 64000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 120.0,
    })

    models = await storage_db.get_active_models(test_db)
    assert any(m["id"] == "groq/test-model" for m in models)

    # Cooldown check
    assert await storage_db.is_provider_in_cooldown(test_db, "groq") is False
    await storage_db.set_provider_cooldown(test_db, "groq", cooldown_seconds=10)
    assert await storage_db.is_provider_in_cooldown(test_db, "groq") is True

    # Quota states
    quotas = await storage_db.get_all_quota_states(test_db)
    groq_q = next(q for q in quotas if q["provider"] == "groq")
    assert groq_q["in_cooldown"] is True

    # DLP Log
    log_id = await storage_db.insert_dlp_log(test_db, {
        "client_ip": "127.0.0.1",
        "direction": "request",
        "risk_level": "HIGH",
        "findings": [{"rule_name": "API Key", "entity_type": "AWS_KEY"}],
        "redaction_count": 1,
        "action_taken": "BLOCKED",
        "prompt_snippet": "test prompt",
    })
    assert log_id > 0

    logs = await storage_db.get_dlp_logs(test_db)
    assert len(logs) >= 1
    assert logs[0]["risk_level"] == "HIGH"

    # Cleanup test db safely on Windows
    await asyncio.sleep(0.1)
    try:
        if os.path.exists(test_db):
            os.remove(test_db)
    except Exception:
        pass


@pytest.mark.asyncio
async def test_approval_gate_lifecycle():
    test_db = "storage/test_gate.db"
    await storage_db.init_db(test_db)
    gate = ApprovalGate(db_path=test_db)

    app_id = await gate.create_request(
        risk_level="MEDIUM",
        client_ip="127.0.0.1",
        model_requested="llama-3.3-70b-versatile",
        findings=[{"rule_name": "Internal IP"}],
        sanitized_messages=[{"role": "user", "content": "Ping <REDACTED_INTERNAL_IP_1>"}],
        mapping={"<REDACTED_INTERNAL_IP_1>": "192.168.1.50"},
        raw_payload={"model": "auto"},
        timeout_seconds=5,
    )

    # Submit approval asynchronously
    async def approve_later():
        await asyncio.sleep(0.5)
        await gate.submit_decision(app_id, "approved")

    asyncio.create_task(approve_later())
    decision, item = await gate.wait_for_decision(app_id, timeout_seconds=5)
    assert decision == "approved"

    # Cleanup safely on Windows
    await asyncio.sleep(0.1)
    try:
        if os.path.exists(test_db):
            os.remove(test_db)
    except Exception:
        pass
