from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Dict, List, Optional
import pytest
import httpx
from httpx import AsyncClient, ASGITransport

from src.config_loader import AppConfig, get_config
from src.main import create_app
from src.router.openai_proxy import (
    CompletionAdmissionController,
    AdmissionLimitExceeded,
    InvalidUpstreamToolArgumentsError,
    restore_json_structure,
    restore_tool_arguments_json,
)
from src.security.egress_guard import EgressGuard, EgressBlockedError
from src.security.sanitizer import DLPSanitizer
from src.storage import db as storage_db


@pytest.fixture
async def spec_app_env(tmp_path):
    test_db = str(tmp_path / "spec_test.db")
    config = get_config().deep_copy()
    config.db_path = test_db
    config.server.gateway_api_key = ""
    config.server.record_pid = False
    config.server.max_body_bytes = 1024  # Set low (1 KiB) for testing
    config.server.max_concurrent_completions = 2  # Low for testing admission
    config.server.max_pending_approvals = 3  # Low for testing admission
    config.egress.max_response_bytes = 2048  # Low (2 KiB) for testing
    config.server.max_sse_line_bytes = 512
    config.server.max_sse_event_bytes = 1024
    config.server.max_tool_buffer_bytes = 1024
    config.server.first_event_timeout_seconds = 0.5
    config.server.idle_timeout_seconds = 1.0
    config.server.max_stream_duration_seconds = 2.0
    config.server.total_completion_timeout_seconds = 2.0

    config.dlp.risk_action.high = "redact"
    config.dlp.risk_action.medium = "redact"
    config.dlp.risk_action.low = "redact"

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
    await storage_db.upsert_model(test_db, {
        "id": "openrouter/fallback-model",
        "provider": "openrouter",
        "model_name": "fallback-model",
        "display_name": "OpenRouter Fallback",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 50.0,
    })

    app = create_app(config)
    yield app, config, test_db


# =========================================================================
# N01: Resource Limits & Admission
# =========================================================================

@pytest.mark.asyncio
async def test_n01_body_limit_early_rejection_content_length(spec_app_env, monkeypatch):
    app, config, test_db = spec_app_env
    egress = app.state.egress_guard

    upstream_called = False
    async def mock_post(*args, **kwargs):
        nonlocal upstream_called
        upstream_called = True
        return httpx.Response(200, json={"choices": []})
    monkeypatch.setattr(egress, "post", mock_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Body size 1500 bytes exceeds max_body_bytes (1024 bytes)
        big_content = "A" * 1500
        payload = {"model": "groq/primary-model", "messages": [{"role": "user", "content": big_content}]}
        body_bytes = json.dumps(payload).encode("utf-8")

        resp = await client.post(
            "/v1/chat/completions",
            content=body_bytes,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body_bytes))},
        )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "payload_too_large"
        assert not upstream_called


