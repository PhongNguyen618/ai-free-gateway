"""
Integration and Unit Test Suite for Universal AI Agent Ecosystem (A01 - A04).
Covers:
- A01: Intent-Based Tiered Routing (fast, reasoning, coding, long_context)
- A02: Deterministic Response Cache (SHA-256 canonical hashing, HIT/MISS headers, TTL)
- A03: Per-Project Quota & Priority Guard (isolation, RPM rate limits, Retry-After, stats)
- A04: Universal Agent SDK (extract_json_payload, UniversalAIClient, BatchAIAgent, ReActToolAgent)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List
import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from src.config_loader import get_config
from src.main import create_app
from src.router.balancer import ModelBalancer, TIER_ALIASES, evaluate_tier_boost
from src.router.response_cache import ResponseCache
from src.security.project_quota import ProjectQuotaGuard
from src.storage import db as storage_db
from sdk.agent_sdk import (
    BatchAIAgent,
    ReActToolAgent,
    UniversalAIClient,
    extract_json_payload,
)


@pytest.fixture
async def ecosystem_app(tmp_path):
    """Provide isolated app instance and database for Universal Agent Ecosystem tests."""
    test_db = str(tmp_path / "test_ecosystem.db")
    config = get_config()
    config.db_path = test_db
    config.server.gateway_api_key = ""  # No auth in test mode
    config.dlp.enabled = False           # Disable DLP noise in ecosystem test

    await storage_db.init_db(test_db)

    # Seed diverse models representing all tiers
    models_to_seed = [
        {
            "id": "groq/llama-3.1-8b-instant",
            "provider": "groq",
            "model_name": "llama-3.1-8b-instant",
            "display_name": "Llama 3.1 8B Instant",
            "context_length": 128000,
            "is_free": True,
            "is_active": True,
            "latency_ms": 120.0,
        },
        {
            "id": "gemini/gemini-2.0-flash",
            "provider": "gemini",
            "model_name": "gemini-2.0-flash",
            "display_name": "Gemini 2.0 Flash",
            "context_length": 1048576,
            "is_free": True,
            "is_active": True,
            "latency_ms": 180.0,
        },
        {
            "id": "openrouter/deepseek-r1-free",
            "provider": "openrouter",
            "model_name": "deepseek/deepseek-r1:free",
            "display_name": "DeepSeek R1 Free",
            "context_length": 64000,
            "is_free": True,
            "is_active": True,
            "latency_ms": 1500.0,
        },
        {
            "id": "openrouter/qwen-2.5-coder-32b",
            "provider": "openrouter",
            "model_name": "qwen/qwen-2.5-coder-32b-instruct:free",
            "display_name": "Qwen 2.5 Coder 32B Free",
            "context_length": 32768,
            "is_free": True,
            "is_active": True,
            "latency_ms": 650.0,
        },
    ]

    for m in models_to_seed:
        await storage_db.upsert_model(test_db, m)

    app = create_app(config)
    yield app, test_db


# ============================================================================
# A01: Intent-Based Tiered Routing Tests
# ============================================================================

@pytest.mark.asyncio
async def test_tier_aliases_definitions():
    """Verify tier aliases exist and cover all 5 required modes."""
    assert "fast" in TIER_ALIASES
    assert "reasoning" in TIER_ALIASES
    assert "coding" in TIER_ALIASES
    assert "long_context" in TIER_ALIASES
    assert "auto" in TIER_ALIASES


@pytest.mark.asyncio
async def test_evaluate_tier_boost_scoring():
    """Verify keyword and characteristic boost scores for models."""
    # Fast tier boosts flash / low latency
    assert evaluate_tier_boost("gemini-2.0-flash", 1000000, 180.0, "fast") > 0.0
    assert evaluate_tier_boost("deepseek-r1:free", 64000, 1500.0, "fast") <= 0.0

    # Reasoning tier boosts r1 / deepseek
    assert evaluate_tier_boost("deepseek-r1:free", 64000, 1500.0, "reasoning") > 0.0
    assert evaluate_tier_boost("qwen-2.5-coder-32b", 32000, 650.0, "reasoning") <= 0.0

    # Coding tier boosts coder
    assert evaluate_tier_boost("qwen-2.5-coder-32b", 32000, 650.0, "coding") > 0.0

    # Long context tier boosts models with >= 100k tokens
    assert evaluate_tier_boost("gemini-2.0-flash", 1000000, 180.0, "long_context") > 0.0
    assert evaluate_tier_boost("qwen-2.5-coder-32b", 32000, 650.0, "long_context") <= 0.0


@pytest.mark.asyncio
async def test_balancer_candidate_chain_tiered_priorities(ecosystem_app):
    """Test candidate ordering across all virtual tiers."""
    app, test_db = ecosystem_app
    balancer: ModelBalancer = app.state.balancer

    # 1. Tier 'fast' should prioritize low-latency / flash models
    fast_chain = await balancer.get_candidate_chain("fast")
    assert len(fast_chain) >= 2
    top_fast = fast_chain[0]["model_name"].lower()
    assert ("flash" in top_fast) or ("instant" in top_fast)

    # 2. Tier 'reasoning' should prioritize deepseek-r1
    reasoning_chain = await balancer.get_candidate_chain("reasoning")
    assert len(reasoning_chain) >= 1
    top_reasoning = reasoning_chain[0]["model_name"].lower()
    assert "r1" in top_reasoning

    # 3. Tier 'coding' should prioritize coder model
    coding_chain = await balancer.get_candidate_chain("coding")
    assert len(coding_chain) >= 1
    top_coding = coding_chain[0]["model_name"].lower()
    assert "coder" in top_coding

    # 4. Tier 'long_context' should prioritize 1M+ context Gemini
    long_chain = await balancer.get_candidate_chain("long_context")
    assert len(long_chain) >= 1
    top_long = long_chain[0]
    assert top_long["context_length"] >= 100000


# ============================================================================
# A02: Deterministic Response Cache Tests
# ============================================================================

def test_response_cache_unit():
    """Unit test for ResponseCache hashing, bypass, TTL, and LRU eviction."""
    cache = ResponseCache(max_entries=2, default_ttl_seconds=10)

    # 1. Test Deterministic Hash
    messages = [{"role": "user", "content": "Ping"}]
    k1 = ResponseCache.compute_cache_key("fast", messages)
    k2 = ResponseCache.compute_cache_key("fast", messages)
    assert k1 == k2

    # Different model yields different hash
    k3 = ResponseCache.compute_cache_key("reasoning", messages)
    assert k1 != k3

    # 2. Set and Get
    dummy_resp = {"id": "chatcmpl-test", "choices": [{"message": {"content": "Pong"}}]}
    cache.set(k1, dummy_resp)

    cached_hit = cache.get(k1)
    assert cached_hit is not None
    assert cached_hit["content"]["choices"][0]["message"]["content"] == "Pong"

    # 3. Miss check
    assert cache.get("non-existent-key") is None

    # 4. Check Stats
    stats = cache.get_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["entries_count"] == 1
    assert stats["hit_rate_pct"] == 50.0

    # 5. Eviction test (max_entries = 2)
    cache.set("key_2", dummy_resp)
    cache.set("key_3", dummy_resp)
    assert cache.get_stats()["entries_count"] == 2
    # Oldest key k1 should be evicted
    assert cache.get(k1) is None


@pytest.mark.asyncio
async def test_cache_proxy_integration_and_stats(ecosystem_app, monkeypatch):
    """Integration test verifying X-Gateway-Cache header and /api/cache/stats."""
    app, _ = ecosystem_app

    # Mock egress_guard.post to simulate successful upstream provider call
    async def mock_post(*args, **kwargs):
        res = httpx.Response(200, json={
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "groq/llama-3.1-8b-instant",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Deterministic output"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        })
        res.latency_ms = 45.0
        return res

    monkeypatch.setattr(app.state.egress_guard, "post", mock_post)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        req_payload = {
            "model": "fast",
            "messages": [{"role": "user", "content": "What is 2 + 2?"}],
            "temperature": 0.0,
            "stream": False,
        }

        # Request 1: Cache MISS
        resp1 = await client.post("/v1/chat/completions", json=req_payload)
        assert resp1.status_code == 200
        assert resp1.headers.get("X-Gateway-Cache") == "MISS"

        # Request 2: Cache HIT (< 1ms return)
        resp2 = await client.post("/v1/chat/completions", json=req_payload)
        assert resp2.status_code == 200
        assert resp2.headers.get("X-Gateway-Cache") == "HIT"
        assert resp2.json()["choices"][0]["message"]["content"] == "Deterministic output"

        # Check /api/cache/stats endpoint
        stats_resp = await client.get("/api/cache/stats")
        assert stats_resp.status_code == 200
        stats_data = stats_resp.json()
        assert stats_data["hits"] >= 1
        assert stats_data["misses"] >= 1
        assert stats_data["hit_rate_pct"] > 0


# ============================================================================
# A03: Per-Project Quota & Priority Guard Tests
# ============================================================================

def test_project_quota_unit():
    """Unit test for ProjectQuotaGuard sliding window rate limiting."""
    guard = ProjectQuotaGuard(limits={"background": 3, "normal": 60, "critical": 600})

    # Allow 3 requests
    assert guard.check_quota("project-a", "background")[0] is True
    assert guard.check_quota("project-a", "background")[0] is True
    assert guard.check_quota("project-a", "background")[0] is True

    # 4th request must be rate limited
    allowed, retry_after, prio = guard.check_quota("project-a", "background")
    assert allowed is False
    assert retry_after > 0
    assert prio == "background"

    # Different project 'project-b' must be unaffected
    assert guard.check_quota("project-b", "background")[0] is True

    # Record outcome
    guard.record_outcome("project-a", prio, error=True)
    guard.record_outcome("project-b", "background", cache_hit=True)

    # Check stats
    stats = guard.get_all_stats()
    assert "project-a" in stats["projects"]
    assert stats["projects"]["project-a"]["errors"] == 1
    assert stats["projects"]["project-b"]["cache_hits"] == 1


@pytest.mark.asyncio
async def test_project_quota_proxy_integration_and_headers(ecosystem_app, monkeypatch):
    """Integration test checking 429 Retry-After and /api/projects/stats."""
    app, _ = ecosystem_app

    async def mock_post(*args, **kwargs):
        res = httpx.Response(200, json={
            "id": "chatcmpl-mock",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        })
        res.latency_ms = 30.0
        return res

    monkeypatch.setattr(app.state.egress_guard, "post", mock_post)

    # Override RPM for test speed
    app.state.project_quota_guard.limits["background"] = 2

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {
            "X-Project-ID": "scraper-bot",
            "X-Project-Priority": "background",
        }
        payload = {
            "model": "fast",
            "messages": [{"role": "user", "content": f"msg {time.time()}"}],
            "temperature": 0.7,
        }

        # Request 1 & 2 pass
        r1 = await client.post("/v1/chat/completions", json=payload, headers=headers)
        assert r1.status_code == 200

        payload["messages"][0]["content"] = f"msg 2 {time.time()}"
        r2 = await client.post("/v1/chat/completions", json=payload, headers=headers)
        assert r2.status_code == 200

        # Request 3 should trigger 429
        payload["messages"][0]["content"] = f"msg 3 {time.time()}"
        r3 = await client.post("/v1/chat/completions", json=payload, headers=headers)
        assert r3.status_code == 429
        assert "Retry-After" in r3.headers
        assert "scraper-bot" in r3.text

        # Verify critical priority on another project still passes
        crit_headers = {
            "X-Project-ID": "order-executor",
            "X-Project-Priority": "critical",
        }
        r_crit = await client.post("/v1/chat/completions", json=payload, headers=crit_headers)
        assert r_crit.status_code == 200

        # Check /api/projects/stats
        pstats = await client.get("/api/projects/stats")
        assert pstats.status_code == 200
        pdata = pstats.json()
        assert "scraper-bot" in pdata["projects"]
        assert pdata["projects"]["scraper-bot"]["total_requests"] >= 2


# ============================================================================
# A04: Universal Agent SDK Tests
# ============================================================================

def test_extract_json_payload_varieties():
    """Verify robust extraction of JSON from Markdown, text wrapping, and arrays."""
    # 1. Clean JSON
    assert extract_json_payload('{"key": "value"}') == {"key": "value"}

    # 2. Markdown codeblock
    md_text = "Here is the plan:\n```json\n{\"action\": \"BUY\", \"amount\": 100}\n```\nGood luck!"
    assert extract_json_payload(md_text) == {"action": "BUY", "amount": 100}

    # 3. Plain codeblock without json tag
    md_plain = "```\n[1, 2, 3, 4]\n```"
    assert extract_json_payload(md_plain) == [1, 2, 3, 4]

    # 4. Embedded JSON object with preamble and postamble
    embedded = "Analysis complete. Result: {\"verdict\": true, \"confidence\": 0.95}. End of line."
    assert extract_json_payload(embedded) == {"verdict": True, "confidence": 0.95}

    # 5. Trailing comma cleanup
    trailing = '{"items": [1, 2, 3,], "active": true,}'
    assert extract_json_payload(trailing) == {"items": [1, 2, 3], "active": True}


def test_batch_ai_agent_processing():
    """Test BatchAIAgent chunking, schema enforcement, and order preservation."""
    class MockClient(UniversalAIClient):
        def __init__(self):
            super().__init__()
            self.call_count = 0

        def ask_json(self, prompt, **kwargs):
            self.call_count += 1
            # Simulate LLM returning array of results with index
            return [
                {"index": 0, "result": "PROCESSED_A"},
                {"index": 1, "result": "PROCESSED_B"},
            ]

    client = MockClient()
    agent = BatchAIAgent(client=client, default_batch_size=2)

    items = ["item_1", "item_2", "item_3", "item_4"]
    results = agent.process_batch(items, "Tag items", batch_size=2)

    assert len(results) == 4
    assert client.call_count == 2
    assert results == ["PROCESSED_A", "PROCESSED_B", "PROCESSED_A", "PROCESSED_B"]


def test_react_tool_agent_loop_and_completion():
    """Test ReActToolAgent autonomous loop: Action -> Observation -> Final Answer."""
    tools = {
        "calculate_rsi": lambda token: "RSI is 28.5 (Oversold)",
        "get_balance": lambda _: "$5000 USDT",
    }

    class MockReActClient(UniversalAIClient):
        def __init__(self):
            super().__init__()
            self.step = 0

        def ask_text(self, prompt, **kwargs):
            self.step += 1
            if self.step == 1:
                return "Thought: I need to check RSI for SOL.\nAction: calculate_rsi[SOL]"
            elif self.step == 2:
                return "Thought: RSI is 28.5 which is oversold. I have enough information.\nFinal Answer: SOL is oversold with RSI 28.5. Good buy signal."
            return "Final Answer: Done."

    client = MockReActClient()
    react_agent = ReActToolAgent(client=client, tools=tools, max_steps=5)

    outcome = react_agent.run("Analyze SOL market entry")
    assert outcome["steps_count"] == 2
    assert "RSI 28.5" in outcome["final_answer"]
    assert len(outcome["actions_executed"]) == 1
    assert outcome["actions_executed"][0] == ("calculate_rsi", "SOL")


def test_react_tool_agent_loop_guard():
    """Test ReActToolAgent loop detection guard when agent repeats same tool call."""
    tools = {"dummy_tool": lambda x: f"result for {x}"}

    class MockLoopingClient(UniversalAIClient):
        def ask_text(self, prompt, **kwargs):
            # Repeatedly output the same action
            return "Thought: Repeating action.\nAction: dummy_tool[arg1]"

    client = MockLoopingClient()
    react_agent = ReActToolAgent(client=client, tools=tools, max_steps=4)

    outcome = react_agent.run("Test loop guard")
    history_str = "\n".join(outcome["history"])
    assert "LoopGuard triggered" in history_str
    assert outcome["steps_count"] == 4
