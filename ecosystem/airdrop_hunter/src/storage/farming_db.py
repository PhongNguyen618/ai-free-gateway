"""SQLite storage manager for Airdrop Hunter Agent."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class FarmingDatabase:
    """Manages persistence for campaigns, wallets, persona profiles, execution logs, and airdrop rewards."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS airdrop_campaigns (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    tier1_backers TEXT DEFAULT '[]',
                    cost_type TEXT DEFAULT 'FREE',
                    estimated_cost_usd REAL DEFAULT 0.0,
                    alpha_score REAL DEFAULT 0.0,
                    status TEXT DEFAULT 'ACTIVE',
                    details_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS farming_wallets (
                    address TEXT PRIMARY KEY,
                    chain_type TEXT DEFAULT 'EVM',
                    persona_id TEXT NOT NULL,
                    persona_json TEXT DEFAULT '{}',
                    status TEXT DEFAULT 'ACTIVE',
                    active_days INTEGER DEFAULT 0,
                    active_weeks INTEGER DEFAULT 0,
                    last_active_date TEXT DEFAULT '',
                    total_gas_usd REAL DEFAULT 0.0,
                    points_accumulated INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet_address TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details TEXT DEFAULT '',
                    cost_usd REAL DEFAULT 0.0,
                    tx_hash TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS airdrop_rewards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet_address TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    token_symbol TEXT NOT NULL,
                    amount_tokens REAL DEFAULT 0.0,
                    value_usd REAL DEFAULT 0.0,
                    routed_to_meme_bot INTEGER DEFAULT 0,
                    routed_amount_usd REAL DEFAULT 0.0,
                    retained_gas_usd REAL DEFAULT 0.0,
                    created_at TEXT NOT NULL,
                    routed_at TEXT DEFAULT ''
                )
                """
            )
            conn.commit()

    def upsert_campaign(self, campaign: Dict[str, Any]) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO airdrop_campaigns (
                    id, name, category, tier1_backers, cost_type,
                    estimated_cost_usd, alpha_score, status, details_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    category = excluded.category,
                    tier1_backers = excluded.tier1_backers,
                    cost_type = excluded.cost_type,
                    estimated_cost_usd = excluded.estimated_cost_usd,
                    alpha_score = excluded.alpha_score,
                    status = excluded.status,
                    details_json = excluded.details_json,
                    updated_at = excluded.updated_at
                """,
                (
                    campaign["id"],
                    campaign["name"],
                    campaign.get("category", "TESTNET"),
                    json.dumps(campaign.get("tier1_backers", [])),
                    campaign.get("cost_type", "FREE"),
                    float(campaign.get("estimated_cost_usd", 0.0)),
                    float(campaign.get("alpha_score", 0.0)),
                    campaign.get("status", "ACTIVE"),
                    json.dumps(campaign.get("details", {})),
                    now_iso,
                    now_iso,
                ),
            )
            conn.commit()

    def list_campaigns(self, status: Optional[str] = "ACTIVE", min_score: float = 0.0) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM airdrop_campaigns WHERE alpha_score >= ?"
            params: List[Any] = [min_score]
            if status:
                query += " AND status = ?"
                params.append(status)
            query += " ORDER BY alpha_score DESC"
            cursor.execute(query, params)
            rows = cursor.fetchall()
            result = []
            for row in rows:
                item = dict(row)
                try:
                    item["tier1_backers"] = json.loads(item["tier1_backers"])
                except Exception:
                    item["tier1_backers"] = []
                try:
                    item["details"] = json.loads(item["details_json"])
                except Exception:
                    item["details"] = {}
                result.append(item)
            return result

    def upsert_wallet(
        self,
        address: str,
        chain_type: str = "EVM",
        persona_id: str = "casual_fauceteer",
        persona_details: Optional[Dict[str, Any]] = None,
        status: str = "ACTIVE",
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        persona_json = json.dumps(persona_details or {})
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO farming_wallets (
                    address, chain_type, persona_id, persona_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    chain_type = excluded.chain_type,
                    persona_id = excluded.persona_id,
                    persona_json = excluded.persona_json,
                    status = excluded.status
                """,
                (address.lower(), chain_type.upper(), persona_id, persona_json, status, now_iso),
            )
            conn.commit()

    def list_wallets(self, status: Optional[str] = "ACTIVE") -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute("SELECT * FROM farming_wallets WHERE status = ? ORDER BY active_days DESC", (status,))
            else:
                cursor.execute("SELECT * FROM farming_wallets ORDER BY active_days DESC")
            rows = cursor.fetchall()
            result = []
            for row in rows:
                item = dict(row)
                try:
                    item["persona"] = json.loads(item["persona_json"])
                except Exception:
                    item["persona"] = {}
                result.append(item)
            return result

    def get_wallet(self, address: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM farming_wallets WHERE address = ?", (address.lower(),))
            row = cursor.fetchone()
            if not row:
                return None
            item = dict(row)
            try:
                item["persona"] = json.loads(item["persona_json"])
            except Exception:
                item["persona"] = {}
            return item

    def record_execution(
        self,
        wallet_address: str,
        campaign_id: str,
        action_type: str,
        status: str,
        details: str = "",
        cost_usd: float = 0.0,
        tx_hash: str = "",
    ) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO execution_logs (
                    wallet_address, campaign_id, action_type, status, details, cost_usd, tx_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (wallet_address.lower(), campaign_id, action_type, status, details, cost_usd, tx_hash, now_iso),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def update_wallet_activity(self, address: str, today_str: str, cost_usd: float = 0.0, points_add: int = 10) -> None:
        """Update wallet active_days and streak if this is a new day."""
        wallet = self.get_wallet(address)
        if not wallet:
            return

        last_active = wallet.get("last_active_date") or ""
        active_days = wallet.get("active_days") or 0
        active_weeks = wallet.get("active_weeks") or 0
        total_gas = (wallet.get("total_gas_usd") or 0.0) + cost_usd
        total_points = (wallet.get("points_accumulated") or 0) + points_add

        if last_active != today_str:
            active_days += 1
            if active_days % 7 == 1 and active_days > 1:
                active_weeks += 1

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE farming_wallets
                SET active_days = ?,
                    active_weeks = ?,
                    last_active_date = ?,
                    total_gas_usd = ?,
                    points_accumulated = ?
                WHERE address = ?
                """,
                (active_days, active_weeks, today_str, round(total_gas, 6), total_points, address.lower()),
            )
            conn.commit()

    def record_reward(
        self,
        wallet_address: str,
        campaign_id: str,
        token_symbol: str,
        amount_tokens: float,
        value_usd: float,
    ) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO airdrop_rewards (
                    wallet_address, campaign_id, token_symbol, amount_tokens, value_usd, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (wallet_address.lower(), campaign_id, token_symbol.upper(), float(amount_tokens), float(value_usd), now_iso),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def list_rewards(self, unrouted_only: bool = False) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if unrouted_only:
                cursor.execute("SELECT * FROM airdrop_rewards WHERE routed_to_meme_bot = 0 ORDER BY id ASC")
            else:
                cursor.execute("SELECT * FROM airdrop_rewards ORDER BY id DESC")
            return [dict(row) for row in cursor.fetchall()]

    def mark_reward_routed(self, reward_id: int, routed_amount_usd: float, retained_gas_usd: float) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE airdrop_rewards
                SET routed_to_meme_bot = 1,
                    routed_amount_usd = ?,
                    retained_gas_usd = ?,
                    routed_at = ?
                WHERE id = ?
                """,
                (float(routed_amount_usd), float(retained_gas_usd), now_iso, reward_id),
            )
            conn.commit()

    def get_unrouted_rewards_summary(self) -> Dict[str, Any]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*), SUM(value_usd) FROM airdrop_rewards WHERE routed_to_meme_bot = 0")
            row = cursor.fetchone()
            return {
                "unrouted_count": row[0] or 0,
                "unrouted_total_usd": round(row[1] or 0.0, 2),
            }

    def get_summary(self) -> Dict[str, Any]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM airdrop_campaigns WHERE status = 'ACTIVE'")
            active_campaigns = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*), SUM(active_days), SUM(total_gas_usd), SUM(points_accumulated) FROM farming_wallets")
            w_row = cursor.fetchone()
            total_wallets = w_row[0] or 0
            total_active_days = w_row[1] or 0
            total_gas = round(w_row[2] or 0.0, 4)
            total_points = w_row[3] or 0

            cursor.execute("SELECT COUNT(*) FROM execution_logs WHERE status = 'SUCCESS'")
            total_actions = cursor.fetchone()[0]

            cursor.execute("SELECT SUM(value_usd), SUM(routed_amount_usd) FROM airdrop_rewards")
            r_row = cursor.fetchone()
            total_rewards_usd = round(r_row[0] or 0.0, 2)
            total_routed_usd = round(r_row[1] or 0.0, 2)

            return {
                "active_campaigns": active_campaigns,
                "total_wallets": total_wallets,
                "cumulative_active_days": total_active_days,
                "total_gas_spent_usd": total_gas,
                "total_farming_points": total_points,
                "successful_actions_count": total_actions,
                "total_airdrop_rewards_usd": total_rewards_usd,
                "total_routed_to_meme_usd": total_routed_usd,
            }
