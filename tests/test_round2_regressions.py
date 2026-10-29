from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional
import pytest
import httpx
from httpx import AsyncClient, ASGITransport

from src.config_loader import AppConfig, PROJECT_ROOT, get_config
from src.main import create_app
from src.security.egress_guard import EgressGuard, EgressBlockedError, PinnedNetworkBackend
from src.security.sanitizer import DLPSanitizer, StreamingRestorer, UnsupportedModalityError
from src.storage import db as storage_db


@pytest.fixture
async def r2_app_env(tmp_path):
    test_db = str(tmp_path / "r2_test.db")
    config = get_config().deep_copy()
    config.db_path = test_db
    config.server.gateway_api_key = ""
    config.server.record_pid = False

    await storage_db.init_db(test_db)
    await storage_db.upsert_model(test_db, {
        "id": "groq/test-model",
        "provider": "groq",
        "model_name": "test-model",
        "display_name": "Groq Test Model",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 30.0,
    })

    app = create_app(config)
    yield app, config, test_db


# =========================================================================
# R2-01: Unknown parameter / legacy functions DLP bypass rejection
# =========================================================================

@pytest.mark.asyncio
async def test_r2_01_unknown_fields_and_legacy_functions_rejected(r2_app_env, monkeypatch):
    app, config, test_db = r2_app_env
    egress = app.state.egress_guard

    upstream_called = False

    async def mock_post(url, headers=None, json_data=None, content=None):
        nonlocal upstream_called
        upstream_called = True
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]})

    monkeypatch.setattr(egress, "post", mock_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Case 1: Legacy functions containing secret should be rejected with 400 (unsupported) or 403 (dlp)
        payload_functions = {
            "model": "groq/test-model",
            "messages": [{"role": "user", "content": "hello"}],
            "functions": [{"name": "lookup", "description": "AKIAIOSFODNN7EXAMPLE"}],
        }
        resp1 = await client.post("/v1/chat/completions", json=payload_functions)
        assert resp1.status_code in (400, 403)
        assert not upstream_called, "Upstream should NEVER be called when unknown/unsupported parameter is passed"

        # Case 2: Unknown top-level field should be rejected with 400
        payload_unknown = {
            "model": "groq/test-model",
            "messages": [{"role": "user", "content": "hello"}],
            "extra_unsupported_param": "some_value",
        }
        resp2 = await client.post("/v1/chat/completions", json=payload_unknown)
        assert resp2.status_code == 400
        err = resp2.json()["error"]
        assert err["type"] == "invalid_request_error"
        assert "extra_unsupported_param" in err["message"] or "unsupported" in err["message"].lower()


# =========================================================================
# R2-02: Audit projection must redact even when policy is 'allow'
# =========================================================================

@pytest.mark.asyncio
async def test_r2_02_audit_redacts_independent_of_allow_policy(r2_app_env, monkeypatch):
    app, config, test_db = r2_app_env
    app.state.config.dlp.risk_action.medium = "allow"
    config.dlp.risk_action.medium = "allow"
    egress = app.state.egress_guard

    captured_upstream_payload = None

    async def mock_post(url, headers=None, json_data=None, content=None):
        nonlocal captured_upstream_payload
        captured_upstream_payload = json_data
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    monkeypatch.setattr(egress, "post", mock_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        test_email = "alice.secret@company.example.com"
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "groq/test-model",
                "messages": [{"role": "user", "content": f"Please contact {test_email}"}],
            },
        )
        assert resp.status_code == 200

        # Outbound payload to upstream is allowed to contain the email per 'allow' policy
        assert captured_upstream_payload is not None
        upstream_content = captured_upstream_payload["messages"][0]["content"]
        assert test_email in upstream_content

        # BUT SQLite audit logs and prompt snippet must NEVER contain the raw email!
        dlp_logs = await storage_db.get_dlp_logs(test_db)
        assert len(dlp_logs) >= 1
        for log_entry in dlp_logs:
            assert test_email not in log_entry.get("prompt_snippet", "")
            assert test_email not in json.dumps(log_entry.get("findings", []))

        # Query raw SQLite file to ensure zero forensic trace of email in audit table
        async with storage_db.get_db(test_db) as conn:
            cursor = await conn.execute("SELECT * FROM dlp_audit_logs")
            rows = await cursor.fetchall()
            for r in rows:
                row_str = " ".join(str(val) for val in dict(r).values())
                assert test_email not in row_str, "Raw email leaked into dlp_audit_logs row!"


# =========================================================================
# R2-03: Preserving tool_call_id for role='tool' messages
# =========================================================================

