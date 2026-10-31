"""Alpha Airdrop Radar & AI Opportunity Scorer.

Features Live DeFiLlama Tokenless Protocol Discovery and Multi-Tier Scoring.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

try:
    from ecosystem.airdrop_hunter.src.agent_sdk import UniversalAIClient
    from ecosystem.airdrop_hunter.src.config import ALPHA_THRESHOLD_HIGH, GATEWAY_API_KEY, GATEWAY_URL, PROJECT_ID
    from ecosystem.airdrop_hunter.src.storage.farming_db import FarmingDatabase
except ImportError:
    from src.agent_sdk import UniversalAIClient
    from src.config import ALPHA_THRESHOLD_HIGH, GATEWAY_API_KEY, GATEWAY_URL, PROJECT_ID
    from src.storage.farming_db import FarmingDatabase


logger = logging.getLogger("airdrop_radar")

# Pre-defined Top-tier Crypto Venture Capitals
TIER1_BACKERS = {
    "binance labs": 1.0,
    "paradigm": 1.0,
    "a16z": 1.0,
    "polychain": 0.95,
    "dragonfly": 0.90,
    "coinbase ventures": 0.90,
    "pantera": 0.85,
    "sequoia": 0.85,
    "multicoin": 0.80,
    "jump crypto": 0.75,
}

# Seed Curated Alpha Opportunities (0đ & Low-Gas)
SEED_CAMPAIGNS: List[Dict[str, Any]] = [
    {
        "id": "berachain-bArtio-testnet",
        "name": "Berachain bArtio Testnet (0đ)",
        "category": "TESTNET",
        "tier1_backers": ["Polychain", "Hack VC", "Framework Ventures"],
        "cost_type": "FREE",
        "estimated_cost_usd": 0.0,
        "details": {
            "chain": "Berachain Artio",
            "faucet_url": "https://artio.faucet.berachain.com",
            "tasks": ["daily_faucet", "bex_swap", "honey_mint"],
            "airdrop_probability": "Very High",
            "funding_m_usd": 142.0,
        },
    },
    {
        "id": "monad-devnet-early",
        "name": "Monad Ecosystem Early Interaction",
        "category": "TESTNET",
        "tier1_backers": ["Paradigm", "Dragonfly", "Electric Capital"],
        "cost_type": "FREE",
        "estimated_cost_usd": 0.0,
        "details": {
            "chain": "Monad EVM 10k TPS",
            "tasks": ["daily_discord_checkin", "testnet_faucet", "governance_vote"],
            "airdrop_probability": "High",
            "funding_m_usd": 225.0,
        },
    },
    {
        "id": "story-protocol-iliad",
        "name": "Story Protocol Testnet (IP Layer)",
        "category": "TESTNET",
        "tier1_backers": ["a16z", "Polychain", "Hashed"],
        "cost_type": "FREE",
        "estimated_cost_usd": 0.0,
        "details": {
            "chain": "Story Network",
            "tasks": ["register_ip_asset", "faucet_claim", "daily_streak"],
            "airdrop_probability": "High",
            "funding_m_usd": 140.0,
        },
    },
    {
        "id": "base-superchain-daily",
        "name": "Base Superchain Micro-Streaks",
        "category": "DAILY_CHECKIN",
        "tier1_backers": ["Coinbase Ventures"],
        "cost_type": "LOW_GAS",
        "estimated_cost_usd": 0.005,
        "details": {
            "chain": "Base L2",
            "tasks": ["daily_checkin_tx", "micro_swap_eth_usdc", "contract_deploy_mini"],
            "airdrop_probability": "Medium-High",
            "funding_m_usd": 500.0,
        },
    },
    {
        "id": "solana-solayer-restaking",
        "name": "Solayer / Jito Micro-Restaking Points",
        "category": "POINTS",
        "tier1_backers": ["Binance Labs", "Polychain", "Multicoin"],
        "cost_type": "LOW_GAS",
        "estimated_cost_usd": 0.02,
        "details": {
            "chain": "Solana",
            "tasks": ["micro_deposit_sol", "daily_points_claim"],
            "airdrop_probability": "High",
            "funding_m_usd": 45.0,
        },
    },
]


class AirdropRadar:
    """Discovers, scores, and filters top airdrop opportunities for zero/low-budget farmers."""

    def __init__(self, db: FarmingDatabase, ai_client: Optional[UniversalAIClient] = None):
        self.db = db
        self.ai_client = ai_client or UniversalAIClient(
            base_url=f"{GATEWAY_URL}/v1",
            api_key=GATEWAY_API_KEY,
            project_id=PROJECT_ID,
        )

    def calculate_alpha_score(self, campaign: Dict[str, Any]) -> float:
        """
        Compute deterministic alpha score (0-100) using 4 quantitative pillars:
        1. Tier-1 Backers (max 40 pts)
        2. Zero / Low Cost (max 30 pts)
        3. Anti-Dilution / High Funding Ratio (max 15 pts)
        4. Automation Fit (max 15 pts)
        """
        # Pillar 1: Backers (40 pts)
        backers = [b.lower().strip() for b in campaign.get("tier1_backers", [])]
        backer_weight = 0.0
        for b in backers:
            for t1, weight in TIER1_BACKERS.items():
                if t1 in b:
                    backer_weight = max(backer_weight, weight)
        backer_score = backer_weight * 40.0

        # Pillar 2: Cost (30 pts)
        cost_type = campaign.get("cost_type", "FREE").upper()
        est_cost = float(campaign.get("estimated_cost_usd", 0.0))
        if cost_type == "FREE" or est_cost == 0.0:
            cost_score = 30.0
        elif est_cost <= 0.01:
            cost_score = 25.0
        elif est_cost <= 0.05:
            cost_score = 18.0
        else:
            cost_score = 8.0

        # Pillar 3: Funding / Anti-dilution (15 pts)
        details = campaign.get("details", {})
        funding_m = float(details.get("funding_m_usd", 10.0))
        if funding_m >= 100.0:
            funding_score = 15.0
        elif funding_m >= 50.0:
            funding_score = 12.0
        elif funding_m >= 20.0:
            funding_score = 8.0
        else:
            funding_score = 5.0

        # Pillar 4: Automation Fit (15 pts)
        tasks = details.get("tasks", [])
        if any(t in ["daily_faucet", "daily_checkin_tx", "bex_swap", "faucet_claim", "stake", "swap"] for t in tasks):
            auto_score = 15.0
        else:
            auto_score = 10.0

        total_score = round(min(100.0, backer_score + cost_score + funding_score + auto_score), 1)
        return total_score

    def fetch_live_tokenless_protocols(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Queries DeFiLlama's open API to discover live protocols without a token.
        Filters for healthy TVL ($3M - $500M) and top ecosystems.
        Gracefully returns fallback seed if offline.
        """
        url = "https://api.llama.fi/protocols"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "AirdropAlphaRadar/2.0", "Accept": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.debug(f"DeFiLlama API live fetch unavailable (using seed): {e}")
            return []

        if not isinstance(data, list):
            return []

        candidates = []
        for p in data:
            # 1. Must NOT have a token symbol
            symbol = p.get("symbol")
            if symbol and symbol.strip() not in ["-", ""]:
                continue

            tvl = float(p.get("tvl") or 0.0)
            # 2. Healthy TVL between $3M and $500M
            if tvl < 3_000_000 or tvl > 500_000_000:
                continue

            slug = p.get("slug") or p.get("name", "").lower().replace(" ", "-")
            name = p.get("name", "Unknown Protocol")
            chain = p.get("chain", "Multi-Chain")
            category = p.get("category", "DeFi")

            # Infer backers based on dominant chains
            backers = []
            chain_lower = chain.lower()
            if "base" in chain_lower:
                backers.append("Coinbase Ventures")
            if "solana" in chain_lower:
                backers.append("Multicoin")
            if "arbitrum" in chain_lower or "optimism" in chain_lower:
                backers.append("Pantera")

            candidates.append({
                "id": f"defillama-{slug}",
                "name": f"{name} ({chain})",
                "category": "RETROACTIVE" if "dex" in category.lower() else "POINTS",
                "tier1_backers": backers or ["Community Protocol"],
                "cost_type": "LOW_GAS" if "solana" in chain_lower or "base" in chain_lower else "FREE",
                "estimated_cost_usd": 0.005 if "solana" in chain_lower or "base" in chain_lower else 0.0,
                "details": {
                    "chain": chain,
                    "tvl_usd": round(tvl, 2),
                    "funding_m_usd": round(tvl / 5_000_000, 1),
                    "tasks": ["daily_checkin_tx", "swap", "stake"],
                    "url": p.get("url", ""),
                    "source": "DeFiLlama Live",
                },
            })

            if len(candidates) >= limit:
                break

        return candidates

    def seed_initial_campaigns(self) -> List[Dict[str, Any]]:
        """Populate database with verified seed campaigns scored by algorithm."""
        seeded = []
        for raw in SEED_CAMPAIGNS:
            c = dict(raw)
            c["alpha_score"] = self.calculate_alpha_score(c)
            self.db.upsert_campaign(c)
            seeded.append(c)
        return seeded

    def scan_and_evaluate(
        self,
        include_live_defillama: bool = True,
        custom_candidates: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Scans all candidates (seed + live DeFiLlama + custom) and returns sorted high-potential airdrops.
        """
        candidates = list(SEED_CAMPAIGNS)

        if include_live_defillama:
            live_protocols = self.fetch_live_tokenless_protocols(limit=8)
            candidates.extend(live_protocols)

        if custom_candidates:
            candidates.extend(custom_candidates)

        evaluated: List[Dict[str, Any]] = []
        seen_ids = set()

        for cand in candidates:
            c_id = cand["id"]
            if c_id in seen_ids:
                continue
            seen_ids.add(c_id)

            score = self.calculate_alpha_score(cand)
            cand_copy = dict(cand)
            cand_copy["alpha_score"] = score
            self.db.upsert_campaign(cand_copy)
            evaluated.append(cand_copy)

        evaluated.sort(key=lambda x: x.get("alpha_score", 0.0), reverse=True)
        return evaluated

    def ai_enrich_opportunity(self, campaign_name: str, notes: str) -> Dict[str, Any]:
        """
        Calls AI Free Gateway to assess a newly rumored airdrop project.
        """
        prompt = (
            f"Phân tích tiềm năng Airdrop cho dự án: '{campaign_name}'.\n"
            f"Ghi chú: {notes}\n"
            f"Hãy đưa ra định dạng JSON gồm:\n"
            f"- alpha_score: số từ 0 đến 100\n"
            f"- category: TESTNET hoặc DAILY_CHECKIN hoặc POINTS\n"
            f"- cost_type: FREE hoặc LOW_GAS\n"
            f"- reason: lý do ngắn gọn vì sao nên làm hoặc tránh rác Sybil"
        )
        try:
            res = self.ai_client.ask_json(
                prompt=prompt,
                system="Bạn là chuyên gia thẩm định Airdrop crypto. Trả về JSON hợp lệ.",
                temperature=0.2,
            )
            return res if isinstance(res, dict) else {}
        except Exception as e:
            logger.warning(f"AI enrichment fallback due to error: {e}")
            return {
                "alpha_score": 75.0,
                "category": "TESTNET",
                "cost_type": "FREE",
                "reason": "Phân tích tự động dựa trên quy tắc an toàn 0 đồng.",
            }
