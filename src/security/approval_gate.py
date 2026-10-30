from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from src.storage import db as storage_db


class GateItem:
    def __init__(
        self,
        approval_id: str,
        risk_level: str,
        client_ip: str,
        model_requested: str,
        findings: List[Dict[str, Any]],
        sanitized_messages: List[Dict[str, Any]],
        sanitized_payload: Dict[str, Any],
        timeout_seconds: int = 60,
    ):
        self.id = approval_id
        self.risk_level = risk_level
        self.client_ip = client_ip
        self.model_requested = model_requested
        self.findings = findings
        self.sanitized_messages = sanitized_messages
        self.sanitized_payload = sanitized_payload

        now = datetime.now(timezone.utc)
        self.created_at = now.isoformat()
        self.expires_at = (now + timedelta(seconds=timeout_seconds)).isoformat()
        self.deadline_mono = time.monotonic() + float(timeout_seconds)

        self.event = asyncio.Event()
        self.decision: Optional[str] = None  # "approved", "rejected", "expired", "cancelled"
        self.decided_at: Optional[str] = None


class ApprovalGate:
    """
    Manages asynchronous Human-in-the-Loop (HITL) approvals.
    Uses asyncio.Event and strict SQLite atomic state transitions.
    """

    def __init__(self, db_path: str = "storage/gateway.db"):
        self.db_path = db_path
        self._gates: Dict[str, GateItem] = {}
        self._lock = asyncio.Lock()

    async def create_request(
        self,
        risk_level: str,
        client_ip: str,
        model_requested: str,
        findings: List[Dict[str, Any]],
        sanitized_messages: List[Dict[str, Any]],
        sanitized_payload: Optional[Dict[str, Any]] = None,
        timeout_seconds: int = 60,
        mapping: Optional[Dict[str, str]] = None,
        raw_payload: Optional[Dict[str, Any]] = None,
    ) -> str:
        approval_id = str(uuid.uuid4())
        # Safe payload is strictly built from sanitized projection, NEVER from raw payload (R2-02)
        safe_payload = sanitized_payload or {"model": model_requested, "messages": sanitized_messages}
        safe_mapping = mapping or {}

        item = GateItem(
            approval_id=approval_id,
            risk_level=risk_level,
            client_ip=client_ip,
            model_requested=model_requested,
            findings=findings,
            sanitized_messages=sanitized_messages,
            sanitized_payload=safe_payload,
            timeout_seconds=timeout_seconds,
        )

        # Persist to database FIRST. If insert fails, do not corrupt memory.
        try:
            await storage_db.insert_pending_approval(
                self.db_path,
                {
                    "id": approval_id,
                    "created_at": item.created_at,
                    "expires_at": item.expires_at,
                    "status": "pending",
                    "client_ip": client_ip,
                    "risk_level": risk_level,
                    "model_requested": model_requested,
                    "findings": findings,
                    "sanitized_messages": sanitized_messages,
                    "sanitized_payload": safe_payload,
                },
            )
        except Exception:
            raise

        async with self._lock:
            self._gates[approval_id] = item

        return approval_id

    async def wait_for_decision(
        self, approval_id: str, timeout_seconds: int = 60
    ) -> Tuple[str, Optional[GateItem]]:
        """
        Wait until human approves/rejects or deadline expires.
        Returns ("approved" | "rejected" | "expired" | "cancelled", GateItem).
        Handles race conditions cleanly between admin submit and timeout expiry.
        """
        async with self._lock:
            item = self._gates.get(approval_id)

        if not item:
            # Fallback check database
            db_row = await storage_db.get_approval_by_id(self.db_path, approval_id)
            if db_row and db_row.get("status") in ("approved", "rejected", "cancelled"):
                return db_row["status"], None
            return "expired", None

        # Calculate remaining time against absolute monotonic deadline
        remaining_time = max(0.001, item.deadline_mono - time.monotonic())

        try:
            await asyncio.wait_for(item.event.wait(), timeout=remaining_time)
            # Re-read authoritative decision
            db_row = await storage_db.get_approval_by_id(self.db_path, approval_id)
            if db_row and db_row.get("status") in ("approved", "rejected", "cancelled"):
                item.decision = db_row["status"]
            decision = item.decision or "rejected"
            return decision, item

        except asyncio.TimeoutError:
            # Race condition handling: atomically expire only if deadline passed (N03)
            updated = await storage_db.expire_pending_approval(
                self.db_path, approval_id
            )
            if not updated:
                db_row = await storage_db.get_approval_by_id(self.db_path, approval_id)
                if db_row and db_row.get("status") in ("approved", "rejected", "cancelled"):
                    item.decision = db_row["status"]
                    return db_row["status"], item

            item.decision = "expired"
            now = datetime.now(timezone.utc).isoformat()
            item.decided_at = now
            return "expired", item

        finally:
            # Cleanup memory gate item
            async with self._lock:
                self._gates.pop(approval_id, None)

    async def cancel_request(self, approval_id: str) -> None:
        """Handle client disconnection / cancellation cleanly in memory and SQLite (N03)."""
        async with self._lock:
            item = self._gates.pop(approval_id, None)
            if item:
                if item.decision not in ("approved", "rejected"):
                    item.decision = "cancelled"
                item.event.set()

        await storage_db.update_pending_approval_status(
            self.db_path, approval_id, "cancelled", require_unexpired=False
        )

    async def submit_decision(self, approval_id: str, decision: str) -> bool:
        """
        Invoked by UI dashboard or API when admin submits decision.
        Atomic conditional update ensures only one transition succeeds.
        """
        valid_decision = decision.lower().strip()
        if valid_decision not in ("approved", "rejected"):
            return False

        now = datetime.now(timezone.utc).isoformat()

        # Update SQLite atomically with conditional state check
        updated = await storage_db.update_pending_approval_status(
            self.db_path, approval_id, valid_decision, require_unexpired=True
        )
        if not updated:
            return False

        # If DB update succeeded, wake up any in-memory waiter
        async with self._lock:
            item = self._gates.get(approval_id)
            if item:
                item.decision = valid_decision
                item.decided_at = now
                item.event.set()

        return True

    def get_active_count(self) -> int:
        return len(self._gates)
