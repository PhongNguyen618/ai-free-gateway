from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    recovery_seconds: float = 60.0
    failures: int = 0
    opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at >= self.recovery_seconds:
            self.failures = 0
            self.opened_at = None
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold and self.opened_at is None:
            self.opened_at = time.monotonic()


class FileIdempotencyStore:
    """Tiny persistent idempotency store for alerts/signals/jobs.

    Intended as a fallback until idempotency is represented by unique keys in DuckDB.
    """

    def __init__(self, path: str = "data/idempotency.json", ttl_seconds: int = 86400):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = int(ttl_seconds)
        self._data: dict[str, float] = {}
        self._load()

    @staticmethod
    def make_key(*parts: Any) -> str:
        payload = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {str(k): float(v) for k, v in raw.items()}
        except (FileNotFoundError, ValueError, TypeError):
            self._data = {}
        self.purge()

    def _save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.path)

    def purge(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        self._data = {k: ts for k, ts in self._data.items() if ts >= cutoff}

    def seen(self, key: str) -> bool:
        self.purge()
        return key in self._data

    def mark(self, key: str) -> None:
        self._data[key] = time.time()
        self._save()
