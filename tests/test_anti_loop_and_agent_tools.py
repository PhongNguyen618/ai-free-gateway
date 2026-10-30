import asyncio
import json
import pytest
import httpx
from httpx import AsyncClient, ASGITransport

from src.config_loader import get_config
from src.main import create_app
from src.router.openai_proxy import restore_tool_arguments_json
from src.storage import db as storage_db


@pytest.fixture
async def anti_loop_env(tmp_path):
    test_db = str(tmp_path / "anti_loop_test.db")
    config = get_config().deep_copy()
    config.db_path = test_db
    config.server.gateway_api_key = ""
    config.server.record_pid = False

    # Generous limits for large prompt and streaming testing
    config.server.max_body_bytes = 1024 * 1024
    config.egress.max_response_bytes = 1024 * 1024
    config.server.max_sse_line_bytes = 65536
    config.server.max_sse_event_bytes = 65536
    config.server.max_tool_buffer_bytes = 65536

    config.dlp.risk_action.high = "redact"
    config.dlp.risk_action.medium = "redact"
    config.dlp.risk_action.low = "redact"
    config.dlp.pseudonymize_reversible = True

    await storage_db.init_db(test_db)
    await storage_db.upsert_model(test_db, {
        "id": "groq/primary-model",
        "provider": "groq",
        "model_name": "primary-model",
        "display_name": "Groq Primary Model",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 25.0,
    })
    await storage_db.upsert_model(test_db, {
        "id": "openrouter/secondary-model",
        "provider": "openrouter",
        "model_name": "secondary-model",
        "display_name": "OpenRouter Secondary Model",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 50.0,
    })

    app = create_app(config)
    yield app, config, test_db


# =========================================================================
# U01: Streaming Tool Calls Accumulation, DLP Restoration & Ordering
# =========================================================================

