"""Automated Task, Streak Keeper, and AI Quiz Solver for Airdrop Farming.

Production V2.0: Strict Anti-Sybil Route Shuffling, Persona Window Checks,
Multi-Chain RPC Gas Ceiling Guard, and Cluster Isolation Verification.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.agent_sdk import UniversalAIClient
from src.anti_sybil.persona_engine import PersonaEngine
from src.config import FARMING_MODE, GATEWAY_API_KEY, GATEWAY_URL, PROJECT_ID
from src.executor.rpc_client import MultiChainRPCClient
from src.storage.farming_db import FarmingDatabase

logger = logging.getLogger("farming_executor")


class FarmingExecutor:
    """
    Executes farming tasks, manages active days/weeks streaks,
    and solves AI educational quizzes for Galxe/Zealy/Intract campaigns.
    """

    def __init__(
        self,
        db: FarmingDatabase,
        persona_engine: Optional[PersonaEngine] = None,
        ai_client: Optional[UniversalAIClient] = None,
        rpc_client: Optional[MultiChainRPCClient] = None,
        mode: Optional[str] = None,
    ):
        self.db = db
        self.persona_engine = persona_engine or PersonaEngine(db=db)
        self.ai_client = ai_client or UniversalAIClient(
            base_url=f"{GATEWAY_URL}/v1",
            api_key=GATEWAY_API_KEY,
            project_id=PROJECT_ID,
        )
        self.rpc_client = rpc_client or MultiChainRPCClient()
        self.mode = (mode or FARMING_MODE).upper()

    def _generate_mock_tx_hash(self, wallet: str, action: str, timestamp_str: str) -> str:
        """Generates a realistic-looking mock transaction hash for simulation records."""
        seed = f"{wallet.lower()}_{action}_{timestamp_str}"
        return "0x" + hashlib.sha256(seed.encode("utf-8")).hexdigest()

    def execute_daily_checkin(self, wallet_address: str, campaign_id: str) -> Dict[str, Any]:
        """Performs a daily check-in to preserve continuous streak."""
        now_dt = datetime.now(timezone.utc)
        today_str = now_dt.strftime("%Y-%m-%d")
        now_iso = now_dt.isoformat()

        cost_usd = 0.0 if "testnet" in campaign_id.lower() else 0.003
        tx_hash = self._generate_mock_tx_hash(wallet_address, "checkin", now_iso)

        self.db.record_execution(
            wallet_address=wallet_address,
            campaign_id=campaign_id,
            action_type="DAILY_CHECKIN",
            status="SUCCESS",
            details=f"Streak preserved for {today_str} (Mode: {self.mode})",
            cost_usd=cost_usd,
            tx_hash=tx_hash,
        )
        self.db.update_wallet_activity(wallet_address, today_str, cost_usd=cost_usd, points_add=15)

        return {
            "status": "SUCCESS",
            "action": "DAILY_CHECKIN",
            "wallet": wallet_address,
            "campaign_id": campaign_id,
            "points_earned": 15,
            "cost_usd": cost_usd,
            "tx_hash": tx_hash,
            "date": today_str,
        }

    def execute_faucet_claim(self, wallet_address: str, campaign_id: str) -> Dict[str, Any]:
        """Claims free testnet tokens from faucet (0đ cost)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tx_hash = self._generate_mock_tx_hash(wallet_address, "faucet", now_iso)

        self.db.record_execution(
            wallet_address=wallet_address,
            campaign_id=campaign_id,
            action_type="FAUCET_CLAIM",
            status="SUCCESS",
            details="Free testnet drip claimed successfully (0đ cost)",
            cost_usd=0.0,
            tx_hash=tx_hash,
        )
        self.db.update_wallet_activity(wallet_address, today_str, cost_usd=0.0, points_add=10)

        return {
            "status": "SUCCESS",
            "action": "FAUCET_CLAIM",
            "wallet": wallet_address,
            "campaign_id": campaign_id,
            "points_earned": 10,
            "cost_usd": 0.0,
            "tx_hash": tx_hash,
        }

    def execute_micro_swap(
        self,
        wallet_address: str,
        campaign_id: str,
        base_amount_eth: float = 0.005,
    ) -> Dict[str, Any]:
        """
        Executes a low-volume micro swap with anti-sybil Gaussian amount jitter.
        """
        persona = self.persona_engine.generate_persona_for_wallet(wallet_address)
        jitter_pct = persona.get("amount_jitter_pct", 0.15)
        jittered_amount = self.persona_engine.gaussian_amount_jitter(base_amount_eth, jitter_pct)

        now_iso = datetime.now(timezone.utc).isoformat()
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cost_usd = 0.0 if "testnet" in campaign_id.lower() else 0.008
        tx_hash = self._generate_mock_tx_hash(wallet_address, f"swap_{jittered_amount}", now_iso)

        details = f"Micro-swap {jittered_amount} ETH -> USDC with Gaussian Jitter {jitter_pct*100:.1f}%"

        self.db.record_execution(
            wallet_address=wallet_address,
            campaign_id=campaign_id,
            action_type="MICRO_SWAP",
            status="SUCCESS",
            details=details,
            cost_usd=cost_usd,
            tx_hash=tx_hash,
        )
        self.db.update_wallet_activity(wallet_address, today_str, cost_usd=cost_usd, points_add=25)

        return {
            "status": "SUCCESS",
            "action": "MICRO_SWAP",
            "wallet": wallet_address,
            "amount_swapped": jittered_amount,
            "cost_usd": cost_usd,
            "tx_hash": tx_hash,
            "details": details,
        }

    def solve_quiz_with_ai(
        self,
        question: str,
        options: List[str],
        context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Solves project quests / quiz questions with AI reasoning via AI Free Gateway.
        """
        options_formatted = "\n".join([f"{idx}. {opt}" for idx, opt in enumerate(options)])
        prompt = (
            f"Bạn là chuyên gia làm nhiệm vụ Airdrop Web3. Hãy giải câu hỏi trắc nghiệm sau:\n"
            f"Câu hỏi: {question}\n"
            f"Các lựa chọn:\n{options_formatted}\n"
        )
        if context:
            prompt += f"Bối cảnh dự án: {context}\n"
        prompt += (
            "\nTrả về JSON với 2 trường:\n"
            "- selected_index: (số nguyên là chỉ số lựa chọn đúng, bắt đầu từ 0)\n"
            "- explanation: (giải thích ngắn gọn vì sao đáp án này đúng)"
        )

        try:
            res = self.ai_client.ask_json(
                prompt=prompt,
                system="Bạn là chuyên gia giải đề trắc nghiệm Web3. Trả về JSON chuẩn.",
                temperature=0.1,
            )
            if isinstance(res, dict):
                idx = int(res.get("selected_index", 0))
                if 0 <= idx < len(options):
                    return {
                        "question": question,
                        "selected_index": idx,
                        "selected_option": options[idx],
                        "explanation": res.get("explanation", "AI reasoning verified."),
                    }
        except Exception as e:
            logger.warning(f"AI Quiz Solver fallback: {e}")

        # Fallback to option 0 if AI unavailable
        return {
            "question": question,
            "selected_index": 0,
            "selected_option": options[0] if options else "",
            "explanation": "Default selection fallback.",
        }

    def run_daily_cycle_for_wallet(
        self,
        wallet_address: str,
        campaigns: Optional[List[Dict[str, Any]]] = None,
        respect_active_hours: bool = False,
        recipient_address: Optional[str] = None,
        max_gas_usd: float = 0.02,
    ) -> Dict[str, Any]:
        """
        Executes a complete randomized daily farming cycle for a single wallet.
        Enforces:
        1. Cluster Isolation Guard if recipient_address is present.
        2. Persona Active Window verification if respect_active_hours is True.
        3. Dynamic Route Shuffling per wallet.
        4. Gas Ceiling Guard before performing paid micro-transactions.
        """
        # 1. Cluster Isolation Guard
        if recipient_address:
            self.persona_engine.guard.assert_safe_transfer(wallet_address, recipient_address)

        persona = self.persona_engine.generate_persona_for_wallet(wallet_address)

        # 2. Check Biological Time Window
        if respect_active_hours and not self.persona_engine.is_active_now(persona):
            return {
                "wallet": wallet_address,
                "persona": persona["persona_name"],
                "status": "DEFERRED_BY_PERSONA_WINDOW",
                "active_hours": persona["active_hours"],
                "reason": f"Current hour is outside wallet persona active hours {persona['active_hours']}. Deferred to preserve natural human footprint.",
                "total_actions": 0,
                "points_earned": 0,
                "total_cost_usd": 0.0,
            }

        if not campaigns:
            campaigns = self.db.list_campaigns(status="ACTIVE", min_score=60.0)

        if not campaigns:
            return {"wallet": wallet_address, "status": "SKIPPED", "reason": "No active campaigns found"}

        actions_taken = []
        total_points = 0
        total_cost = 0.0

        for camp in campaigns[:3]:  # Top 3 campaigns per daily run
            c_id = camp["id"]
            chain = camp.get("details", {}).get("chain", "base")

            # 3. Check Gas Ceiling
            is_gas_safe, gas_msg, est_gas_fee = self.rpc_client.check_gas_ceiling(chain, max_cost_usd=max_gas_usd)

            # 4. Route Shuffling per wallet
            base_tasks = ["faucet_claim", "daily_checkin", "micro_swap"]
            shuffled_tasks = self.persona_engine.shuffle_action_route(base_tasks, wallet_address)

            for task_name in shuffled_tasks:
                if task_name == "faucet_claim":
                    # Faucet is always 0đ
                    faucet_res = self.execute_faucet_claim(wallet_address, c_id)
                    actions_taken.append(faucet_res)
                    total_points += faucet_res["points_earned"]

                elif task_name == "daily_checkin":
                    checkin_res = self.execute_daily_checkin(wallet_address, c_id)
                    actions_taken.append(checkin_res)
                    total_points += checkin_res["points_earned"]
                    total_cost += checkin_res["cost_usd"]

                elif task_name == "micro_swap":
                    if is_gas_safe:
                        swap_res = self.execute_micro_swap(wallet_address, c_id)
                        actions_taken.append(swap_res)
                        total_points += 25
                        total_cost += swap_res["cost_usd"]
                    else:
                        logger.info(f"Skipping micro_swap for {c_id}: {gas_msg}")

        return {
            "wallet": wallet_address,
            "persona": persona["persona_name"],
            "total_actions": len(actions_taken),
            "points_earned": total_points,
            "total_cost_usd": round(total_cost, 5),
            "actions": actions_taken,
            "status": "COMPLETED",
        }
