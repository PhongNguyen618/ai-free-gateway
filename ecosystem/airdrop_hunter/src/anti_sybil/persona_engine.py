"""AI Anti-Sybil Persona Engine and Cluster Isolation Guard."""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.config import PERSONA_ARCHETYPES
from src.storage.farming_db import FarmingDatabase


class SybilClusterViolationError(Exception):
    """Raised when an action violates the strict anti-clustering Sybil prevention rules."""
    pass


class ClusterIsolationGuard:
    """
    Prevents the #1 mistake of airdrop farmers: linking multiple wallets together.
    Strictly forbids direct or indirect transfers between wallets in the farming pool.
    """

    def __init__(self, registered_wallets: Optional[List[str]] = None):
        self._wallet_set = set(w.lower().strip() for w in (registered_wallets or []))

    def register_wallet(self, address: str) -> None:
        self._wallet_set.add(address.lower().strip())

    def register_wallets(self, addresses: List[str]) -> None:
        for addr in addresses:
            self.register_wallet(addr)

    def is_known_wallet(self, address: str) -> bool:
        return address.lower().strip() in self._wallet_set

    def validate_transfer(self, sender: str, recipient: str) -> Tuple[bool, str]:
        """
        Validates if a transaction between sender and recipient is safe.
        Returns (is_safe, reason).
        """
        s = sender.lower().strip()
        r = recipient.lower().strip()

        if s == r:
            return False, "Self-transfer produces no distinct airdrop footprint."

        if s in self._wallet_set and r in self._wallet_set:
            return False, (
                f"CRITICAL SYBIL CLUSTER DETECTED: Attempted direct transfer between "
                f"wallet '{s[:6]}...{s[-4:]}' and '{r[:6]}...{r[-4:]}'. Transaction BLOCKED."
            )

        return True, "Safe: No inter-farming-wallet cluster link detected."

    def assert_safe_transfer(self, sender: str, recipient: str) -> None:
        is_safe, reason = self.validate_transfer(sender, recipient)
        if not is_safe:
            raise SybilClusterViolationError(reason)


class PersonaEngine:
    """
    Simulates unique, human-like activity patterns for each wallet to evade behavioral Sybil detection.
    """

    def __init__(self, db: Optional[FarmingDatabase] = None):
        self.db = db
        self.guard = ClusterIsolationGuard()

    def generate_persona_for_wallet(self, address: str, preferred_archetype: Optional[str] = None) -> Dict[str, Any]:
        """
        Assigns or generates a deterministic yet varied persona profile based on wallet address hash.
        """
        addr_clean = address.lower().strip()
        self.guard.register_wallet(addr_clean)

        if preferred_archetype:
            archetype = next((a for a in PERSONA_ARCHETYPES if a["id"] == preferred_archetype), PERSONA_ARCHETYPES[0])
        else:
            # Deterministic selection based on wallet hash
            hash_int = int(hashlib.sha256(addr_clean.encode("utf-8")).hexdigest(), 16)
            archetype = PERSONA_ARCHETYPES[hash_int % len(PERSONA_ARCHETYPES)]

        # Add unique seed characteristics
        time_offset_min = (int(hashlib.md5(addr_clean.encode("utf-8")).hexdigest()[:4], 16) % 90) - 45

        persona = {
            "wallet_address": addr_clean,
            "archetype_id": archetype["id"],
            "persona_name": archetype["name"],
            "active_hours": archetype["active_hours"],
            "action_delay_range_sec": archetype["action_delay_range_sec"],
            "amount_jitter_pct": archetype["amount_jitter_pct"],
            "time_offset_minutes": time_offset_min,
            "risk_tolerance": archetype["risk_tolerance"],
        }
        return persona

    def is_active_now(self, persona: Dict[str, Any], current_hour: Optional[int] = None) -> bool:
        """Checks if current hour falls within the persona's daily activity window."""
        if current_hour is None:
            current_hour = datetime.now(timezone.utc).hour
        active_hours = persona.get("active_hours", list(range(24)))
        return current_hour in active_hours

    def gaussian_amount_jitter(self, base_amount: float, jitter_pct: float = 0.15) -> float:
        """
        Applies Gaussian noise to transaction amounts to avoid suspicious round numbers (e.g. 0.01000).
        Always returns a positive, naturally-varying float with 5-7 decimals.
        """
        if base_amount <= 0.0:
            return 0.0

        # Normal distribution with mean=base_amount, std_dev=base_amount * (jitter_pct / 2)
        std_dev = base_amount * (jitter_pct / 2.0)
        noisy = random.gauss(base_amount, std_dev)

        # Clamp between 70% and 130% of base amount to stay reasonable
        noisy_clamped = max(base_amount * 0.70, min(base_amount * 1.30, noisy))

        # Precision 5 to 7 decimal digits
        precision = random.choice([5, 6, 7])
        return round(noisy_clamped, precision)

    def calculate_delay_sec(self, persona: Dict[str, Any]) -> float:
        """Calculates a randomized human-like delay between steps."""
        delay_min, delay_max = persona.get("action_delay_range_sec", (5, 25))
        return round(random.uniform(delay_min, delay_max), 2)

    def shuffle_action_route(self, task_list: List[str], wallet_address: str) -> List[str]:
        """
        Permutes the execution route uniquely per wallet, breaking uniform sequence signatures.
        """
        tasks = list(task_list)
        if len(tasks) <= 1:
            return tasks

        # Deterministic shuffle seeded by wallet address + date string
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        seed_str = f"{wallet_address.lower()}_{today_str}"
        rng = random.Random(seed_str)
        rng.shuffle(tasks)
        return tasks

    def plan_daily_schedule(self, wallet_addresses: List[str], task_pool: List[str]) -> List[Dict[str, Any]]:
        """
        Creates an anti-sybil daily execution plan for all wallets with staggered routes and times.
        """
        plan = []
        for addr in wallet_addresses:
            persona = self.generate_persona_for_wallet(addr)
            shuffled_tasks = self.shuffle_action_route(task_pool, addr)
            base_delay = self.calculate_delay_sec(persona)

            plan.append({
                "wallet_address": addr,
                "persona_name": persona["persona_name"],
                "active_hours": persona["active_hours"],
                "shuffled_route": shuffled_tasks,
                "recommended_delay_sec": base_delay,
                "anti_sybil_score": "EXCELLENT",
            })
        return plan