@pytest.mark.asyncio
async def test_u01_streaming_tool_calls_preserves_id_name_and_valid_json_arguments(anti_loop_env, monkeypatch):
    """
    Test U01: Verify multi-chunk streaming tool calls accumulate correctly,
    restore DLP pseudonymized secrets in arguments, and never emit tool_calls chunks
    after finish_reason='tool_calls'.
    """
    app, config, test_db = anti_loop_env
    egress = app.state.egress_guard

    chunks = [
        b'data: {"id":"chatcmpl-123","choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_read_1","type":"function","function":{"name":"read_file","arguments":""}}]},"finish_reason":null}]}\n\n',
        b'data: {"id":"chatcmpl-123","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"path\\": \\""}}]},"finish_reason":null}]}\n\n',
        b'data: {"id":"chatcmpl-123","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"<REDACTED_EMAIL_1>/config.py\\"}"}}]},"finish_reason":null}]}\n\n',
        b'data: {"id":"chatcmpl-123","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"total_tokens":30}}\n\n',
        b'data: [DONE]\n\n',
    ]

    async def mock_stream_post(*args, **kwargs):
        for c in chunks:
            yield c

    monkeypatch.setattr(egress, "stream_post", mock_stream_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Prompt contains email to generate reverse_map via DLP redaction
        payload = {
            "model": "groq/primary-model",
            "messages": [{"role": "user", "content": "Check alice@example.com config"}],
            "stream": True,
        }
        resp = await client.post("/v1/chat/completions", json=payload)
        assert resp.status_code == 200

        tool_calls = {}
        found_finish_reason = False
        tool_calls_after_finish = 0

        async for line in resp.aiter_lines():
            line = line.strip()
            if not line.startswith("data: "):
                continue
            event_data = line[6:].strip()
            if event_data == "[DONE]":
                continue

            chunk = json.loads(event_data)
            choices = chunk.get("choices", [])
            if not choices:
                continue

            delta = choices[0].get("delta", {})
            finish_reason = choices[0].get("finish_reason")

            if found_finish_reason:
                if "tool_calls" in delta and delta["tool_calls"]:
                    tool_calls_after_finish += 1

            if finish_reason == "tool_calls":
                found_finish_reason = True

            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                if idx not in tool_calls:
                    tool_calls[idx] = {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                if tc.get("id"):
                    tool_calls[idx]["id"] = tc["id"]
                if tc.get("type"):
                    tool_calls[idx]["type"] = tc["type"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    tool_calls[idx]["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    tool_calls[idx]["function"]["arguments"] += fn["arguments"]

        assert found_finish_reason is True, "Must receive finish_reason='tool_calls'"
        assert tool_calls_after_finish == 0, "No tool_calls chunk must follow finish_reason"
        assert 0 in tool_calls
        call = tool_calls[0]
        assert call["id"] == "call_read_1"
        assert call["type"] == "function"
        assert call["function"]["name"] == "read_file"

        parsed_args = json.loads(call["function"]["arguments"])
        assert parsed_args == {"path": "alice@example.com/config.py"}


@pytest.mark.asyncio
async def test_u01_streaming_single_chunk_tool_call_with_finish_reason(anti_loop_env, monkeypatch):
    """
    Test U01: Handle providers that emit complete tool calls and finish_reason
    in a single SSE chunk without dropping id, name, or DLP-restored arguments.
    """
    app, config, test_db = anti_loop_env
    egress = app.state.egress_guard

    chunks = [
        b'data: {"id":"chatcmpl-single","choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_single_1","type":"function","function":{"name":"search_user","arguments":"{\\"email\\": \\"<REDACTED_EMAIL_1>\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n',
        b'data: [DONE]\n\n',
    ]

    async def mock_stream_post(*args, **kwargs):
        for c in chunks:
            yield c

    monkeypatch.setattr(egress, "stream_post", mock_stream_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "model": "groq/primary-model",
            "messages": [{"role": "user", "content": "Find bob@example.com"}],
            "stream": True,
        }
        resp = await client.post("/v1/chat/completions", json=payload)
        assert resp.status_code == 200

        tool_calls = {}
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:].strip())
            choices = chunk.get("choices", [])
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                if idx not in tool_calls:
                    tool_calls[idx] = {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                if tc.get("id"):
                    tool_calls[idx]["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    tool_calls[idx]["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    tool_calls[idx]["function"]["arguments"] += fn["arguments"]

        assert 0 in tool_calls
        call = tool_calls[0]
        assert call["id"] == "call_single_1"
        assert call["function"]["name"] == "search_user"
        parsed = json.loads(call["function"]["arguments"])
        assert parsed == {"email": "bob@example.com"}


# =========================================================================
# U02: Multimodal Normalization & Safe System Prompt Preservation
# =========================================================================

@pytest.mark.asyncio
async def test_u02_multimodal_tool_and_user_messages_normalized_and_system_prompt_not_truncated(anti_loop_env, monkeypatch):
    """
    Test U02: Verify that large System prompt retains its footer (not truncated from the tail),
    no extra system message is injected midway, and ContentPart lists in user/tool messages
    are flattened to string for upstream compatibility.
    """
    app, config, test_db = anti_loop_env
    egress = app.state.egress_guard

    sys_prompt = "You are a coding assistant. " * 750 + "\nCRITICAL_TOOL_FORMAT_FOOTER_END"
    assert len(sys_prompt) > 18000

    captured_payload = None

    async def mock_post(url, headers=None, json_data=None, content=None):
        nonlocal captured_payload
        captured_payload = json_data
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "Acknowledged."}}],
            "usage": {"total_tokens": 100},
        })

    monkeypatch.setattr(egress, "post", mock_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "model": "groq/primary-model",
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": [{"type": "text", "text": "Execute task"}]},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "run", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "File content ok"}]},
            ],
            "tools": [{"type": "function", "function": {"name": "run", "parameters": {"type": "object"}}}],
        }
        resp = await client.post("/v1/chat/completions", json=payload)
        assert resp.status_code == 200

    assert captured_payload is not None
    messages = captured_payload["messages"]

    # 1. System prompt preserved end marker
    assert messages[0]["role"] == "system"
    assert "CRITICAL_TOOL_FORMAT_FOOTER_END" in messages[0]["content"]

    # 2. No second system message injected into messages array
    system_messages = [m for m in messages if m.get("role") == "system"]
    assert len(system_messages) == 1, "Must have exactly 1 system message, no middle injections"

    # 3. User message flattened from list of ContentParts to str
    user_msg = next(m for m in messages if m.get("role") == "user")
    assert isinstance(user_msg["content"], str)
    assert user_msg["content"] == "Execute task"

    # 4. Tool message flattened from list of ContentParts to str
    tool_msg = next(m for m in messages if m.get("role") == "tool")
    assert isinstance(tool_msg["content"], str)
    assert tool_msg["content"] == "File content ok"


# =========================================================================
# U03: Dead Model Penalization & Health-Prioritized Routing
# =========================================================================