@pytest.mark.asyncio
async def test_r2_03_tool_call_id_preserved_in_outbound(r2_app_env, monkeypatch):
    app, config, test_db = r2_app_env
    egress = app.state.egress_guard

    captured_upstream_payload = None

    async def mock_post(url, headers=None, json_data=None, content=None):
        nonlocal captured_upstream_payload
        captured_upstream_payload = json_data
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Tool processed"}}]})

    monkeypatch.setattr(egress, "post", mock_post)

    conversation = [
        {"role": "user", "content": "What is the weather?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{\"city\":\"Hanoi\"}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_abc123",
            "content": "28 degrees Celsius, sunny",
        },
    ]

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "groq/test-model", "messages": conversation},
        )
        assert resp.status_code == 200

        assert captured_upstream_payload is not None
        out_msgs = captured_upstream_payload["messages"]
        assert len(out_msgs) == 3
        # Check tool_call_id is preserved!
        tool_msg = out_msgs[2]
        assert tool_msg["role"] == "tool"
        assert tool_msg.get("tool_call_id") == "call_abc123"

        # Check tool message missing tool_call_id returns 400
        invalid_conv = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "missing id"},
        ]
        r_invalid = await client.post(
            "/v1/chat/completions",
            json={"model": "groq/test-model", "messages": invalid_conv},
        )
        assert r_invalid.status_code == 400


# =========================================================================
# R2-04: Strict typed validation without leaking input in error message
# =========================================================================

@pytest.mark.asyncio
async def test_r2_04_text_part_as_dict_returns_400_without_500_or_leaks(r2_app_env):
    app, _, _ = r2_app_env
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Invalid payload where text is a dict
        bad_payload = {
            "model": "groq/test-model",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": {"bad_dict": "sensitive_info_123"}}],
                }
            ],
        }
        resp = await client.post("/v1/chat/completions", json=bad_payload)
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
        err = resp.json()["error"]
        assert err["type"] == "invalid_request_error"
        # Must NOT echo back raw input dict or internal traceback
        assert "sensitive_info_123" not in str(resp.json())


# =========================================================================
# R2-05: DNS Pinning at transport boundary & anti-rebinding
# =========================================================================

@pytest.mark.asyncio
async def test_r2_05_dns_pinning_transport_boundary():
    # 1. PinnedNetworkBackend preserves original host for TLS and pins IP for connect_tcp
    pinned_map = {"api.groq.com": "93.184.216.34"}
    backend = PinnedNetworkBackend(pinned_map=pinned_map)
    assert backend.pinned_map["api.groq.com"] == "93.184.216.34"

    # 2. Verify EgressGuard pins the resolved IP upon verify_dns
    resolver_calls = []

    async def mock_resolver(host, port):
        resolver_calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    guard = EgressGuard(
        whitelist_domains=["api.groq.com"],
        block_private_ips=True,
        dns_resolver=mock_resolver,
    )
    resolved = await guard.verify_dns("api.groq.com", 443)
    assert resolved == ["93.184.216.34"]
    assert guard._pinned_hosts["api.groq.com"] == "93.184.216.34"
    assert guard._network_backend.pinned_map["api.groq.com"] == "93.184.216.34"

    # 3. Verify DNS mixed public and private rejects entire request
    async def mock_mixed_resolver(host, port):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443)),
        ]

    guard_mixed = EgressGuard(
        whitelist_domains=["api.groq.com"],
        block_private_ips=True,
        dns_resolver=mock_mixed_resolver,
    )
    with pytest.raises(EgressBlockedError) as exc_info:
        await guard_mixed.verify_dns("api.groq.com", 443)
    assert "private/non-public IP" in str(exc_info.value)

    # 4. Zero answer raises clear error
    async def mock_empty_resolver(host, port):
        return []

    guard_empty = EgressGuard(
        whitelist_domains=["api.groq.com"],
        block_private_ips=True,
        dns_resolver=mock_empty_resolver,
    )
    with pytest.raises(EgressBlockedError) as exc_info2:
        await guard_empty.verify_dns("api.groq.com", 443)
    assert "No IP addresses resolved" in str(exc_info2.value)


# =========================================================================
# R2-06: SSE Streaming lifecycle & Multi-line parsing & Per-channel restorer
# =========================================================================

