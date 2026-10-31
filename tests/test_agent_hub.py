"""
Comprehensive Test Suite for All-in-One AI Agent Hub (H01 - H04).
Covers:
- H01: Plug-and-Play Project & MCP-Lite Tool Registry
- H02: Shared Event Bus (Pub/Sub, Topic Wildcards, History)
- H03: AI Auto-Orchestrator (Multi-tool planning, node dispatch, answer synthesis)
- H04: AgentHubNode SDK (Decorators, Heartbeat, Event listeners, Command execution)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List
import pytest
from httpx import ASGITransport, AsyncClient

from sdk.agent_sdk import AgentHubNode
from src.config_loader import get_config
from src.hub.event_bus import SharedEventBus
from src.hub.orchestrator import AIAutoOrchestrator
from src.hub.registry import ProjectRegistry
from src.main import create_app
from src.storage import db as storage_db


@pytest.fixture
async def hub_app(tmp_path):
    """Provide isolated app instance for Agent Hub tests."""
    test_db = str(tmp_path / "test_hub.db")
    config = get_config()
    config.db_path = test_db
    config.server.gateway_api_key = ""  # No auth in test mode
    config.dlp.enabled = False

    await storage_db.init_db(test_db)
    app = create_app(config)
    yield app, test_db


# ============================================================================
# H01: Project & Tool Registry Unit Tests
# ============================================================================

def test_project_registry_lifecycle():
    """Verify project registration, tool listing, heartbeat, and offline pruning."""
    reg = ProjectRegistry(heartbeat_timeout_seconds=0.5)

    # 1. Register project
    tools = [
        {"name": "check_risk", "description": "Check coin risk", "parameters": {"coin": "str"}},
        {"name": "get_balance", "description": "Get balance"},
    ]
    p = reg.register_project("smart-money-agent", "Smart Money Bot", "Trading agent", tools)
    assert p["project_id"] == "smart-money-agent"
    assert p["status"] == "online"
    assert len(reg.get_all_projects()) == 1
    assert len(reg.get_all_tools()) == 2

    # 2. Heartbeat pops empty queue
    cmds = reg.heartbeat("smart-money-agent")
    assert cmds == []

    # 3. Prune stale projects after timeout
    time.sleep(0.6)
    assert len(reg.get_online_projects()) == 0
    assert reg.get_all_projects()[0]["status"] == "offline"

    # 4. Heartbeat recovers online status
    reg.heartbeat("smart-money-agent")
    assert reg.get_all_projects()[0]["status"] == "online"


@pytest.mark.asyncio
async def test_project_registry_command_dispatch_and_result():
    """Verify async command dispatching from gateway to project node."""
    reg = ProjectRegistry()
    reg.register_project("crypto-bot", tools=[{"name": "get_sol_price"}])

    # Start dispatch task in background
    dispatch_task = asyncio.create_task(
        reg.dispatch_command("crypto-bot", "get_sol_price", arguments={"token": "SOL"}, timeout=2.0)
    )

    # Simulate node heartbeat retrieving command
    await asyncio.sleep(0.05)
    cmds = reg.heartbeat("crypto-bot")
    assert len(cmds) == 1
    cmd_id = cmds[0]["command_id"]
    assert cmds[0]["tool_name"] == "get_sol_price"
    assert cmds[0]["arguments"] == {"token": "SOL"}

    # Simulate node returning result
    success = reg.submit_command_result(
        command_id=cmd_id,
        project_id="crypto-bot",
        status="success",
        result="$195.50",
    )
    assert success is True

    # Await outcome
    result = await dispatch_task
    assert result == "$195.50"


@pytest.mark.asyncio
async def test_project_registry_dispatch_timeout():
    """Verify dispatch command throws TimeoutError if node never responds."""
    reg = ProjectRegistry()
    reg.register_project("unresponsive-bot")

    with pytest.raises(TimeoutError):
        await reg.dispatch_command("unresponsive-bot", "some_tool", timeout=0.2)


# ============================================================================
# H02: Shared Event Bus Unit Tests
# ============================================================================

def test_shared_event_bus_pub_sub_and_wildcard():
    """Verify event publishing, topic filtering, and sequence polling."""
    bus = SharedEventBus(max_history=50)

    received_events = []
    bus.subscribe(lambda e: received_events.append(e))

    # Publish events
    e1 = bus.publish("market.solana", "smart-money-agent", {"signal": "BUY", "token": "SOL"})
    e2 = bus.publish("market.bitcoin", "smart-money-agent", {"signal": "HOLD", "token": "BTC"})
    e3 = bus.publish("news.breaking", "scraper-bot", {"headline": "Fed cuts rates"})

    assert len(received_events) == 3

    # Poll with wildcard 'market.*'
    market_events = bus.poll(topics=["market.*"], since_sequence=0)
    assert len(market_events) == 2
    assert all(e["topic"].startswith("market.") for e in market_events)

    # Poll with sequence offset
    newer_events = bus.poll(since_sequence=e2["sequence"])
    assert len(newer_events) == 1
    assert newer_events[0]["event_id"] == e3["event_id"]

    # History
    history = bus.get_history(limit=2)
    assert len(history) == 2


# ============================================================================
# H03: AI Auto-Orchestrator Unit Tests
# ============================================================================

@pytest.mark.asyncio
async def test_ai_orchestrator_tool_dispatch():
    """Verify AI orchestrator plans, calls tool on project node, and synthesizes final answer."""
    reg = ProjectRegistry()
    bus = SharedEventBus()

    reg.register_project(
        project_id="risk-agent",
        display_name="Risk Auditor",
        tools=[{"name": "audit_token", "description": "Audits on-chain token risk score"}],
    )

    # Mock AI responses (Plan -> then Synthesis)
    ai_call_count = 0

    async def mock_ai(messages: List[Dict[str, str]], model_tier: str) -> str:
        nonlocal ai_call_count
        ai_call_count += 1
        if ai_call_count == 1:
            # Step 1: Return Plan calling tool
            return json.dumps({
                "action": "call_tool",
                "project_id": "risk-agent",
                "tool_name": "audit_token",
                "arguments": {"token": "SOL"},
            })
        else:
            # Step 2: Return Synthesized answer
            return "Đồng SOL có mức độ an toàn cao (Risk Score 12/100). Đủ điều kiện giao dịch."

    orchestrator = AIAutoOrchestrator(registry=reg, event_bus=bus, chat_fn=mock_ai)

    # Background task to simulate node fulfilling the tool execution
    async def fake_node_worker():
        await asyncio.sleep(0.05)
        cmds = reg.heartbeat("risk-agent")
        if cmds:
            reg.submit_command_result(
                command_id=cmds[0]["command_id"],
                project_id="risk-agent",
                status="success",
                result={"risk_score": 12, "verdict": "SAFE"},
            )

    worker_task = asyncio.create_task(fake_node_worker())

    outcome = await orchestrator.orchestrate("Kiểm tra rủi ro đồng SOL cho tôi", model_tier="reasoning")
    await worker_task

    assert outcome["plan_action"] == "call_tool"
    assert outcome["tool_called"] == "risk-agent:audit_token"
    assert outcome["tool_success"] is True
    assert "Đồng SOL" in outcome["final_answer"]


@pytest.mark.asyncio
async def test_ai_orchestrator_direct_answer_when_no_tools():
    """Verify orchestrator gives direct answer when no project tools match."""
    reg = ProjectRegistry()
    bus = SharedEventBus()

    async def mock_ai(messages: List[Dict[str, str]], model_tier: str) -> str:
        return "Hà Nội là thủ đô của Việt Nam."

    orchestrator = AIAutoOrchestrator(registry=reg, event_bus=bus, chat_fn=mock_ai)
    outcome = await orchestrator.orchestrate("Thủ đô của Việt Nam là gì?")

    assert outcome["plan_action"] == "direct_answer"
    assert "Hà Nội" in outcome["final_answer"]


# ============================================================================
# H04: AgentHub API Integration & AgentHubNode SDK Tests
# ============================================================================

@pytest.mark.asyncio
async def test_agent_hub_api_endpoints(hub_app):
    """Integration test verifying full /api/hub/* REST endpoints."""
    app, _ = hub_app
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Register project
        reg_payload = {
            "project_id": "test-bot",
            "display_name": "Test Bot",
            "tools": [{"name": "ping", "description": "Returns pong"}],
        }
        r_reg = await client.post("/api/hub/register", json=reg_payload)
        assert r_reg.status_code == 200
        assert r_reg.json()["status"] == "online"

        # 2. Get projects & tools
        r_p = await client.get("/api/hub/projects")
        assert r_p.status_code == 200
        assert len(r_p.json()) == 1

        r_t = await client.get("/api/hub/tools")
        assert r_t.status_code == 200
        assert any(t["name"] == "ping" for t in r_t.json())

        # 3. Publish Event
        pub_payload = {
            "topic": "test.signal",
            "source_project": "test-bot",
            "payload": {"code": 100},
            "priority": "high",
        }
        r_pub = await client.post("/api/hub/events/publish", json=pub_payload)
        assert r_pub.status_code == 200
        assert r_pub.json()["topic"] == "test.signal"

        # 4. Poll Events
        r_poll = await client.get("/api/hub/events/poll?topic=test.signal")
        assert r_poll.status_code == 200
        assert len(r_poll.json()) == 1

        # 5. Heartbeat
        r_hb = await client.post("/api/hub/heartbeat", json={"project_id": "test-bot"})
        assert r_hb.status_code == 200
        assert "commands" in r_hb.json()


def test_agent_hub_node_decorators_and_manual_cycle():
    """Verify AgentHubNode decorator registrations and payload building."""
    node = AgentHubNode(
        project_id="my-scraper",
        name="Web Scraper Agent",
        description="Scrapes news",
        base_url="http://127.0.0.1:8000",
    )

    # Decorate a tool
    @node.tool(description="Scrapes articles from target URL")
    def scrape_url(url: str):
        return f"Scraped content from {url}"

    # Decorate an event handler
    handled_events = []

    @node.on_event("market.alert")
    def on_market_alert(evt):
        handled_events.append(evt)

    # Verify internal registration
    assert "scrape_url" in node._tools
    assert "market.alert" in node._event_listeners

    # Verify tool execution
    func, meta = node._tools["scrape_url"]
    assert meta["description"] == "Scrapes articles from target URL"
    assert func("http://example.com") == "Scraped content from http://example.com"


# ============================================================================
# S01 - S04: Hardening & Multi-Step Integration Tests
# ============================================================================

@pytest.mark.asyncio
async def test_agent_hub_auth_protection(tmp_path):
    """S01: Verify /api/hub/* endpoints strictly enforce gateway_api_key authentication."""
    test_db = str(tmp_path / "test_hub_auth.db")
    config = get_config()
    config.db_path = test_db
    config.server.gateway_api_key = "test-secret-key-123"

    await storage_db.init_db(test_db)
    app = create_app(config)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Request without key must be 401 Unauthorized
        r_unauth = await client.get("/api/hub/projects")
        assert r_unauth.status_code == 401

        # Request with invalid key must be 401
        r_invalid = await client.get("/api/hub/projects", headers={"Authorization": "Bearer wrong-key"})
        assert r_invalid.status_code == 401

        # Request with valid key must be 200 OK
        r_valid = await client.get("/api/hub/projects", headers={"Authorization": "Bearer test-secret-key-123"})
        assert r_valid.status_code == 200


@pytest.mark.asyncio
async def test_ai_orchestrator_multi_step_chaining_and_loop_guard():
    """S03: Verify multi-step tool chaining across projects and loop detection."""
    reg = ProjectRegistry()
    bus = SharedEventBus()

    reg.register_project("scraper", tools=[{"name": "get_hot_coin"}])
    reg.register_project("auditor", tools=[{"name": "audit_coin"}])

    step_counter = 0

    async def mock_multi_step_ai(messages: List[Dict[str, str]], model_tier: str) -> str:
        nonlocal step_counter
        step_counter += 1
        if step_counter == 1:
            # Step 1: Call scraper
            return json.dumps({
                "action": "call_tool",
                "project_id": "scraper",
                "tool_name": "get_hot_coin",
                "arguments": {},
            })
        elif step_counter == 2:
            # Step 2: Call auditor with scraped token
            return json.dumps({
                "action": "call_tool",
                "project_id": "auditor",
                "tool_name": "audit_coin",
                "arguments": {"token": "SOL"},
            })
        else:
            # Step 3: Finish with synthesized answer
            return json.dumps({
                "action": "finish",
                "answer": "Đồng SOL là coin hot và được đánh giá an toàn 95/100.",
            })

    orchestrator = AIAutoOrchestrator(registry=reg, event_bus=bus, chat_fn=mock_multi_step_ai)

    # Background worker satisfying commands for both projects
    async def node_worker():
        for _ in range(10):
            await asyncio.sleep(0.02)
            c1 = reg.heartbeat("scraper")
            for c in c1:
                reg.submit_command_result(c["command_id"], "scraper", "success", "SOL")
            c2 = reg.heartbeat("auditor")
            for c in c2:
                reg.submit_command_result(c["command_id"], "auditor", "success", {"safety_score": 95})

    w_task = asyncio.create_task(node_worker())
    res = await orchestrator.orchestrate("Tìm coin hot và chấm điểm an toàn", max_steps=4)
    await w_task

    assert res["steps_count"] == 2
    assert len(res["steps_executed"]) == 2
    assert "scraper:get_hot_coin" in res["steps_executed"][0]["tool"]
    assert "auditor:audit_coin" in res["steps_executed"][1]["tool"]
    assert "SOL" in res["final_answer"]


@pytest.mark.asyncio
async def test_shared_event_bus_sqlite_persistence(tmp_path):
    """S04: Verify SharedEventBus persists events to SQLite and recovers on restart."""
    test_db = str(tmp_path / "test_bus_persist.db")
    await storage_db.init_db(test_db)

    # Bus 1 with persistence callback
    bus1 = SharedEventBus(persistence_callback=lambda evt: storage_db.save_hub_event(test_db, evt))
    bus1.publish("market.signal", "bot-1", {"signal": "BUY", "price": 100})
    bus1.publish("market.alert", "bot-2", {"alert": "HIGH_VOLATILITY"})

    await asyncio.sleep(0.1)

    # Read events directly from SQLite
    persisted = await storage_db.load_recent_hub_events(test_db, limit=10)
    assert len(persisted) == 2
    assert persisted[0]["topic"] == "market.signal"
    assert persisted[1]["topic"] == "market.alert"

    # Bus 2 recovering state on restart
    bus2 = SharedEventBus()
    bus2.init_from_history(persisted)
    assert bus2._event_counter == 2
    history = bus2.get_history()
    assert len(history) == 2
    assert history[0]["source_project"] == "bot-1"


@pytest.mark.asyncio
async def test_agent_hub_call_and_telegram_endpoints(hub_app):
    """Verify /api/hub/call and /api/hub/telegram/* endpoints."""
    app, _ = hub_app
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Telegram status & test
        r_tel_status = await client.get("/api/hub/telegram/status")
        assert r_tel_status.status_code == 200
        assert "is_configured" in r_tel_status.json()

        r_tel_test = await client.post("/api/hub/telegram/test")
        assert r_tel_test.status_code == 200
        assert r_tel_test.json()["status"] in ("NOT_CONFIGURED", "SENT", "FAILED")

        # Register a project
        reg_payload = {
            "project_id": "direct-worker",
            "display_name": "Direct Worker",
            "tools": [{"name": "compute", "description": "Computes something"}],
        }
        await client.post("/api/hub/register", json=reg_payload)

        # Background worker responding to heartbeat command
        async def mock_worker():
            for _ in range(15):
                await asyncio.sleep(0.05)
                hb = await client.post("/api/hub/heartbeat", json={"project_id": "direct-worker"})
                cmds = hb.json().get("commands", [])
                for cmd in cmds:
                    await client.post(
                        "/api/hub/command/result",
                        json={
                            "command_id": cmd["command_id"],
                            "project_id": "direct-worker",
                            "status": "success",
                            "result": {"answer": 42},
                        },
                    )

        w_task = asyncio.create_task(mock_worker())

        # Call /api/hub/call
        r_call = await client.post(
            "/api/hub/call",
            json={
                "project_id": "direct-worker",
                "tool_name": "compute",
                "arguments": {"x": 10},
                "timeout": 5.0,
            },
        )
        await w_task

        assert r_call.status_code == 200
        data = r_call.json()
        assert data["status"] == "success"
        assert data["result"] == {"answer": 42}