@pytest.mark.asyncio
async def test_u03_dead_models_with_high_errors_ranked_below_healthy_models(anti_loop_env):
    """
    Test U03: Models with consecutive errors (dead models) are heavily penalized
    and ranked below healthy models with proven successes, even if the dead model
    is listed in priority_models.
    """
    app, config, test_db = anti_loop_env
    balancer = app.state.balancer

    # Seed Model A (Dead): error_count = 15, success_count = 0
    await storage_db.upsert_model(test_db, {
        "id": "github/dead-model",
        "provider": "github",
        "model_name": "dead-model",
        "display_name": "GitHub Dead Model",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 10.0,
    })
    # Update metrics in DB: error_count=15, success_count=0
    for _ in range(15):
        await storage_db.update_model_metrics(test_db, "github/dead-model", 500.0, False)

    # Seed Model B (Healthy): error_count = 0, success_count = 5
    await storage_db.upsert_model(test_db, {
        "id": "groq/healthy-model",
        "provider": "groq",
        "model_name": "healthy-model",
        "display_name": "Groq Healthy Model",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 50.0,
    })
    for _ in range(5):
        await storage_db.update_model_metrics(test_db, "groq/healthy-model", 50.0, True)

    # Configure priority_models favoring Model A
    config.harvester.priority_models = ["github/dead-model", "groq/healthy-model"]

    # Candidate chain resolution
    chain = await balancer.get_candidate_chain("auto")
    chain_ids = [c["id"] for c in chain]

    assert "groq/healthy-model" in chain_ids
    assert "github/dead-model" in chain_ids

    # Healthy model must rank BEFORE the dead model despite priority_models list
    healthy_idx = chain_ids.index("groq/healthy-model")
    dead_idx = chain_ids.index("github/dead-model")
    assert healthy_idx < dead_idx, "Healthy model must be ranked ahead of dead model with high errors"


# =========================================================================
# U04: Agent Repetition Loop Detection & Mitigation Notice Injection
# =========================================================================

@pytest.mark.asyncio
async def test_u04_agent_loop_detection_injects_recovery_notice_and_adjusts_temperature(anti_loop_env, monkeypatch):
    """
    Test U04: When conversation history contains consecutive identical tool calls
    and responses, detect_agent_loop injects a guidance notice and increases temperature.
    """
    app, config, test_db = anti_loop_env
    egress = app.state.egress_guard

    captured_payload = None

    async def mock_post(url, headers=None, json_data=None, content=None):
        nonlocal captured_payload
        captured_payload = json_data
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "I will try an alternative approach."}}],
            "usage": {"total_tokens": 50},
        })

    monkeypatch.setattr(egress, "post", mock_post)

    looping_messages = [
        {"role": "user", "content": "Fix the missing dependency"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "package.json"}'}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "Error: File not found"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "package.json"}'}}],
        },
        {"role": "tool", "tool_call_id": "c2", "content": "Error: File not found"},
    ]

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "model": "groq/primary-model",
            "messages": looping_messages,
            "temperature": 0.0,
        }
        resp = await client.post("/v1/chat/completions", json=payload)
        assert resp.status_code == 200

    assert captured_payload is not None
    # 1. Temperature raised to at least 0.5 to break agent determinism loop
    assert captured_payload.get("temperature", 0.0) >= 0.5

    # 2. Loop mitigation notice injected into messages
    all_contents = [str(m.get("content") or "") for m in captured_payload["messages"]]
    has_notice = any("[SYSTEM NOTICE:" in c for c in all_contents)
    assert has_notice is True, "Must inject [SYSTEM NOTICE: ...] to guide agent out of loop"


# =========================================================================
# U05: Empty Arguments Normalization
# =========================================================================

@pytest.mark.asyncio
async def test_u05_empty_arguments_normalized_to_json_object(anti_loop_env, monkeypatch):
    """
    Test U05: Empty arguments (empty string, whitespace, or None) are normalized
    to '{}' instead of crashing JSON parser or emitting invalid schema.
    """
    # 1. Unit test restore_tool_arguments_json behavior
    assert restore_tool_arguments_json("", {}) == "{}"
    assert restore_tool_arguments_json("   ", {}) == "{}"
    assert restore_tool_arguments_json(None, {}) == "{}"
    assert restore_tool_arguments_json('{"key": "<REDACTED_1>"}', {"<REDACTED_1>": "value"}) == '{"key": "value"}'

    # 2. End-to-end non-streaming test: upstream returns empty string arguments
    app, config, test_db = anti_loop_env
    egress = app.state.egress_guard

    async def mock_post(url, headers=None, json_data=None, content=None):
        return httpx.Response(200, json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "call_empty_1", "type": "function", "function": {"name": "get_status", "arguments": ""}}
                        ],
                    }
                }
            ],
            "usage": {"total_tokens": 15},
        })

    monkeypatch.setattr(egress, "post", mock_post)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "model": "groq/primary-model",
            "messages": [{"role": "user", "content": "What is status?"}],
            "tools": [{"type": "function", "function": {"name": "get_status", "parameters": {"type": "object"}}}],
        }
        resp = await client.post("/v1/chat/completions", json=payload)
        assert resp.status_code == 200
        data = resp.json()

        tc = data["choices"][0]["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "get_status"
        assert tc["function"]["arguments"] == "{}"
