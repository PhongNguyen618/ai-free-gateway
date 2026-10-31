from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ai_free_gateway.response_cache")


class ResponseCache:
    """
    Deterministic Response Cache for AI Agent queries.
    Prevents duplicate upstream LLM calls, saving up to 80% quota for identical
    structured queries (e.g. repetitive scans, classification, retry loops).
    """

    def __init__(self, max_entries: int = 512, default_ttl_seconds: float = 180.0):
        self.max_entries = max(1, int(max_entries))
        self.default_ttl_seconds = max(1.0, float(default_ttl_seconds))
        self._cache: OrderedDict[str, Tuple[Dict[str, Any], float]] = OrderedDict()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def compute_cache_key(
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        response_format: Optional[Any] = None,
    ) -> str:
        """
        Compute a stable SHA-256 hash of the canonical request payload.
        Normalizes messages and tool schemas to ensure whitespace/order consistency.
        """
        canonical_messages = []
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            canonical_messages.append({
                "role": str(m.get("role", "")).lower().strip(),
                "content": m.get("content"),
                "name": m.get("name"),
                "tool_calls": m.get("tool_calls"),
                "tool_call_id": m.get("tool_call_id"),
            })

        canonical_obj = {
            "model": str(model).strip().lower(),
            "messages": canonical_messages,
            "tools": tools or [],
            "response_format": response_format,
        }
        serialized = json.dumps(canonical_obj, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Retrieve cached response if present and not expired."""
        if key not in self._cache:
            self._misses += 1
            return None

        content, expire_at = self._cache[key]
        now = time.monotonic()
        if now >= expire_at:
            # Expired
            self._cache.pop(key, None)
            self._misses += 1
            return None

        # Move to end (MRU)
        self._cache.move_to_end(key)
        self._hits += 1
        remaining_ttl = max(1, int(expire_at - now))
        return {
            "content": content,
            "remaining_ttl": remaining_ttl,
        }

    def set(self, key: str, content: Dict[str, Any], ttl_seconds: Optional[float] = None) -> None:
        """Store response with expiration TTL."""
        ttl = self.default_ttl_seconds if (ttl_seconds is None or ttl_seconds <= 0) else float(ttl_seconds)
        expire_at = time.monotonic() + ttl

        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = (content, expire_at)

        # Evict oldest entry if capacity exceeded
        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)

    def get_stats(self) -> Dict[str, Any]:
        """Return cache health and hit rate telemetry."""
        total = self._hits + self._misses
        hit_rate = (self._hits / total) if total > 0 else 0.0
        return {
            "entries_count": len(self._cache),
            "max_entries": self.max_entries,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate_pct": round(hit_rate * 100, 2),
        }

    def clear(self) -> None:
        """Clear all cached entries."""
        self._cache.clear()
        self._hits = 0
        self._misses = 0
