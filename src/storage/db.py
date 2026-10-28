from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional
import aiosqlite


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_db_dir(db_path: str) -> None:
    p = Path(db_path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def get_db(db_path: str) -> AsyncIterator[aiosqlite.Connection]:
    ensure_db_dir(db_path)
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


async def init_db(db_path: str) -> None:
    """Initialize SQLite tables and indexes."""
    async with get_db(db_path) as conn:
        # Table 1: models_pool
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS models_pool (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                model_name TEXT NOT NULL,
                display_name TEXT,
                context_length INTEGER DEFAULT 128000,
                is_free INTEGER DEFAULT 1,
                is_active INTEGER DEFAULT 1,
                latency_ms REAL DEFAULT 0.0,
                error_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                last_checked_at TEXT,
                created_at TEXT NOT NULL
            )
        """)

        # Table 2: quota_state
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS quota_state (
                provider TEXT PRIMARY KEY,
                requests_today INTEGER DEFAULT 0,
                tokens_today INTEGER DEFAULT 0,
                rate_limited_until TEXT,
                last_status_code INTEGER DEFAULT 200,
                last_error_message TEXT,
                updated_at TEXT NOT NULL
            )
        """)

        # Table 3: dlp_audit_logs
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS dlp_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                client_ip TEXT,
                direction TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                findings_json TEXT NOT NULL,
                redaction_count INTEGER DEFAULT 0,
                action_taken TEXT NOT NULL,
                prompt_snippet TEXT,
                restored_count INTEGER DEFAULT 0
            )
        """)

        # Table 4: pending_approvals
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_approvals (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL,
                client_ip TEXT,
                risk_level TEXT NOT NULL,
                model_requested TEXT,
                findings_json TEXT NOT NULL,
                sanitized_messages_json TEXT NOT NULL,
                mapping_json TEXT NOT NULL,
                raw_payload_json TEXT NOT NULL
            )
        """)

        # Indexes for fast filtering
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_models_provider ON models_pool(provider)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_models_active ON models_pool(is_active)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_dlp_timestamp ON dlp_audit_logs(timestamp)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_approvals_status ON pending_approvals(status)")

        await conn.commit()


# --- Models Pool Operations ---

async def upsert_model(db_path: str, model_data: Dict[str, Any]) -> None:
    now = _now_iso()
    async with get_db(db_path) as conn:
        await conn.execute("""
            INSERT INTO models_pool (
                id, provider, model_name, display_name, context_length,
                is_free, is_active, latency_ms, last_checked_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                display_name = excluded.display_name,
                context_length = excluded.context_length,
                is_free = excluded.is_free,
                is_active = excluded.is_active,
                last_checked_at = excluded.last_checked_at
        """, (
            model_data["id"],
            model_data["provider"],
            model_data["model_name"],
            model_data.get("display_name", model_data["model_name"]),
            model_data.get("context_length", 128000),
            1 if model_data.get("is_free", True) else 0,
            1 if model_data.get("is_active", True) else 0,
            model_data.get("latency_ms", 0.0),
            now,
            now,
        ))
        await conn.commit()


