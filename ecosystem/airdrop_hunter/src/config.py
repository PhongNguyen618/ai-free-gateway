"""System configuration for Airdrop Hunter Agent."""

from __future__ import annotations

import os
from pathlib import Path

# Base Paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Gateway & Agent Hub Connection
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")
GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY", "sk-local-dev-token")
PROJECT_ID = "airdrop-hunter-agent"

# Farming Operating Mode: 'LIVE' (thực thi on-chain thật 100%, Fail-Closed) or 'SIMULATION'
FARMING_MODE = os.getenv("FARMING_MODE", "LIVE").upper()

# Database Path
DB_PATH = os.getenv("AIRDROP_DB_PATH", str(DATA_DIR / "airdrop_hunter.db"))

# AI Evaluation & Anti-Sybil Settings
ALPHA_THRESHOLD_HIGH = 80.0
ALPHA_THRESHOLD_MEDIUM = 65.0
MAX_DAILY_GAS_USD = float(os.getenv("MAX_DAILY_GAS_USD", "0.5"))

# Anti-Sybil Persona Archetypes
PERSONA_ARCHETYPES = [
    {
        "id": "night_owl_degen",
        "name": "Cú đêm hệ DeFi",
        "active_hours": [20, 21, 22, 23, 0, 1, 2, 3],
        "risk_tolerance": "medium",
        "action_delay_range_sec": (12, 45),
        "amount_jitter_pct": 0.18,
    },
    {
        "id": "morning_jogger",
        "name": "Thợ săn sáng sớm",
        "active_hours": [5, 6, 7, 8, 9, 10],
        "risk_tolerance": "low",
        "action_delay_range_sec": (8, 28),
        "amount_jitter_pct": 0.12,
    },
    {
        "id": "weekend_nft_collector",
        "name": "Nhà sưu tầm cuối tuần",
        "active_hours": [11, 12, 13, 14, 15, 16, 17, 18, 19],
        "risk_tolerance": "low",
        "action_delay_range_sec": (15, 60),
        "amount_jitter_pct": 0.22,
    },
    {
        "id": "casual_fauceteer",
        "name": "Học sinh chuyên Testnet 0đ",
        "active_hours": [8, 11, 14, 17, 20],
        "risk_tolerance": "ultra_safe",
        "action_delay_range_sec": (5, 20),
        "amount_jitter_pct": 0.10,
    },
]
