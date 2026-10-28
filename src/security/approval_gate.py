from __future__ import annotations

import asyncio
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
        mapping: Dict[str, str],
        raw_payload: Dict[str, Any],
        timeout_seconds: int = 60,
    ):
        self.id = approval_id
        self.risk_level = risk_level
        self.client_ip = client_ip
        self.model_requested = model_requested
        self.findings = findings
        self.sanitized_messages = sanitized_messages
        self.mapping = mapping
        self.raw_payload = raw_payload

        now = datetime.now(timezone.utc)
        self.created_at = now.isoformat()
        self.expires_at = (now + timedelta(seconds=timeout_seconds)).isoformat()

        self.event = asyncio.Event()
        self.decision: Optional[str] = None  # "approved", "rejected", "expired"
        self.decided_at: Optional[str] = None


class ApprovalGate:
    """
    Manages asynchronous Human-in-the-Loop (HITL) approvals.
    Uses asyncio.Event for zero-latency resumption when an admin approves or rejects a request.
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
        mapping: Dict[str, str],
        raw_payload: Dict[str, Any],
        timeout_seconds: int = 60,
    ) -> str:
        approval_id = str(uuid.uuid4())
        item = GateItem(
            approval_id=approval_id,
            risk_level=risk_level,
            client_ip=client_ip,
            model_requested=model_requested,
            findings=findings,
            sanitized_messages=sanitized_messages,
            mapping=mapping,
            raw_payload=raw_payload,
            timeout_seconds=timeout_seconds,
        )

        async with self._lock:
            self._gates[approval_id] = item

        # Persist to local database (NEVER write raw secrets/mapping to disk)
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
                "mapping": {},
                "raw_payload": {"model": model_requested, "messages": sanitized_messages},
            },
        )

        return approval_id

    async def wait_for_decision(
        self, approval_id: str, timeout_seconds: int = 60
    ) -> Tuple[str, Optional[GateItem]]:
        """
        Wait until human approves/rejects or timeout occurs.
        Returns ("approved" | "rejected" | "expired", GateItem).
        """
        async with self._lock:
            item = self._gates.get(approval_id)

        if not item:
            # Fallback check database
            db_row = await storage_db.get_approval_by_id(self.db_path, approval_id)
            if db_row and db_row.get("status") in ("approved", "rejected"):
                return db_row["status"], None
            return "expired", None

        try:
            await asyncio.wait_for(item.event.wait(), timeout=float(timeout_seconds))
            decision = item.decision or "rejected"
            return decision, item

        except asyncio.TimeoutError:
            # Timed out
            item.decision = "expired"
            now = datetime.now(timezone.utc).isoformat()
            item.decided_at = now
            await storage_db.update_pending_approval_status(self.db_path, approval_id, "expired")
            return "expired", item

        finally:
            # Cleanup memory gate item
            async with self._lock:
                self._gates.pop(approval_id, None)

    async def submit_decision(self, approval_id: str, decision: str) -> bool:
        """
        Invoked by UI dashboard when admin clicks Approve or Reject.
        """
        valid_decision = decision.lower().strip()
        if valid_decision not in ("approved", "rejected"):
            return False

        now = datetime.now(timezone.utc).isoformat()

        async with self._lock:
            item = self._gates.get(approval_id)
            if item:
                item.decision = valid_decision
                item.decided_at = now
                item.event.set()
                await storage_db.update_pending_approval_status(
                    self.db_path, approval_id, valid_decision
                )
                return True

        # If not active in memory (e.g. process restarted), still update DB record
        return await storage_db.update_pending_approval_status(
            self.db_path, approval_id, valid_decision
        )

    def get_active_count(self) -> int:
        return len(self._gates)
