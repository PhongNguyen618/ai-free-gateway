"""Airdrop Reward Harvester and Capital Router to Smart Money Agent."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

try:
    from ecosystem.airdrop_hunter.src.storage.farming_db import FarmingDatabase
except ImportError:
    from src.storage.farming_db import FarmingDatabase



logger = logging.getLogger("reward_harvester")


class RewardHarvester:
    """
    Monitors airdrop reward allocations, records claims,
    and automatically routes profit capital (80%) to the smart-money-agent.
    """

    def __init__(self, db: FarmingDatabase, hub_node: Optional[Any] = None):
        self.db = db
        self.hub_node = hub_node

    def audit_real_onchain_balances(self, wallet_address: str) -> Dict[str, Any]:
        """Queries live on-chain balances (Native ETH, WETH, USDC) directly from Base L2 RPC."""
        import json
        import urllib.request
        from ecosystem.airdrop_hunter.src.executor.onchain_signer import BASE_RPC_URL, BASE_WETH_ADDRESS

        usdc_addr = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        clean_addr = wallet_address.lower().strip()
        result = {
            "wallet": clean_addr,
            "eth_balance": 0.0,
            "weth_balance": 0.0,
            "usdc_balance": 0.0,
            "live_verified": False,
        }

        try:
            # 1. Native ETH
            req_eth = urllib.request.Request(
                BASE_RPC_URL,
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [clean_addr, "latest"]}).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "AIFreeGateway/1.0"},
            )
            with urllib.request.urlopen(req_eth, timeout=5.0) as resp:
                eth_hex = json.loads(resp.read().decode("utf-8")).get("result", "0x0")
                result["eth_balance"] = round(int(eth_hex, 16) / 1e18, 6)

            # 2. WETH Balance
            padded_addr = clean_addr[2:].rjust(64, "0")
            req_weth = urllib.request.Request(
                BASE_RPC_URL,
                data=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "eth_call", "params": [{"to": BASE_WETH_ADDRESS, "data": "0x70a08231" + padded_addr}, "latest"]}).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "AIFreeGateway/1.0"},
            )
            with urllib.request.urlopen(req_weth, timeout=5.0) as resp:
                weth_hex = json.loads(resp.read().decode("utf-8")).get("result", "0x0")
                result["weth_balance"] = round(int(weth_hex, 16) / 1e18, 6)

            # 3. USDC Balance (6 decimals)
            req_usdc = urllib.request.Request(
                BASE_RPC_URL,
                data=json.dumps({"jsonrpc": "2.0", "id": 3, "method": "eth_call", "params": [{"to": usdc_addr, "data": "0x70a08231" + padded_addr}, "latest"]}).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "AIFreeGateway/1.0"},
            )
            with urllib.request.urlopen(req_usdc, timeout=5.0) as resp:
                usdc_hex = json.loads(resp.read().decode("utf-8")).get("result", "0x0")
                result["usdc_balance"] = round(int(usdc_hex, 16) / 1e6, 2)

            result["live_verified"] = True
        except Exception as e:
            result["error"] = str(e)

        return result

    def evaluate_wallet_eligibility(
        self,
        wallet: Dict[str, Any],
        campaign: Dict[str, Any],
    ) -> Tuple[bool, float, str]:
        """
        Determines if a wallet meets airdrop criteria based on verified on-chain activity.
        Returns (is_eligible, estimated_value_usd, reason).
        Fail-Closed: Tuyệt đối không bịa ra số USD ảo khi chưa claim token thực tế.
        """
        active_days = wallet.get("active_days", 0)
        points = wallet.get("points_accumulated", 0)
        alpha_score = campaign.get("alpha_score", 0.0)

        if active_days >= 3 and points >= 50 and alpha_score >= 80.0:
            return True, 0.0, f"Đạt chuẩn xếp hạng Tier-1 ({active_days} ngày on-chain, {points} điểm)"
        elif active_days >= 1 and points >= 20:
            return True, 0.0, f"Đạt chuẩn xếp hạng Tier-3 ({active_days} ngày on-chain)"
        else:
            return False, 0.0, "Chưa đủ điều kiện streak (cần >=1 ngày active và >=20 điểm)"


    def record_discovered_reward(
        self,
        wallet_address: str,
        campaign_id: str,
        token_symbol: str,
        amount_tokens: float,
        value_usd: float,
    ) -> int:
        """Stores a claimable reward in the database."""
        reward_id = self.db.record_reward(
            wallet_address=wallet_address,
            campaign_id=campaign_id,
            token_symbol=token_symbol,
            amount_tokens=amount_tokens,
            value_usd=value_usd,
        )
        logger.info(f"Recorded reward ID #{reward_id}: {amount_tokens} {token_symbol} (${value_usd}) for {wallet_address[:8]}...")
        return reward_id

    def route_capital_to_smart_money(
        self,
        profit_share_pct: float = 0.80,
    ) -> Dict[str, Any]:
        """
        Processes unrouted rewards, splitting:
        - 80% capital to smart-money-agent for meme hunting
        - 20% retained for gas buffer in airdrop-hunter-agent
        Publishes the 'airdrop.capital_routed' event to AI Free Gateway Event Bus.
        """
        unrouted = self.db.list_rewards(unrouted_only=True)
        if not unrouted:
            return {
                "status": "NO_NEW_REWARDS",
                "processed_count": 0,
                "total_value_usd": 0.0,
                "routed_to_meme_bot_usd": 0.0,
                "retained_gas_usd": 0.0,
            }

        total_value = sum(r.get("value_usd", 0.0) for r in unrouted)
        routed_total = round(total_value * profit_share_pct, 2)
        retained_total = round(total_value * (1.0 - profit_share_pct), 2)

        for r in unrouted:
            r_val = r.get("value_usd", 0.0)
            r_routed = round(r_val * profit_share_pct, 2)
            r_gas = round(r_val * (1.0 - profit_share_pct), 2)
            self.db.mark_reward_routed(
                reward_id=r["id"],
                routed_amount_usd=r_routed,
                retained_gas_usd=r_gas,
            )

        # Notify Gateway Hub via Event Bus
        if self.hub_node and hasattr(self.hub_node, "publish_event"):
            try:
                self.hub_node.publish_event(
                    topic="airdrop.capital_routed",
                    payload={
                        "target_agent": "smart-money-agent",
                        "capital_added_usd": routed_total,
                        "retained_gas_usd": retained_total,
                        "rewards_count": len(unrouted),
                        "message": f"Airdrop profits harvested! Pumped ${routed_total} to smart-money-agent for meme sniping.",
                    },
                    priority="high",
                )
            except Exception as e:
                logger.debug(f"Event broadcast failed: {e}")

        return {
            "status": "SUCCESS",
            "processed_count": len(unrouted),
            "total_value_usd": round(total_value, 2),
            "routed_to_meme_bot_usd": routed_total,
            "retained_gas_usd": retained_total,
            "profit_split_ratio": f"{int(profit_share_pct*100)}% Meme / {int((1-profit_share_pct)*100)}% Gas",
        }
