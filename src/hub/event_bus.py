"""
Shared Event Bus for Inter-Project Collaboration.
Provides a lightweight publish/subscribe event hub allowing autonomous agents
from different projects to post signals, discover shared opportunities, and react.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger("ai_free_gateway.hub.event_bus")


class SharedEventBus:
    def __init__(self, max_history: int = 1000):
        self.max_history = max(50, int(max_history))
        self._events: deque[Dict[str, Any]] = deque(maxlen=self.max_history)
        self._event_counter = 0
        self._subscribers: List[Callable[[Dict[str, Any]], Optional[Coroutine[Any, Any, None]]]] = []

    def publish(
        self,
        topic: str,
        source_project: str,
        payload: Dict[str, Any],
        priority: str = "normal",
    ) -> Dict[str, Any]:
        """Publishes an event to the shared event board."""
        self._event_counter += 1
        clean_topic = (topic or "general").strip().lower()
        clean_source = (source_project or "system").strip()

        event = {
            "event_id": f"evt_{self._event_counter}",
            "sequence": self._event_counter,
            "topic": clean_topic,
            "source_project": clean_source,
            "payload": payload or {},
            "priority": priority.lower(),
            "timestamp": time.time(),
        }
        self._events.append(event)
        logger.info(f"Published event #{self._event_counter} on topic '{clean_topic}' from '{clean_source}'")

        # Notify active callbacks (non-blocking)
        for sub in list(self._subscribers):
            try:
                res = sub(event)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as e:
                logger.warning(f"Error in event subscriber: {e}")

        return event

    def poll(
        self,
        topics: Optional[List[str]] = None,
        since_sequence: int = 0,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Retrieves events newer than since_sequence matching the requested topic filter.
        """
        filter_topics = {t.strip().lower() for t in topics} if topics else None
        results = []

        for evt in self._events:
            if evt["sequence"] <= since_sequence:
                continue
            if filter_topics:
                # Matches exact topic or prefix wildcard (e.g. 'market.*' matches 'market.hot_token')
                matched = False
                for ft in filter_topics:
                    if ft.endswith("*") and evt["topic"].startswith(ft[:-1]):
                        matched = True
                        break
                    elif evt["topic"] == ft:
                        matched = True
                        break
                if not matched:
                    continue

            results.append(evt)
            if len(results) >= limit:
                break

        return results

    def get_history(self, limit: int = 100, topic: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns the most recent events in chronological order."""
        events_list = list(self._events)
        if topic:
            clean_topic = topic.strip().lower()
            events_list = [e for e in events_list if e["topic"] == clean_topic]
        return events_list[-limit:]

    def subscribe(self, callback: Callable[[Dict[str, Any]], Optional[Coroutine[Any, Any, None]]]) -> None:
        """Registers a callback for real-time event notifications."""
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[Dict[str, Any]], Optional[Coroutine[Any, Any, None]]]) -> None:
        """Removes a previously registered subscriber callback."""
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def clear(self) -> None:
        """Resets the event stream and counter."""
        self._events.clear()
        self._event_counter = 0
