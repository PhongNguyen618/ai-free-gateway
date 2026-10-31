"""Agent Hub Bridge for Airdrop Hunter Agent.

Connects the Airdrop Hunter Agent directly into the AI Free Gateway All-in-One Hub
using AgentHubNode and exposes 6 high-value MCP-Lite production tools.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Optional

from src.agent_sdk import AgentHubNode
from src.anti_sybil.persona_engine import PersonaEngine
from src.config import DB_PATH, FARMING_MODE, GATEWAY_API_KEY, GATEWAY_URL, PROJECT_ID
from src.executor.farming_executor import FarmingExecutor
from src.executor.reward_harvester import RewardHarvester
from src.executor.rpc_client import MultiChainRPCClient
from src.radar.airdrop_radar import AirdropRadar
from src.storage.farming_db import FarmingDatabase

logger = logging.getLogger("hub_bridge")

_node_instance: Optional[AgentHubNode] = None
_lock = threading.Lock()


def get_farming_components(custom_db_path: Optional[str] = None):
    db = FarmingDatabase(custom_db_path or DB_PATH)
    radar = AirdropRadar(db=db)
    persona = PersonaEngine(db=db)
    rpc_client = MultiChainRPCClient()
    executor = FarmingExecutor(db=db, persona_engine=persona, rpc_client=rpc_client)
    return db, radar, persona, rpc_client, executor


def build_hub_node(
    hub_url: Optional[str] = None,
    api_key: Optional[str] = None,
    db_path: Optional[str] = None,
) -> AgentHubNode:
    """Builds and registers the AgentHubNode for Airdrop Hunter."""
    url = (hub_url or GATEWAY_URL).rstrip("/")
    key = api_key or GATEWAY_API_KEY
    db, radar, persona_engine, rpc_client, executor = get_farming_components(db_path)

    # Initialize seed campaigns in database
    radar.seed_initial_campaigns()

    node = AgentHubNode(
        project_id=PROJECT_ID,
        name="Bot Săn Airdrop & Anti-Sybil",
        description="Tự động săn Airdrop 0đ, giữ chuỗi Active Days & chống Sybil tạo vốn nuôi Meme bot",
        base_url=url,
        api_key=key,
    )
    harvester = RewardHarvester(db=db, hub_node=node)

    # Tool 1: Khám phá kèo Airdrop ngon (Hỗ trợ Live DeFiLlama)
    @node.tool(
        name="discover_top_airdrops",
        description="Quét và xếp hạng các cơ hội Airdrop 0đ / vốn nhỏ tiềm năng cao nhất (Testnet, Daily Points, DeFiLlama Live Tokenless).",
    )
    def tool_discover_top_airdrops(
        min_score: float = 70.0,
        category: str = "ALL",
        include_live_defillama: bool = True,
    ) -> Dict[str, Any]:
        min_score = float(min_score)
        cat = (category or "ALL").upper()
        all_campaigns = radar.scan_and_evaluate(include_live_defillama=include_live_defillama)
        filtered = [c for c in all_campaigns if c.get("alpha_score", 0) >= min_score]
        if cat != "ALL":
            filtered = [c for c in filtered if c.get("category") == cat]

        # Publish event to Hub
        if filtered:
            top = filtered[0]
            try:
                node.publish_event(
                    topic="airdrop.alpha_opportunity",
                    payload={
                        "top_campaign": top["name"],
                        "alpha_score": top["alpha_score"],
                        "cost_type": top.get("cost_type"),
                        "total_found": len(filtered),
                    },
                )
            except Exception as e:
                logger.debug(f"Event publish failed (non-critical): {e}")

        return {
            "total_found": len(filtered),
            "min_score": min_score,
            "campaigns": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "alpha_score": c["alpha_score"],
                    "category": c["category"],
                    "cost_type": c["cost_type"],
                    "tier1_backers": c.get("tier1_backers", []),
                }
                for c in filtered
            ],
        }

    # Tool 2: Lập kế hoạch chống Sybil
    @node.tool(
        name="generate_anti_sybil_plan",
        description="Lập lịch trình hành động ngẫu nhiên hóa chống Sybil cho dàn ví (đảo lộ trình, khung giờ sinh học, chống chuyển chéo).",
    )
    def tool_generate_anti_sybil_plan(wallets: Optional[List[str]] = None) -> Dict[str, Any]:
        target_wallets = wallets or []
        if not target_wallets:
            stored = db.list_wallets()
            target_wallets = [w["address"] for w in stored] or [
                "0x71c6bf00a49be0834277b0292bfef32342ef0123",
                "0x89d24a6b4ccb1b6faa2625fe562bdd9a23260456",
            ]

        sample_tasks = ["faucet_claim", "daily_checkin", "micro_swap"]
        plan = persona_engine.plan_daily_schedule(target_wallets, sample_tasks)
        return {
            "wallets_count": len(plan),
            "safety_verdict": "CLUSTER_ISOLATION_ACTIVE",
            "schedule": plan,
        }

    # Tool 3: Chạy chu kỳ cày hàng ngày (Có bảo vệ Gas & Khung giờ Persona)
    @node.tool(
        name="run_daily_farming_cycle",
        description="Chạy chu kỳ điểm danh, giữ chuỗi Active Days và làm nhiệm vụ Airdrop cho 1 hoặc toàn bộ dàn ví.",
    )
    def tool_run_daily_farming_cycle(
        target_wallet: str = "ALL",
        respect_active_hours: bool = False,
        max_gas_usd: float = 0.02,
    ) -> Dict[str, Any]:
        target = (target_wallet or "ALL").strip()
        wallets_to_run = []
        if target.upper() == "ALL":
            stored = db.list_wallets()
            if stored:
                wallets_to_run = [w["address"] for w in stored]
            else:
                demo_wallets = [
                    "0x71c6bf00a49be0834277b0292bfef32342ef0123",
                    "0x89d24a6b4ccb1b6faa2625fe562bdd9a23260456",
                ]
                for dw in demo_wallets:
                    db.upsert_wallet(dw)
                wallets_to_run = demo_wallets
        else:
            db.upsert_wallet(target)
            wallets_to_run = [target]

        results = []
        total_points = 0
        total_cost = 0.0

        for w in wallets_to_run:
            res = executor.run_daily_cycle_for_wallet(
                w,
                respect_active_hours=respect_active_hours,
                max_gas_usd=max_gas_usd,
            )
            results.append(res)
            total_points += res.get("points_earned", 0)
            total_cost += res.get("total_cost_usd", 0.0)

        # Notify hub of completed cycle
        try:
            node.publish_event(
                topic="airdrop.cycle_completed",
                payload={
                    "wallets_farmed": len(wallets_to_run),
                    "total_points_earned": total_points,
                    "total_cost_usd": total_cost,
                },
            )
        except Exception as e:
            logger.debug(f"Event publish failed: {e}")

        return {
            "status": "SUCCESS",
            "mode": FARMING_MODE,
            "wallets_processed": len(wallets_to_run),
            "total_points_earned": total_points,
            "total_cost_usd": round(total_cost, 5),
            "details": results,
        }

    # Tool 4: Xem thống kê tổng hợp
    @node.tool(
        name="get_farming_stats",
        description="Xem thống kê tổng quan: số chiến dịch đang theo dõi, số ví, chuỗi ngày active, tổng điểm tích lũy và doanh thu Airdrop.",
    )
    def tool_get_farming_stats() -> Dict[str, Any]:
        summary = db.get_summary()
        summary["operating_mode"] = FARMING_MODE
        summary["gateway_hub_connected"] = True
        return summary

    # Tool 5: Thu hoạch Airdrop & Bơm vốn sang Smart Money Agent
    @node.tool(
        name="check_and_route_airdrop_rewards",
        description="Kiểm tra phần thưởng Airdrop nhận được và tự động trích 80% lợi nhuận bơm vốn sang smart-money-agent để săn Meme.",
    )
    def tool_check_and_route_airdrop_rewards(profit_share_pct: float = 0.80) -> Dict[str, Any]:
        route_res = harvester.route_capital_to_smart_money(profit_share_pct=profit_share_pct)
        return route_res

    # Tool 6: Kiểm tra sức khỏe on-chain của ví & Gas thời gian thực
    @node.tool(
        name="check_wallet_onchain_health",
        description="Kiểm tra số dư RPC thật, nonce giao dịch, phí gas mạng lưới thật và chỉ số an toàn Anti-Sybil của từng ví.",
    )
    def tool_check_wallet_onchain_health(
        wallet_address: str = "ALL",
        chain: str = "base",
    ) -> Dict[str, Any]:
        chain_clean = chain.lower().strip()
        gas_price, is_live_gas = rpc_client.get_gas_price_gwei(chain_clean)
        est_cost, _ = rpc_client.estimate_tx_cost_usd(chain_clean)

        wallets_to_check = []
        if wallet_address.upper() == "ALL":
            stored = db.list_wallets()
            wallets_to_check = [w["address"] for w in stored] or ["0x71c6bf00a49be0834277b0292bfef32342ef0123"]
        else:
            wallets_to_check = [wallet_address]

        health_reports = []
        for addr in wallets_to_check:
            bal, is_live_bal = rpc_client.get_native_balance(chain_clean, addr)
            nonce, _ = rpc_client.get_transaction_count(chain_clean, addr)
            persona = persona_engine.generate_persona_for_wallet(addr)
            is_active = persona_engine.is_active_now(persona)

            health_reports.append({
                "address": addr,
                "chain": chain_clean,
                "balance": bal,
                "transaction_count": nonce,
                "persona_archetype": persona["persona_name"],
                "is_active_window_now": is_active,
                "is_live_rpc": is_live_bal,
            })

        return {
            "chain": chain_clean,
            "network_gas_price_gwei": gas_price,
            "estimated_tx_cost_usd": est_cost,
            "is_live_network_data": is_live_gas,
            "wallets": health_reports,
        }

    return node


def start_hub_bridge(
    hub_url: Optional[str] = None,
    api_key: Optional[str] = None,
    background: bool = True,
) -> Optional[AgentHubNode]:
    """Starts the AgentHubNode background worker connecting to ai-free-gateway."""
    global _node_instance
    with _lock:
        if _node_instance is not None:
            return _node_instance

        try:
            node = build_hub_node(hub_url=hub_url, api_key=api_key)
            if background:
                node.start_background(poll_interval=2.5)
                logger.info("Airdrop Hunter Agent Hub Bridge started in background thread.")
            else:
                node.register()
            _node_instance = node
            return _node_instance
        except Exception as e:
            logger.warning(f"Could not connect to Agent Hub at {hub_url or GATEWAY_URL}: {e}")
            return None
