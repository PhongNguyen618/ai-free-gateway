from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import httpx

from src.config_loader import AppConfig, get_provider_api_key
from src.security.egress_guard import EgressGuard
from src.storage import db as storage_db

logger = logging.getLogger("ai_free_gateway.harvester")


def is_strictly_zero_price(val: Any) -> bool:
    """
    Validate that pricing value is strictly 0 (accepting 0, 0.0, "0", "0.0").
    Strictly rejects None, missing, empty, negative, positive, NaN, Infinity, or invalid types.
    """
    if val is None:
        return False
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return False
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return False
        return f == 0.0
    except (ValueError, TypeError):
        return False


class ModelHarvester:
    """
    Discovers and tracks free-tier LLM models across providers.
    Polls OpenRouter API strictly for verified $0 models and synchronizes static provider pools.
    """

    def __init__(self, config: AppConfig, egress_guard: EgressGuard):
        self.config = config
        self.egress = egress_guard
        self.db_path = config.db_path
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self.last_harvest_time: Optional[str] = None
        self.is_harvesting: bool = False
        self.is_stale: bool = False

    async def register_static_free_models(self) -> int:
        """Register configured free models from enabled providers into DB."""
        count = 0
        providers_dict = {
            "groq": self.config.providers.groq,
            "gemini": self.config.providers.gemini,
            "github": self.config.providers.github,
        }

        for provider_name, pconfig in providers_dict.items():
            if not pconfig or not pconfig.enabled:
                # If provider is disabled, mark its models inactive
                await storage_db.reconcile_active_models(self.db_path, provider_name, [])
                continue

            active_ids = []
            for model_cfg in pconfig.static_free_models:
                namespaced_id = f"{provider_name}/{model_cfg.id}"
                model_entry = {
                    "id": namespaced_id,
                    "provider": provider_name,
                    "model_name": model_cfg.id,
                    "display_name": f"{provider_name.capitalize()} - {model_cfg.id}",
                    "context_length": model_cfg.context_length,
                    "is_free": True,
                    "is_active": True,
                    "latency_ms": 150.0,
                }
                await storage_db.upsert_model(self.db_path, model_entry)
                active_ids.append(namespaced_id)
                count += 1

                # Canonical alias pointing to the same provider+model
                alias_entry = dict(model_entry)
                alias_entry["id"] = model_cfg.id
                alias_entry["display_name"] = f"{model_cfg.id} (via {provider_name})"
                await storage_db.upsert_model(self.db_path, alias_entry)
                active_ids.append(model_cfg.id)

            await storage_db.reconcile_active_models(self.db_path, provider_name, active_ids)

        return count

    async def harvest_groq(self) -> int:
        """Query Groq API for currently active models for this API key."""
        pcfg = self.config.providers.groq
        if not pcfg or not pcfg.enabled:
            await storage_db.reconcile_active_models(self.db_path, "groq", [])
            return 0

        api_key = get_provider_api_key("groq", self.config)
        if not api_key:
            return 0

        try:
            headers = {"Authorization": f"Bearer {api_key}"}
            response = await self.egress.get("https://api.groq.com/openai/v1/models", headers=headers)
            if response.status_code != 200:
                logger.warning(f"Groq models endpoint returned HTTP {response.status_code}")
                self.is_stale = True
                return 0

            data = response.json()
            models_list = data.get("data", [])
            discovered_ids = []
            count = 0

            for m in models_list:
                try:
                    m_id = m.get("id", "")
                    if not m_id:
                        continue
                    m_lower = m_id.lower()
                    if any(x in m_lower for x in ["whisper", "guard", "orpheus", "allam", "vision", "tts"]):
                        continue

                    namespaced_id = f"groq/{m_id}"
                    model_entry = {
                        "id": namespaced_id,
                        "provider": "groq",
                        "model_name": m_id,
                        "display_name": f"Groq - {m_id}",
                        "context_length": 128000,
                        "is_free": True,
                        "is_active": True,
                        "latency_ms": 60.0,
                    }
                    await storage_db.upsert_model(self.db_path, model_entry)
                    discovered_ids.append(namespaced_id)
                    count += 1

                    alias_entry = dict(model_entry)
                    alias_entry["id"] = m_id
                    alias_entry["display_name"] = f"{m_id} (via groq)"
                    await storage_db.upsert_model(self.db_path, alias_entry)
                    discovered_ids.append(m_id)
                except Exception as ex:
                    logger.warning(f"Skipping malformed Groq model record {m}: {ex}")
                    continue

            # Reconcile snapshot
            await storage_db.reconcile_active_models(self.db_path, "groq", discovered_ids)
            logger.info(f"Successfully harvested {count} active models from Groq.")
            return count

        except Exception as e:
            logger.warning(f"Network error harvesting Groq models: {e}. Preserving previous snapshot.")
            self.is_stale = True
            return 0

    async def harvest_openrouter(self) -> int:
        """
        Query OpenRouter API strictly for verified 0$ models.
        Strictly requires prompt and completion pricing == 0.
        """
        or_cfg = self.config.providers.openrouter
        if not or_cfg or not or_cfg.enabled:
            await storage_db.reconcile_active_models(self.db_path, "openrouter", [])
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
                logger.warning(f"OpenRouter models endpoint returned HTTP {response.status_code}. Retaining existing snapshot.")
                self.is_stale = True
                return 0

            data = response.json()
            models_list = data.get("data", [])
            discovered_count = 0
            discovered_ids = []

            for m in models_list:
                try:
                    model_id = m.get("id", "")
                    if not model_id:
                        continue

                    pricing = m.get("pricing")
                    if not isinstance(pricing, dict):
                        # Missing pricing information -> reject
                        continue

                    prompt_p = pricing.get("prompt")
                    completion_p = pricing.get("completion")

                    # Verify strict zero price
                    if not (is_strictly_zero_price(prompt_p) and is_strictly_zero_price(completion_p)):
                        continue

                    context_len = m.get("context_length", 128000)
                    namespaced_id = f"openrouter/{model_id}"
                    model_entry = {
                        "id": namespaced_id,
                        "provider": "openrouter",
                        "model_name": model_id,
                        "display_name": m.get("name", model_id),
                        "context_length": context_len,
                        "is_free": True,
                        "is_active": True,
                        "latency_ms": getattr(response, "latency_ms", 300.0),
                    }
                    await storage_db.upsert_model(self.db_path, model_entry)
                    discovered_ids.append(namespaced_id)
                    discovered_count += 1

                    # Short alias
                    alias_entry = dict(model_entry)
                    alias_entry["id"] = model_id
                    await storage_db.upsert_model(self.db_path, alias_entry)
                    discovered_ids.append(model_id)

                except Exception as ex:
                    # Single malformed record does NOT abort the harvest
                    logger.debug(f"Skipping invalid OpenRouter model record: {ex}")
                    continue

            # Reconcile snapshot: unlist models that disappeared or became paid
            await storage_db.reconcile_active_models(self.db_path, "openrouter", discovered_ids)
            logger.info(f"Successfully harvested {discovered_count} verified $0 free models from OpenRouter.")
            self.is_stale = False
            return discovered_count

        except Exception as e:
            logger.error(f"Error harvesting models from OpenRouter: {e}. Preserving previous snapshot as stale.")
            self.is_stale = True
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
                "is_stale": self.is_stale,
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
