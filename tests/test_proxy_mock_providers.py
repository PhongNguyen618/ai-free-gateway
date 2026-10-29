from __future__ import annotations

import asyncio
import json
import pytest
import httpx
from httpx import AsyncClient, ASGITransport

from src.config_loader import get_config
from src.discovery.harvester import ModelHarvester
from src.router.balancer import ModelBalancer
from src.router.openai_proxy import create_openai_router
from src.security.approval_gate import ApprovalGate
from src.security.egress_guard import EgressGuard
from src.storage import db as storage_db
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_proxy_chat_completions_non_stream_with_restoration(monkeypatch, tmp_path):
    """
    Test that sensitive data in prompt is sanitized before sending to upstream provider,
    and when the provider returns the placeholder in the answer, the proxy automatically
    restores the original value for the client!
    """
    config = get_config()
    test_db = str(tmp_path / "test_proxy_mock.db")
    await storage_db.init_db(test_db)

    # Insert test model
    await storage_db.upsert_model(test_db, {
        "id": "groq/llama-3.3-70b-versatile",
        "provider": "groq",
        "model_name": "llama-3.3-70b-versatile",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 120.0,
    })

    test_config = config.model_copy()
    test_config.db_path = test_db
    test_config.dlp.risk_action.medium = "redact"  # Auto-redact without stopping for HITL

    egress = EgressGuard(whitelist_domains=test_config.egress.whitelist_domains)
    approval_gate = ApprovalGate(db_path=test_db)
    balancer = ModelBalancer(config=test_config)
    harvester = ModelHarvester(config=test_config, egress_guard=egress)

    recorded_upstream_payload = {}

    # Mock egress.post to simulate upstream Groq response
    async def mock_post(url, headers=None, json_data=None, content=None):
        nonlocal recorded_upstream_payload
        recorded_upstream_payload = json_data

        # Upstream echoes back the sanitized token
        reply_content = (
            f"I have connected to server at {json_data['messages'][0]['content']} "
            "and everything is working fine."
        )
        resp_data = {
            "id": "chatcmpl-mock-123",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "llama-3.3-70b-versatile",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": reply_content,
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {"total_tokens": 42}
        }
        res = httpx.Response(200, json=resp_data)
        res.latency_ms = 45.0
        return res

    monkeypatch.setattr(egress, "post", mock_post)

    # Build test app with router
    test_app = FastAPI()
    router = create_openai_router(test_config, egress, approval_gate, balancer, harvester)
    test_app.include_router(router)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Client sends a prompt with sensitive internal IP and phone
        client_prompt = "Server 192.168.1.150 and phone 0987654321"
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "groq/llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": client_prompt}],
                "stream": False,
            }
        )

        assert resp.status_code == 200
        data = resp.json()

        # 1. Verify upstream received SANITIZED data (Zero-Trust leak prevention)
        upstream_content = recorded_upstream_payload["messages"][0]["content"]
        assert "192.168.1.150" not in upstream_content
        assert "0987654321" not in upstream_content
        assert "<REDACTED_INTERNAL_IP_" in upstream_content
        assert "<REDACTED_PHONE_" in upstream_content

        # 2. Verify client receives RESTORED original data
        assistant_reply = data["choices"][0]["message"]["content"]
        assert "192.168.1.150" in assistant_reply
        assert "0987654321" in assistant_reply
        assert "<REDACTED_" not in assistant_reply

    await egress.close()

    # Cleanup
    await asyncio.sleep(0.1)
    import os
    if os.path.exists(test_db):
        try:
            os.remove(test_db)
        except Exception:
            pass