@pytest.mark.asyncio
async def test_r2_06_streaming_utf8_multiline_and_tool_arguments_restoration(r2_app_env, monkeypatch):
    app, config, test_db = r2_app_env
    egress = app.state.egress_guard

    # Streaming chunks simulating:
    # 1. Multi-line data SSE event
    # 2. Vietnamese UTF-8 character split across byte chunks
    # 3. Tool call arguments restoration across SSE events
    # 4. Terminal [DONE]
    raw_secret_email = "alice.dev@company.example.com"

    async def mock_stream_post(url, headers=None, json_data=None):
        # Chunk 1: Multiline SSE event with partial Vietnamese multibyte character
        # Multi-line SSE: single valid JSON object split across multiple data: lines
        yield b"data: {\"choices\": [{\"index\": 0,\n"
        yield b"data: \"delta\": {\"content\": \"Xin ch"
        # "ào" in UTF-8: \xc3\xa0 split across chunks!
        yield b"\xc3"
        yield b"\xa0o b\xe1\xba\xa1n \"}}]}\n\n"

        # Chunk 2: Tool argument containing redacted token split across chunks
        # Event 1 emits partial token prefix
        tc_chunk1 = {
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"arguments": '{"email": "<REDACTED_'},
                    }]
                },
            }]
        }
        yield f"data: {json.dumps(tc_chunk1)}\n\n".encode("utf-8")

        # Event 2 emits the rest of the token (including closing '>') and closes JSON
        tc_chunk2 = {
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"arguments": 'EMAIL_1>"}'},
                    }]
                },
            }]
        }
        yield f"data: {json.dumps(tc_chunk2)}\n\n".encode("utf-8")

        # Chunk 3: DONE event
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr(egress, "stream_post", mock_stream_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Pre-seed sanitizer reverse_map by passing the sensitive email in prompt with 'redact'
        app.state.config.dlp.risk_action.medium = "redact"
        config.dlp.risk_action.medium = "redact"
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "groq/test-model",
                "messages": [{"role": "user", "content": f"Check user {raw_secret_email}"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        content_received = []
        arguments_received = []
        saw_done = False

        async for line in resp.aiter_lines():
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                saw_done = True
                continue
            chunk = json.loads(data_str)
            choices = chunk.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                if "content" in delta and delta["content"]:
                    content_received.append(delta["content"])
                if "tool_calls" in delta and delta["tool_calls"]:
                    tc = delta["tool_calls"][0]
                    args = tc.get("function", {}).get("arguments", "")
                    if args:
                        arguments_received.append(args)

        assert saw_done, "Stream must terminate with [DONE]"
        full_content = "".join(content_received)
        assert "Xin chào bạn" in full_content, f"Multibyte UTF-8 decode failed: {full_content}"
        full_args = "".join(arguments_received)
        assert raw_secret_email in full_args, f"Tool argument pseudonym restoration failed: {full_args}"


@pytest.mark.asyncio
async def test_r2_06_streaming_premature_eof_reports_incomplete_error(r2_app_env, monkeypatch):
    app, config, test_db = r2_app_env
    egress = app.state.egress_guard

    async def mock_stream_post_incomplete(url, headers=None, json_data=None):
        # Emits one chunk then terminates EOF WITHOUT [DONE]
        yield b"data: {\"choices\": [{\"index\": 0, \"delta\": {\"content\": \"partial content\"}}]}\n\n"

    monkeypatch.setattr(egress, "stream_post", mock_stream_post_incomplete)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "groq/test-model",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        lines = [line.strip() async for line in resp.aiter_lines() if line.strip()]
        error_found = any("upstream_incomplete" in l for l in lines)
        assert error_found, f"Stream ending without [DONE] must report upstream_incomplete error: {lines}"


# =========================================================================
# R2-07: Upstream error sanitization without leaking sensitive body/headers
# =========================================================================

@pytest.mark.asyncio
async def test_r2_07_upstream_error_sanitization_no_leak(r2_app_env, monkeypatch):
    app, config, test_db = r2_app_env
    egress = app.state.egress_guard

    sentinel_upstream_leak = "SUPER_SECRET_INTERNAL_UPSTREAM_TOKEN_XYZ_9999"

    async def mock_post_leak(url, headers=None, json_data=None, content=None):
        return httpx.Response(
            500,
            text=f'{{"internal_error": "{sentinel_upstream_leak}", "detail": "Crash in upstream server"}}',
        )

    monkeypatch.setattr(egress, "post", mock_post_leak)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "groq/test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        # Gateway returns 502 Bad Gateway
        assert resp.status_code == 502
        resp_body = resp.text
        # Sentinel must NOT leak into client response
        assert sentinel_upstream_leak not in resp_body

        # Check balancer routing status does not contain raw response text
        routing_status = await app.state.balancer.get_routing_status()
        assert sentinel_upstream_leak not in json.dumps(routing_status)


# =========================================================================
# R2-08: Policy coordination & Approval race & Timeout action redact
# =========================================================================

@pytest.mark.asyncio
async def test_r2_08_policy_coordination_timeout_action_redact(r2_app_env, monkeypatch):
    app, config, test_db = r2_app_env
    app.state.config.dlp.risk_action.medium = "approve"
    app.state.config.dlp.approval_timeout_action = "redact"
    app.state.config.dlp.approval_timeout_seconds = 1  # 1 second for fast test
    egress = app.state.egress_guard

    captured_payload = None

    async def mock_post(url, headers=None, json_data=None, content=None):
        nonlocal captured_payload
        captured_payload = json_data
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Processed"}}]})

    monkeypatch.setattr(egress, "post", mock_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        test_email = "bob.confidential@example.com"
        # Send request containing medium finding (email)
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "groq/test-model",
                "messages": [{"role": "user", "content": f"Send report to {test_email}"}],
            },
        )
        assert resp.status_code == 200

        # Since approval timed out and approval_timeout_action == "redact",
        # outbound message must be REDACTED, never raw email!
        assert captured_payload is not None
        out_msg = captured_payload["messages"][0]["content"]
        assert test_email not in out_msg
        assert "<REDACTED_" in out_msg


@pytest.mark.asyncio
async def test_r2_08_admin_reject_blocks_request(r2_app_env, monkeypatch):
    app, config, test_db = r2_app_env
    app.state.config.dlp.risk_action.medium = "approve"
    app.state.config.dlp.approval_timeout_seconds = 10
    gate = app.state.approval_gate

    async def auto_reject():
        # Wait a tiny moment for the request to be registered in gate
        await asyncio.sleep(0.1)
        for _ in range(50):
            async with gate._lock:
                gates = list(gate._gates.keys())
            if gates:
                await gate.submit_decision(gates[0], "rejected")
                return
            await asyncio.sleep(0.05)

    reject_task = asyncio.create_task(auto_reject())

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "groq/test-model",
                "messages": [{"role": "user", "content": "Contact admin@corp.example.com"}],
            },
        )
        assert resp.status_code == 403
        err = resp.json()["error"]
        assert err["type"] == "approval_rejected"

    await reject_task


