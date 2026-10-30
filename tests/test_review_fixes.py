import json
import logging

import httpx
import pytest

from src.config_loader import AppConfig, ProviderDetail
from src.main import create_app
from src.storage import db


@pytest.fixture
async def review_app(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEW_UNUSED_KEY", "mock-groq-key")
    config = AppConfig(db_path=str(tmp_path / "gateway.db"))
    config.server.record_pid = False
    config.dlp.entropy_scan.enabled = False
    config.providers.groq = ProviderDetail(
        base_url="https://api.groq.com/openai/v1", api_key_env="REVIEW_UNUSED_KEY",
    )
    await db.init_db(config.db_path)
    await db.upsert_model(config.db_path, {
        "id": "groq/test", "provider": "groq", "model_name": "test",
        "context_length": 128000,
    })
    app = create_app(config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        yield app, client, config
    await app.state.egress_guard.close()


KEY = "AKIAIOSFODNN7EXAMPLE"
BASE = {"model": "groq/test", "messages": [{"role": "user", "content": "hello"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"response_format": {"type": "json_schema", "json_schema": {"schema": {"description": KEY}}}},
    {"stop": [KEY]},
    {"stream_options": {"extra": KEY}},
    {"tool_choice": {"type": "function", "function": {"name": KEY}}},
    {"tools": [{"type": "function", "function": {"name": "lookup", "parameters": {
        "type": "object", "properties": {KEY: {"type": "string"}},
    }}}]},
    {"messages": [{"role": "tool", "tool_call_id": KEY, "content": "ok"}]},
])
async def test_sensitive_control_fields_never_reach_upstream(review_app, monkeypatch, extra):
    app, client, config = review_app
    async def unexpected(*args, **kwargs):
        pytest.fail("DLP violation reached upstream")
    monkeypatch.setattr(app.state.egress_guard, "post", unexpected)
    response = await client.post("/v1/chat/completions", json={**BASE, **extra})
    assert response.status_code == 403
    assert KEY not in response.text
    assert KEY not in json.dumps(await db.get_dlp_logs(config.db_path))


@pytest.mark.asyncio
async def test_clean_controls_preserved_and_success_counted_once(review_app, monkeypatch):
    app, client, config = review_app
    async def post(url, headers=None, json_data=None, **kwargs):
        assert json_data["stop"] == ["END"]
        assert json_data["stream_options"] == {"include_usage": True}
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 3},
        })
    monkeypatch.setattr(app.state.egress_guard, "post", post)
    response = await client.post("/v1/chat/completions", json={
        **BASE, "stop": ["END"], "stream_options": {"include_usage": True},
    })
    assert response.status_code == 200
    model = await db.get_model_by_id(config.db_path, "groq/test")
    assert model["success_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("http_error", [True, False])
async def test_stream_errors_do_not_log_raw_data(review_app, monkeypatch, caplog, http_error):
    app, client, _ = review_app
    sentinel = "SYNTHETIC_PRIVATE_ERROR_123"
    async def stream(*args, **kwargs):
        if http_error:
            request = httpx.Request("POST", "https://api.groq.com")
            response = httpx.Response(400, text=sentinel, request=request)
            raise httpx.HTTPStatusError(sentinel, request=request, response=response)
        raise RuntimeError(sentinel)
        yield b""
    monkeypatch.setattr(app.state.egress_guard, "stream_post", stream)
    with caplog.at_level(logging.ERROR):
        response = await client.post("/v1/chat/completions", json={**BASE, "stream": True})
    assert response.status_code == 502
    assert sentinel not in caplog.text
    assert sentinel not in response.text
    assert sentinel not in json.dumps(app.state.balancer.last_fallback_chain)


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
async def test_stream_tool_indices_and_completion_metrics(review_app, monkeypatch, complete):
    app, client, config = review_app
    token = "<REDACTED_EMAIL_1>"
    app.state.config.dlp.risk_action.medium = "redact"
    closed = []
    val0 = json.dumps({"email": token})
    val1 = json.dumps({"tool": "SECOND_TOOL"})
    split_pos = len(val0) // 2
    async def stream(*args, **kwargs):
        try:
            for index, value in [(0, val0[:split_pos]), (1, val1), (0, val0[split_pos:])]:
                event = {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": index, "function": {"arguments": value}},
                ]}}]}
                yield ("data: " + json.dumps(event) + "\n\n").encode()
            if complete:
                yield b"data: [DONE]\n\n"
        finally:
            closed.append(True)
    monkeypatch.setattr(app.state.egress_guard, "stream_post", stream)
    response = await client.post("/v1/chat/completions", json={
        "model": "groq/test", "stream": True,
        "messages": [{"role": "user", "content": "alice@example.com"}],
    })
    arguments = {0: "", 1: ""}
    for line in response.text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            for choice in json.loads(line[6:]).get("choices", []):
                for call in choice.get("delta", {}).get("tool_calls", []):
                    arguments[call["index"]] += call["function"]["arguments"]
    assert json.loads(arguments[0]) == {"email": "alice@example.com"}
    assert json.loads(arguments[1]) == {"tool": "SECOND_TOOL"}
    assert closed == [True]
    model = await db.get_model_by_id(config.db_path, "groq/test")
    assert model["success_count"] == int(complete)
    assert ("upstream_incomplete" in response.text) == (not complete)