async def get_active_models(db_path: str, provider: Optional[str] = None) -> List[Dict[str, Any]]:
    async with get_db(db_path) as conn:
        if provider:
            cursor = await conn.execute(
                "SELECT * FROM models_pool WHERE is_active = 1 AND is_free = 1 AND provider = ? ORDER BY latency_ms ASC",
                (provider,)
            )
        else:
            cursor = await conn.execute(
                "SELECT * FROM models_pool WHERE is_active = 1 AND is_free = 1 ORDER BY provider ASC, latency_ms ASC"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_model_by_id(db_path: str, model_id: str) -> Optional[Dict[str, Any]]:
    async with get_db(db_path) as conn:
        cursor = await conn.execute("SELECT * FROM models_pool WHERE id = ?", (model_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_model_metrics(db_path: str, model_id: str, latency_ms: float, is_success: bool) -> None:
    now = _now_iso()
    async with get_db(db_path) as conn:
        if is_success:
            await conn.execute("""
                UPDATE models_pool
                SET latency_ms = (latency_ms * 0.7 + ? * 0.3),
                    success_count = success_count + 1,
                    last_checked_at = ?
                WHERE id = ?
            """, (latency_ms, now, model_id))
        else:
            await conn.execute("""
                UPDATE models_pool
                SET error_count = error_count + 1,
                    last_checked_at = ?
                WHERE id = ?
            """, (now, model_id))
        await conn.commit()


# --- Quota & Cooldown Operations ---

async def set_provider_cooldown(
    db_path: str,
    provider: str,
    cooldown_seconds: int,
    status_code: int = 429,
    error_msg: str = "Rate limit reached"
) -> None:
    now = datetime.now(timezone.utc)
    until = (now + timedelta(seconds=cooldown_seconds)).isoformat()
    async with get_db(db_path) as conn:
        await conn.execute("""
            INSERT INTO quota_state (
                provider, requests_today, tokens_today, rate_limited_until,
                last_status_code, last_error_message, updated_at
            ) VALUES (?, 1, 0, ?, ?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                rate_limited_until = excluded.rate_limited_until,
                last_status_code = excluded.last_status_code,
                last_error_message = excluded.last_error_message,
                updated_at = excluded.updated_at
        """, (provider, until, status_code, error_msg, now.isoformat()))
        await conn.commit()


async def is_provider_in_cooldown(db_path: str, provider: str) -> bool:
    async with get_db(db_path) as conn:
        cursor = await conn.execute(
            "SELECT rate_limited_until FROM quota_state WHERE provider = ?",
            (provider,)
        )
        row = await cursor.fetchone()
        if not row or not row["rate_limited_until"]:
            return False

        try:
            until = datetime.fromisoformat(row["rate_limited_until"])
            return datetime.now(timezone.utc) < until
        except Exception:
            return False


async def increment_provider_usage(db_path: str, provider: str, tokens: int = 0) -> None:
    now = _now_iso()
    async with get_db(db_path) as conn:
        await conn.execute("""
            INSERT INTO quota_state (
                provider, requests_today, tokens_today, rate_limited_until,
                last_status_code, last_error_message, updated_at
            ) VALUES (?, 1, ?, NULL, 200, NULL, ?)
            ON CONFLICT(provider) DO UPDATE SET
                requests_today = requests_today + 1,
                tokens_today = tokens_today + excluded.tokens_today,
                updated_at = excluded.updated_at
        """, (provider, max(0, tokens), now))
        await conn.commit()


async def get_all_quota_states(db_path: str) -> List[Dict[str, Any]]:
    async with get_db(db_path) as conn:
        cursor = await conn.execute("SELECT * FROM quota_state ORDER BY provider ASC")
        rows = await cursor.fetchall()
        result = []
        now = datetime.now(timezone.utc)
        for r in rows:
            d = dict(r)
            in_cooldown = False
            cooldown_remaining_sec = 0
            if d.get("rate_limited_until"):
                try:
                    until = datetime.fromisoformat(d["rate_limited_until"])
                    if now < until:
                        in_cooldown = True
                        cooldown_remaining_sec = max(0, int((until - now).total_seconds()))
                except Exception:
                    pass
            d["in_cooldown"] = in_cooldown
            d["cooldown_remaining_sec"] = cooldown_remaining_sec
            result.append(d)
        return result


# --- DLP Audit Logs ---

async def insert_dlp_log(db_path: str, log_data: Dict[str, Any]) -> int:
    async with get_db(db_path) as conn:
        cursor = await conn.execute("""
            INSERT INTO dlp_audit_logs (
                timestamp, client_ip, direction, risk_level, findings_json,
                redaction_count, action_taken, prompt_snippet, restored_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            log_data.get("timestamp", _now_iso()),
            log_data.get("client_ip", "127.0.0.1"),
            log_data.get("direction", "request"),
            log_data.get("risk_level", "LOW"),
            json.dumps(log_data.get("findings", [])),
            log_data.get("redaction_count", 0),
            log_data.get("action_taken", "ALLOWED"),
            log_data.get("prompt_snippet", "")[:200],
            log_data.get("restored_count", 0),
        ))
        await conn.commit()
        return cursor.lastrowid or 0


async def get_dlp_logs(db_path: str, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    async with get_db(db_path) as conn:
        cursor = await conn.execute("""
            SELECT * FROM dlp_audit_logs
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))
        rows = await cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["findings"] = json.loads(d["findings_json"])
            except Exception:
                d["findings"] = []
            result.append(d)
        return result


# --- Human-in-the-Loop Approvals ---

async def insert_pending_approval(db_path: str, data: Dict[str, Any]) -> None:
    async with get_db(db_path) as conn:
        await conn.execute("""
            INSERT INTO pending_approvals (
                id, created_at, expires_at, status, client_ip, risk_level,
                model_requested, findings_json, sanitized_messages_json, mapping_json, raw_payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["id"],
            data["created_at"],
            data["expires_at"],
            data.get("status", "pending"),
            data.get("client_ip", "127.0.0.1"),
            data.get("risk_level", "MEDIUM"),
            data.get("model_requested", "auto"),
            json.dumps(data.get("findings", [])),
            json.dumps(data.get("sanitized_messages", [])),
            json.dumps(data.get("mapping", {})),
            json.dumps(data.get("raw_payload", {})),
        ))
        await conn.commit()


async def update_pending_approval_status(db_path: str, approval_id: str, status: str) -> bool:
    async with get_db(db_path) as conn:
        cursor = await conn.execute(
            "UPDATE pending_approvals SET status = ? WHERE id = ?",
            (status, approval_id)
        )
        await conn.commit()
        return cursor.rowcount > 0


async def get_pending_approvals(db_path: str) -> List[Dict[str, Any]]:
    now_iso = _now_iso()
    async with get_db(db_path) as conn:
        # Auto expire timed out pending rows
        await conn.execute(
            "UPDATE pending_approvals SET status = 'expired' WHERE status = 'pending' AND expires_at < ?",
            (now_iso,)
        )
        await conn.commit()

        cursor = await conn.execute("""
            SELECT id, created_at, expires_at, status, client_ip, risk_level,
                   model_requested, findings_json
            FROM pending_approvals
            WHERE status = 'pending'
            ORDER BY created_at DESC
        """)
        rows = await cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["findings"] = json.loads(d["findings_json"])
            except Exception:
                d["findings"] = []
            result.append(d)
        return result


async def get_approval_by_id(db_path: str, approval_id: str) -> Optional[Dict[str, Any]]:
    async with get_db(db_path) as conn:
        cursor = await conn.execute(
            "SELECT * FROM pending_approvals WHERE id = ?",
            (approval_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        for key in ["findings_json", "sanitized_messages_json", "mapping_json", "raw_payload_json"]:
            try:
                d[key.replace("_json", "")] = json.loads(d[key])
            except Exception:
                pass
        return d


async def get_dashboard_stats(db_path: str) -> Dict[str, Any]:
    async with get_db(db_path) as conn:
        # Total models
        c_models = await conn.execute("SELECT COUNT(*) as cnt FROM models_pool WHERE is_active = 1")
        row_m = await c_models.fetchone()
        total_models = row_m["cnt"] if row_m else 0

        # Total requests and tokens
        c_quota = await conn.execute("SELECT SUM(requests_today) as reqs, SUM(tokens_today) as toks FROM quota_state")
        q_row = await c_quota.fetchone()
        total_requests = (q_row["reqs"] if q_row else 0) or 0
        total_tokens = (q_row["toks"] if q_row else 0) or 0

        # Pending approvals
        c_pending = await conn.execute("SELECT COUNT(*) as cnt FROM pending_approvals WHERE status = 'pending'")
        row_p = await c_pending.fetchone()
        pending_count = row_p["cnt"] if row_p else 0

        # Total DLP incidents
        c_dlp = await conn.execute("SELECT COUNT(*) as cnt FROM dlp_audit_logs WHERE risk_level IN ('MEDIUM', 'HIGH')")
        row_d = await c_dlp.fetchone()
        dlp_incidents = row_d["cnt"] if row_d else 0

        return {
            "total_active_models": total_models,
            "total_requests_today": total_requests,
            "total_tokens_today": total_tokens,
            "pending_approvals_count": pending_count,
            "dlp_incidents_count": dlp_incidents,
        }
