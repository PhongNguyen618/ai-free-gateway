from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import httpx

from src.config_loader import AppConfig, get_provider_api_key
from src.security.egress_guard import EgressGuard
from src.storage import db as storage_db

logger = logging.getLogger("ai_free_gateway.harvester")


class ModelHarvester:
    """
    Discovers and tracks free-tier LLM models across providers.
    Polls OpenRouter API for active $0 models and registers configured free providers.
    """

    def __init__(self, config: AppConfig, egress_guard: EgressGuard):
        self.config = config
        self.egress = egress_guard
        self.db_path = config.db_path
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self.last_harvest_time: Optional[str] = None
        self.is_harvesting: bool = False

    async def register_static_free_models(self) -> int:
        """Register configured free models from Groq, Gemini, and GitHub into DB."""
        count = 0
        providers_dict = {
            "groq": self.config.providers.groq,
            "gemini": self.config.providers.gemini,
            "github": self.config.providers.github,
        }

        for provider_name, pconfig in providers_dict.items():
            if not pconfig or not pconfig.enabled:
                continue

            for model_cfg in pconfig.static_free_models:
                # Store with namespaced ID and alias
                model_entry = {
                    "id": f"{provider_name}/{model_cfg.id}",
                    "provider": provider_name,
                    "model_name": model_cfg.id,
                    "display_name": f"{provider_name.capitalize()} - {model_cfg.id}",
                    "context_length": model_cfg.context_length,
                    "is_free": True,
                    "is_active": True,
                    "latency_ms": 150.0,
                }
                await storage_db.upsert_model(self.db_path, model_entry)
                count += 1

                # Also insert generic alias if not conflicted
                alias_entry = dict(model_entry)
                alias_entry["id"] = model_cfg.id
                alias_entry["display_name"] = f"{model_cfg.id} (via {provider_name})"
                await storage_db.upsert_model(self.db_path, alias_entry)

        return count

    async def harvest_groq(self) -> int:
        """Query Groq API for currently active models for this API key."""
        api_key = get_provider_api_key("groq", self.config)
        if not api_key:
            return 0
        try:
            headers = {"Authorization": f"Bearer {api_key}"}
            response = await self.egress.get("https://api.groq.com/openai/v1/models", headers=headers)
            if response.status_code != 200:
                return 0
            data = response.json()
            models_list = data.get("data", [])
            count = 0
            for m in models_list:
                m_id = m.get("id", "")
                if not m_id:
                    continue
                m_lower = m_id.lower()
                if any(x in m_lower for x in ["whisper", "guard", "orpheus", "allam", "vision", "tts"]):
                    continue
                model_entry = {
                    "id": f"groq/{m_id}",
                    "provider": "groq",
                    "model_name": m_id,
                    "display_name": f"Groq - {m_id}",
                    "context_length": 128000,
                    "is_free": True,
                    "is_active": True,
                    "latency_ms": 60.0,
                }
                await storage_db.upsert_model(self.db_path, model_entry)
                count += 1
                alias_entry = dict(model_entry)
                alias_entry["id"] = m_id
                alias_entry["display_name"] = f"{m_id} (via groq)"
                await storage_db.upsert_model(self.db_path, alias_entry)
            logger.info(f"Successfully harvested {count} active models from Groq.")
            return count
        except Exception as e:
            logger.warning(f"Error harvesting Groq models: {e}")
            return 0

    async def harvest_openrouter(self) -> int:
        """
        Query OpenRouter API for 0$ / ':free' models.
        """
        or_cfg = self.config.providers.openrouter
        if not or_cfg or not or_cfg.enabled:
            return 0

        models_endpoint = or_cfg.models_endpoint or "https://openrouter.ai/api/v1/models"
        api_key = get_provider_api_key("openrouter", self.config)

        headers = {
            "User-Agent": "ai-free-gateway/1.0",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            response = await self.egress.get(models_endpoint, headers=headers)
            if response.status_code != 200:
                logger.warning(f"OpenRouter models endpoint returned HTTP {response.status_code}")
                return 0

            data = response.json()
            models_list = data.get("data", [])
            discovered_count = 0

            for m in models_list:
                model_id = m.get("id", "")
                pricing = m.get("pricing", {})
                prompt_price = float(pricing.get("prompt", -1) or -1)
                completion_price = float(pricing.get("completion", -1) or -1)

                is_free = False
                # Criteria 1: ends with :free
                if model_id.endswith(":free"):
                    is_free = True
                # Criteria 2: pricing explicitly 0
                elif prompt_price == 0.0 and completion_price == 0.0:
                    is_free = True

                if is_free:
                    context_len = m.get("context_length", 128000)
                    model_entry = {
                        "id": f"openrouter/{model_id}",
                        "provider": "openrouter",
                        "model_name": model_id,
                        "display_name": m.get("name", model_id),
                        "context_length": context_len,
                        "is_free": True,
                        "is_active": True,
                        "latency_ms": getattr(response, "latency_ms", 300.0),
                    }
                    await storage_db.upsert_model(self.db_path, model_entry)
                    discovered_count += 1

                    # Short alias
                    alias_entry = dict(model_entry)
                    alias_entry["id"] = model_id
                    await storage_db.upsert_model(self.db_path, alias_entry)

            logger.info(f"Successfully harvested {discovered_count} free models from OpenRouter.")
            return discovered_count

        except Exception as e:
            logger.error(f"Error harvesting models from OpenRouter: {e}")
            return 0

    async def harvest_all(self) -> Dict[str, Any]:
        """Perform a full harvest cycle across all configured providers."""
        if self.is_harvesting:
            return {"status": "in_progress", "timestamp": self.last_harvest_time}

        self.is_harvesting = True
        try:
            static_count = await self.register_static_free_models()
            groq_count = await self.harvest_groq()
            openrouter_count = 0
            if self.config.harvester.auto_discover_openrouter:
                openrouter_count = await self.harvest_openrouter()

            self.last_harvest_time = datetime.now(timezone.utc).isoformat()
            return {
                "status": "success",
                "static_models_registered": static_count,
                "groq_discovered": groq_count,
                "openrouter_discovered": openrouter_count,
                "timestamp": self.last_harvest_time,
            }
        finally:
            self.is_harvesting = False

    async def _loop(self) -> None:
        """Background periodic harvesting loop."""
        interval = max(60, self.config.harvester.poll_interval_seconds)
        while not self._stop_event.is_set():
            try:
                await self.harvest_all()
            except Exception as e:
                logger.error(f"Harvester loop encountered exception: {e}")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=float(interval))
            except asyncio.TimeoutError:
                pass

    def start_background(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