@pytest.mark.asyncio
async def test_proxy_automatic_429_cooldown_and_fallback(monkeypatch, tmp_path):
    """
    Test that when provider A returns HTTP 429 Rate Limit, the gateway:
    1. Records cooldown in database for provider A
    2. Automatically falls back to provider B
    3. Successfully returns response from provider B to client without interruption!
    """
    config = get_config()
    test_db = str(tmp_path / "test_proxy_fallback.db")
    await storage_db.init_db(test_db)

    # Insert two models from different providers
    await storage_db.upsert_model(test_db, {
        "id": "groq/llama-3.3-70b-versatile",
        "provider": "groq",
        "model_name": "llama-3.3-70b-versatile",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 50.0,  # Lower latency -> tried first
    })
    await storage_db.upsert_model(test_db, {
        "id": "openrouter/auto",
        "provider": "openrouter",
        "model_name": "auto",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 100.0, # Higher latency -> fallback second
    })

    test_config = config.model_copy()
    test_config.db_path = test_db
    test_config.dlp.risk_action.medium = "redact"

    egress = EgressGuard(whitelist_domains=test_config.egress.whitelist_domains)
    approval_gate = ApprovalGate(db_path=test_db)
    balancer = ModelBalancer(config=test_config)
    harvester = ModelHarvester(config=test_config, egress_guard=egress)

    attempted_providers = []

    # Mock egress.post: First call (Groq) -> 429, Second call (OpenRouter) -> 200 OK
    async def mock_post(url, headers=None, json_data=None, content=None):
        if "api.groq.com" in url:
            attempted_providers.append("groq")
            r = httpx.Response(429, text="Rate limit exceeded")
            r.latency_ms = 30.0
            return r
        elif "openrouter.ai" in url:
            attempted_providers.append("openrouter")
            resp_data = {
                "id": "chatcmpl-fallback-ok",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Fallback to OpenRouter succeeded!"},
                        "finish_reason": "stop"
                    }
                ],
                "usage": {"total_tokens": 15}
            }
            r = httpx.Response(200, json=resp_data)
            r.latency_ms = 80.0
            return r
        return httpx.Response(500, text="Unknown url")

    monkeypatch.setattr(egress, "post", mock_post)

    test_app = FastAPI()
    router = create_openai_router(test_config, egress, approval_gate, balancer, harvester)
    test_app.include_router(router)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "groq/llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": "Hello!"}],
            }
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["choices"][0]["message"]["content"] == "Fallback to OpenRouter succeeded!"

        # Verify groq was tried first, then openrouter
        assert attempted_providers == ["groq", "openrouter"]

        # Verify Groq is now recorded as in 429 cooldown in SQLite
        assert await storage_db.is_provider_in_cooldown(test_db, "groq") is True

    await egress.close()

    # Cleanup
    await asyncio.sleep(0.1)
    import os
    if os.path.exists(test_db):
        try:
            os.remove(test_db)
        except Exception:
            pass


@pytest.mark.asyncio
async def test_proxy_chat_completions_streaming_sse(monkeypatch, tmp_path):
    """
    Test Server-Sent Events (stream=True) with real chunking, token split handling,
    and [DONE] termination.
    """
    config = get_config()
    test_db = str(tmp_path / "test_proxy_stream.db")
    await storage_db.init_db(test_db)

    await storage_db.upsert_model(test_db, {
        "id": "groq/llama-3.3-70b-versatile",
        "provider": "groq",
        "model_name": "llama-3.3-70b-versatile",
        "context_length": 128000,
        "is_free": True,
        "is_active": True,
        "latency_ms": 50.0,
    })

    test_config = config.model_copy()
    test_config.db_path = test_db
    test_config.dlp.risk_action.medium = "redact"

    egress = EgressGuard(whitelist_domains=test_config.egress.whitelist_domains)
    approval_gate = ApprovalGate(db_path=test_db)
    balancer = ModelBalancer(config=test_config)
    harvester = ModelHarvester(config=test_config, egress_guard=egress)

    # Mock stream_post to emit chunks
    async def mock_stream_post(url, headers=None, json_data=None):
        # Chunk 1: normal text
        yield b'data: {"choices":[{"delta":{"content":"Pinging "}}]}\n\n'
        # Chunk 2 & 3: placeholder token split across two chunks: '<REDACTED_INTER' and 'NAL_IP_1>...'
        yield b'data: {"choices":[{"delta":{"content":"<REDACTED_INTER"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"NAL_IP_1> now."}}]}\n\n'
        # Chunk 4: Done signal
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
                "messages": [{"role": "user", "content": "Check 192.168.1.55"}],
                "stream": True,
            }
        )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        body_stream = resp.text
        assert "data: [DONE]" in body_stream
        # The restored IP 192.168.1.55 must be present in the stream chunks!
        assert "192.168.1.55" in body_stream

    await egress.close()

    # Cleanup
    await asyncio.sleep(0.1)
    import os
    if os.path.exists(test_db):
        try:
            os.remove(test_db)
        except Exception:
            pass