@pytest.mark.asyncio
async def test_n01_body_limit_chunked_without_content_length(spec_app_env, monkeypatch):
    app, config, test_db = spec_app_env
    egress = app.state.egress_guard

    upstream_called = False
    async def mock_post(*args, **kwargs):
        nonlocal upstream_called
        upstream_called = True
        return httpx.Response(200, json={"choices": []})
    monkeypatch.setattr(egress, "post", mock_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Generator for streaming chunks exceeding limit
        async def chunk_gen():
            for _ in range(5):
                yield b"X" * 300  # Total 1500 > 1024

        resp = await client.post(
            "/v1/chat/completions",
            content=chunk_gen(),
            headers={"Content-Type": "application/json"},  # No Content-Length
        )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "payload_too_large"
        assert not upstream_called


@pytest.mark.asyncio
async def test_n01_admission_limit_concurrency_and_retry_after(spec_app_env, monkeypatch):
    app, config, test_db = spec_app_env
    egress = app.state.egress_guard

    # Simulate upstream taking some time so requests overlap
    release_event = asyncio.Event()

    async def mock_post(*args, **kwargs):
        await release_event.wait()
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    monkeypatch.setattr(egress, "post", mock_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {"model": "groq/primary-model", "messages": [{"role": "user", "content": "hi"}]}

        # Config has max_concurrent_completions = 2
        task1 = asyncio.create_task(client.post("/v1/chat/completions", json=payload))
        task2 = asyncio.create_task(client.post("/v1/chat/completions", json=payload))

        await asyncio.sleep(0.05)  # Let both acquire completion slots

        # 3rd request must be rejected with 429 gateway_overloaded
        resp3 = await client.post("/v1/chat/completions", json=payload)
        assert resp3.status_code == 429
        assert resp3.json()["error"]["code"] == "gateway_overloaded"
        assert resp3.headers.get("Retry-After") is not None

        # System health & dashboard endpoints MUST still be 200 OK while completions are saturated
        health_resp = await client.get("/health")
        assert health_resp.status_code == 200

        # Release upstream to complete pending requests
        release_event.set()
        resp1 = await task1
        resp2 = await task2
        assert resp1.status_code == 200
        assert resp2.status_code == 200

        # Now that slots are released, a new completion succeeds
        resp4 = await client.post("/v1/chat/completions", json=payload)
        assert resp4.status_code == 200


@pytest.mark.asyncio
async def test_n01_egress_max_response_bytes_ceiling(tmp_path):
    guard = EgressGuard(
        whitelist_domains=["openrouter.ai"],
        max_response_bytes=100,  # 100 bytes max
    )

    class FakeStreamResponse:
        def __init__(self, headers, chunks):
            self.headers = headers
            self._chunks = chunks
            self.status_code = 200

        async def aiter_bytes(self):
            for c in self._chunks:
                yield c

        async def aclose(self):
            pass

    async def fake_send(req, stream=False):
        return FakeStreamResponse({"content-length": "200"}, [b"X" * 200])

    guard._client.send = fake_send
    guard.validate_url = lambda u: ("openrouter.ai", 443, "https")
    guard.verify_dns = lambda h, p: asyncio.sleep(0)

    with pytest.raises(EgressBlockedError, match="exceeds maximum allowed limit"):
        await guard.post("https://openrouter.ai/api/v1/chat/completions", json_data={"test": 1})


# =========================================================================
# N02: Tool Arguments JSON Restoration & Terminal SSE
# =========================================================================

def test_n02_restore_tool_arguments_json_special_chars():
    reverse_map = {
        "<REDACTED_API_KEY_1>": 'secret"with"quotes\\and\\backslashes\nand\nnewlines',
    }
    raw_args = json.dumps({
        "api_key": "<REDACTED_API_KEY_1>",
        "endpoint": "https://api.internal.com",
    })

    restored_str = restore_tool_arguments_json(raw_args, reverse_map)
    # The output string MUST be valid parseable JSON!
    parsed = json.loads(restored_str)
    assert parsed["api_key"] == 'secret"with"quotes\\and\\backslashes\nand\nnewlines'
    assert parsed["endpoint"] == "https://api.internal.com"


def test_n02_restore_tool_arguments_invalid_upstream_json():
    reverse_map = {"<REDACTED_KEY_1>": "my-secret"}
    malformed_raw = '{"api_key": "<REDACTED_KEY_1>", missing_bracket'
    with pytest.raises(InvalidUpstreamToolArgumentsError):
        restore_tool_arguments_json(malformed_raw, reverse_map)


@pytest.mark.asyncio
async def test_n02_sse_tool_arguments_flushed_before_finish_reason(spec_app_env, monkeypatch):
    app, config, test_db = spec_app_env
    egress = app.state.egress_guard

    secret_token = "ghp_123456789012345678901234567890123456"
    chunks = [
        b'data: {"id":"1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"query","arguments":""}}]},"finish_reason":null}]}\n\n',
        b'data: {"id":"1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"key\\": \\"<REDACTED_GITHUB_TOKEN_1>\\"}"}}]},"finish_reason":null}]}\n\n',
        b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"total_tokens":42}}\n\n',
        b'data: [DONE]\n\n',
    ]

    async def mock_stream_post(*args, **kwargs):
        for c in chunks:
            yield c
    monkeypatch.setattr(egress, "stream_post", mock_stream_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Send prompt with secret that triggers DLP redaction and generates reverse_map
        payload = {
            "model": "groq/primary-model",
            "messages": [{"role": "user", "content": f"Use secret: {secret_token}"}],
            "stream": True,
        }
        resp = await client.post("/v1/chat/completions", json=payload)
        assert resp.status_code == 200

        received_events = []
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                received_events.append(line[6:].strip())

        # Verify [DONE] is emitted exactly once at the end
        done_events = [e for e in received_events if e == "[DONE]"]
        assert len(done_events) == 1
        assert received_events[-1] == "[DONE]"

        # Check tool arguments delta and finish_reason ordering
        finish_event_idx = None
        tool_args_event_idx = None
        restored_args_val = None

        for idx, ev in enumerate(received_events):
            if ev == "[DONE]":
                continue
            data = json.loads(ev)
            choice = data["choices"][0]
            if choice.get("finish_reason") == "tool_calls":
                finish_event_idx = idx
            delta = choice.get("delta", {})
            if "tool_calls" in delta and delta["tool_calls"]:
                fn = delta["tool_calls"][0].get("function", {})
                args = fn.get("arguments", "")
                if secret_token in args:
                    tool_args_event_idx = idx
                    restored_args_val = args

        assert tool_args_event_idx is not None
        assert finish_event_idx is not None
        # Must be flushed BEFORE finish_reason!
        assert tool_args_event_idx < finish_event_idx

        # And restored argument must be valid JSON
        parsed_args = json.loads(restored_args_val)
        assert parsed_args["key"] == secret_token


@pytest.mark.asyncio
async def test_n02_sse_malformed_event_terminates_stream_without_success(spec_app_env, monkeypatch):
    app, config, test_db = spec_app_env
    egress = app.state.egress_guard

    chunks = [
        b'data: {"id":"1","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}\n\n',
        b'data: {BAD MALFORMED JSON EVENT\n\n',
        b'data: [DONE]\n\n',
    ]

    async def mock_stream_post(*args, **kwargs):
        for c in chunks:
            yield c
    monkeypatch.setattr(egress, "stream_post", mock_stream_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {"model": "groq/primary-model", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        resp = await client.post("/v1/chat/completions", json=payload)
        assert resp.status_code == 200

        lines = [line async for line in resp.aiter_lines()]
        # Verify an error was emitted in stream
        has_error = any("error" in line.lower() for line in lines)
        assert has_error

        # Model metrics success count must NOT be incremented
        m = await storage_db.get_model_by_id(test_db, "groq/primary-model")
        assert m["success_count"] == 0


@pytest.mark.asyncio
async def test_n02_multi_choice_interleaved_tools(spec_app_env, monkeypatch):
    app, config, test_db = spec_app_env
    egress = app.state.egress_guard

    # 2 choices, 2 tools interleaved across chunks
    chunks = [
        b'data: {"id":"1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_0_0","type":"function","function":{"name":"f1","arguments":"{\\"a\\": "}}]},"finish_reason":null}]}\n\n',
        b'data: {"id":"1","choices":[{"index":1,"delta":{"tool_calls":[{"index":0,"id":"call_1_0","type":"function","function":{"name":"f2","arguments":"{\\"b\\": "}}]},"finish_reason":null}]}\n\n',
        b'data: {"id":"1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]},"finish_reason":null}]}\n\n',
        b'data: {"id":"1","choices":[{"index":1,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"2}"}}]},"finish_reason":null}]}\n\n',
        b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
        b'data: {"id":"1","choices":[{"index":1,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
        b'data: [DONE]\n\n',
    ]

    async def mock_stream_post(*args, **kwargs):
        for c in chunks:
            yield c
    monkeypatch.setattr(egress, "stream_post", mock_stream_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {"model": "groq/primary-model", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        resp = await client.post("/v1/chat/completions", json=payload)
        assert resp.status_code == 200

        received_events = []
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                received_events.append(line[6:].strip())

        # Verify arguments for choice 0 and choice 1 did not intermix
        c0_args = []
        c1_args = []
        for ev in received_events:
            if ev == "[DONE]":
                continue
            data = json.loads(ev)
            choice = data["choices"][0]
            c_idx = choice.get("index")
            delta = choice.get("delta", {})
            if "tool_calls" in delta and delta["tool_calls"]:
                fn = delta["tool_calls"][0].get("function", {})
                args = fn.get("arguments", "")
                if args:
                    if c_idx == 0:
                        c0_args.append(args)
                    elif c_idx == 1:
                        c1_args.append(args)

        assert any('{"a": 1}' in a for a in c0_args)
        assert any('{"b": 2}' in b for b in c1_args)


@pytest.mark.asyncio
async def test_n01_sse_oversized_line_terminated(spec_app_env, monkeypatch):
    app, config, test_db = spec_app_env
    egress = app.state.egress_guard

    # Oversized line > max_sse_line_bytes (512 bytes)
    big_line = b"data: " + (b"A" * 600) + b"\n\n"

    async def mock_stream_post(*args, **kwargs):
        yield big_line
    monkeypatch.setattr(egress, "stream_post", mock_stream_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {"model": "groq/primary-model", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        resp = await client.post("/v1/chat/completions", json=payload)
        assert resp.status_code == 200

        lines = [line async for line in resp.aiter_lines()]
        assert any("error" in l.lower() for l in lines)
        assert any("[DONE]" in l for l in lines)


@pytest.mark.asyncio
async def test_n01_tool_buffer_limit_exceeded(spec_app_env, monkeypatch):
    app, config, test_db = spec_app_env
    egress = app.state.egress_guard

    # Tool arguments chunk > max_tool_buffer_bytes (1024 bytes)
    big_args = "X" * 1200
    chunk = f'data: {{"id":"1","choices":[{{"index":0,"delta":{{"tool_calls":[{{"index":0,"function":{{"arguments":"{big_args}"}}}}]}},"finish_reason":null}}]}}\n\n'.encode()

    async def mock_stream_post(*args, **kwargs):
        yield chunk
    monkeypatch.setattr(egress, "stream_post", mock_stream_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {"model": "groq/primary-model", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        resp = await client.post("/v1/chat/completions", json=payload)
        assert resp.status_code == 200

        lines = [line async for line in resp.aiter_lines()]
        assert any("error" in l.lower() for l in lines)
        assert any("[DONE]" in l for l in lines)
