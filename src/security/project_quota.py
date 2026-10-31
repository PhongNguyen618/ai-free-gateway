from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("ai_free_gateway.project_quota")

# Rate limits in Requests Per Minute (RPM) per project priority
PRIORITY_RPM_LIMITS = {
    "critical": 600,   # High-priority agents (e.g. smart-money-agent trading gates)
    "normal": 60,      # Standard assistants, chatbot responses, interactive tools
    "background": 15,  # Bulk scrapers, background summarizers, news feed parsers
}


class ProjectQuotaGuard:
    """
    Project-level quota isolation and noisy-neighbor protection.
    Ensures background jobs cannot exhaust rate limits for critical live agents.
    """

    def __init__(self, limits: Optional[Dict[str, int]] = None):
        self.limits = limits or dict(PRIORITY_RPM_LIMITS)
        self._project_history: Dict[str, deque] = defaultdict(deque)
        self._stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "total_requests": 0,
            "cache_hits": 0,
            "errors": 0,
            "tokens_estimated": 0,
            "active_priority": "normal",
            "last_seen_at": 0.0,
        })

    def resolve_priority(self, project_id: str, requested_priority: Optional[str] = None) -> str:
        """Determine effective priority from header or known project convention."""
        clean_p = str(requested_priority or "").strip().lower()
        if clean_p in self.limits:
            return clean_p

        # Intelligent default priority attribution by project name
        proj_lower = project_id.lower()
        if any(term in proj_lower for term in ["trading", "smart-money", "critical", "exec", "live"]):
            return "critical"
        if any(term in proj_lower for term in ["scrape", "crawl", "news", "batch", "background", "harvest"]):
            return "background"

        return "normal"

    def check_quota(self, project_id: str, requested_priority: Optional[str] = None) -> Tuple[bool, int, str]:
        """
        Check if the project is within its rate limit for the current minute.
        Returns: (allowed, retry_after_seconds, effective_priority)
        """
        clean_id = (project_id or "default").strip()
        priority = self.resolve_priority(clean_id, requested_priority)
        limit_rpm = self.limits.get(priority, 60)

        now = time.monotonic()
        history = self._project_history[clean_id]

        # Prune events older than 60 seconds
        cutoff = now - 60.0
        while history and history[0] < cutoff:
            history.popleft()

        if len(history) >= limit_rpm:
            earliest = history[0]
            retry_after = max(1, int(60.0 - (now - earliest)))
            return False, retry_after, priority

        # Consume token
        history.append(now)
        return True, 0, priority

    def record_outcome(
        self,
        project_id: str,
        priority: str,
        cache_hit: bool = False,
        error: bool = False,
        tokens_estimated: int = 0,
    ) -> None:
        """Update telemetry stats for the given project."""
        clean_id = (project_id or "default").strip()
        row = self._stats[clean_id]
        row["total_requests"] += 1
        row["active_priority"] = priority
        row["last_seen_at"] = time.time()
        row["tokens_estimated"] += tokens_estimated
        if cache_hit:
            row["cache_hits"] += 1
        if error:
            row["errors"] += 1

    def get_all_stats(self) -> Dict[str, Any]:
        """Return formatted overview of all tracked projects."""
        projects = {}
        for pid, s in self._stats.items():
            hit_rate = (s["cache_hits"] / s["total_requests"] * 100) if s["total_requests"] > 0 else 0.0
            projects[pid] = {
                "project_id": pid,
                "priority": s["active_priority"],
                "total_requests": s["total_requests"],
                "cache_hits": s["cache_hits"],
                "cache_hit_rate_pct": round(hit_rate, 2),
                "errors": s["errors"],
                "tokens_estimated": s["tokens_estimated"],
                "last_seen_timestamp": s["last_seen_at"],
            }
        return {"projects": projects, "total_projects_count": len(projects)}

    def clear(self) -> None:
        """Reset history and stats."""
        self._project_history.clear()
        self._stats.clear()
