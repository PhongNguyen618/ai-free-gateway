"""Airdrop Reward Harvester and Capital Router to Smart Money Agent."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

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

    def evaluate_wallet_eligibility(
        self,
        wallet: Dict[str, Any],
        campaign: Dict[str, Any],
    ) -> Tuple[bool, float, str]:
        """
        Determines if a wallet meets airdrop claim criteria.
        Returns (is_eligible, estimated_value_usd, reason).
        """
        active_days = wallet.get("active_days", 0)
        points = wallet.get("points_accumulated", 0)
        alpha_score = campaign.get("alpha_score", 0.0)

        # Realistic criteria: At least 3 active days or 50 points in high-alpha projects
        if active_days >= 3 and points >= 50 and alpha_score >= 80.0:
            est_value = round(120.0 + (active_days * 15.5) + (points * 0.25), 2)
            return True, est_value, f"High streak qualify ({active_days} days, {points} pts)"
        elif active_days >= 1 and points >= 20:
            est_value = round(25.0 + (points * 0.10), 2)
            return True, est_value, f"Tier-3 allocation qualify ({active_days} days)"
        else:
            return False, 0.0, f"Insufficient streak (need >=1 active day and >=20 pts)"

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