# =========================================================================
# R2-09: Context budget estimation & Explicit model 404/400 & Isolation
# =========================================================================

@pytest.mark.asyncio
async def test_r2_09_explicit_model_not_found_returns_404(r2_app_env):
    app, _, _ = r2_app_env
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "non-existent-provider/fake-model-12345",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["type"] == "model_not_found"


@pytest.mark.asyncio
async def test_r2_09_context_length_exceeded_returns_400(r2_app_env):
    app, _, _ = r2_app_env
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # groq/test-model context_length is 128000
        # Requesting output reserve max_tokens=200000 exceeds context budget
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "groq/test-model",
                "messages": [{"role": "user", "content": "Write an encyclopedia"}],
                "max_tokens": 200000,
            },
        )
        assert resp.status_code == 400
        err = resp.json()["error"]
        assert err["type"] == "invalid_request_error"
        assert "context length" in err["message"].lower()


@pytest.mark.asyncio
async def test_r2_09_test_environment_isolation_no_cache_mutation_or_pid_leak():
    # Verify that get_config() default cache was NOT modified by previous test fixtures
    cached_cfg = get_config()
    assert cached_cfg.db_path == "storage/gateway.db"

    # Verify that .gateway.pid was not created by the test process itself
    pid_file = PROJECT_ROOT / ".gateway.pid"
    if pid_file.exists():
        recorded_pid = pid_file.read_text(encoding="utf-8").strip()
        assert recorded_pid != str(os.getpid()), f"Test process leaked its own PID ({os.getpid()}) into .gateway.pid!"


@pytest.mark.asyncio
async def test_cline_reasoning_content_supported(r2_app_env, monkeypatch):
    """Verify that assistant messages with reasoning_content (sent by Cline / thinking models) are supported without 400 error."""
    app, config, test_db = r2_app_env
    egress = app.state.egress_guard

    captured_payload = None

    async def mock_post(url, headers=None, json_data=None, content=None):
        nonlocal captured_payload
        captured_payload = json_data
        return httpx.Response(200, json={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "Result",
                    "reasoning_content": "Thinking process",
                }
            }]
        })

    monkeypatch.setattr(egress, "post", mock_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # History sent by Cline containing reasoning_content in assistant message
        history = [
            {"role": "user", "content": "Solve 2+2"},
            {"role": "assistant", "content": "4", "reasoning_content": "2 plus 2 equals 4."},
            {"role": "user", "content": "What about 3+3?"},
        ]
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "groq/test-model", "messages": history},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert captured_payload is not None
        out_msgs = captured_payload["messages"]
        assert len(out_msgs) == 3
        assert out_msgs[1].get("reasoning_content") == "2 plus 2 equals 4."
        # Verify response also restores reasoning_content
        resp_json = resp.json()
        assert resp_json["choices"][0]["message"]["reasoning_content"] == "Thinking process"
