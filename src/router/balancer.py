from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from src.config_loader import AppConfig, get_provider_api_key
from src.storage import db as storage_db

logger = logging.getLogger("ai_free_gateway.balancer")


class ModelBalancer:
    """
    Intelligent Model Balancer & Router.
    Selects optimal free models, monitors provider cooldowns, and manages fallback chains.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.db_path = config.db_path

    def classify_task(self, prompt_text: str) -> str:
        """Classify task as 'code', 'reasoning', or 'general'."""
        text_lower = prompt_text.lower()

        # Code keywords
        if any(kw in text_lower for kw in [
            "def ", "function", "class ", "import ", "const ", "var ", "let ",
            "sql", "select ", "github", "stack trace", "exception", "bug", "refactor",
            "regex", "algorithm", "html", "css", "dockerfile", "api"
        ]):
            return "code"

        # Reasoning keywords
        if any(kw in text_lower for kw in [
            "prove", "theorem", "step by step", "think carefully", "solve this puzzle",
            "reasoning", "math", "calculate", "deduce", "logic problem"
        ]):
            return "reasoning"

        return "general"

    async def get_candidate_chain(
        self,
        requested_model: str,
        prompt_text: str = "",
        estimated_tokens: int = 1000,
    ) -> List[Dict[str, Any]]:
        """
        Build an ordered list of candidate models for routing and automatic fallback.
        Excludes providers currently in 429 cooldown.
        """
        task_type = self.classify_task(prompt_text)
        all_models = await storage_db.get_active_models(self.db_path)

        # Get cooldown status per provider
        quota_states = await storage_db.get_all_quota_states(self.db_path)
        cooldown_providers = {
            q["provider"] for q in quota_states if q.get("in_cooldown", False)
        }

        # Filter out models from cooling down providers
        available_models = [
            m for m in all_models if m["provider"] not in cooldown_providers
        ]

        # Case 1: Exact model requested (bypass auto-tier scoring only if specific model is requested)
        clean_req = requested_model.strip()
        is_auto_mode = clean_req.lower() in ("auto", "default", "best", "free", "")
        matched = None

        # Check configured API keys
        configured_providers = set()
        for p_name in ["openrouter", "groq", "gemini", "github"]:
            if get_provider_api_key(p_name, self.config):
                configured_providers.add(p_name)

        if not is_auto_mode:
            for m in available_models:
                if m["id"] == clean_req or m["model_name"] == clean_req:
                    matched = m
                    break

        candidates: List[Dict[str, Any]] = []
        if matched:
            candidates.append(matched)

        # Build prioritized fallback list using Tier & Capability scoring
        priority_models = getattr(self.config.harvester, "priority_models", [])
        scored_candidates: List[Tuple[float, Dict[str, Any]]] = []

        for m in available_models:
            if matched and m["id"] == matched["id"]:
                continue

            # Context window check
            if m.get("context_length", 128000) < estimated_tokens:
                continue

            score = 100.0
            name_lower = m["model_name"].lower()

            # 1. Strongly prioritize providers with configured API keys
            if m["provider"] in configured_providers:
                score += 500.0
            elif configured_providers:
                score -= 500.0

            # 2. Priority List rank boost (User-configured tier order)
            for rank_idx, p_id in enumerate(priority_models):
                if m["id"] == p_id or m["model_name"] == p_id or m["id"].endswith("/" + p_id):
                    score += (len(priority_models) - rank_idx) * 150.0
                    break

            # 3. Model Architecture & Parameter Size Tier boost (120B > 70B > 32B/27B > 8B)
            if "120b" in name_lower:
                score += 250.0
            elif "70b" in name_lower:
                score += 200.0
            elif "32b" in name_lower or "27b" in name_lower or "qwen3.8" in name_lower:
                score += 160.0
            elif "8b" in name_lower or "7b" in name_lower:
                score += 50.0

            # 4. Specialized capabilities (Coding & Reasoning)
            if "coder" in name_lower or "deepseek" in name_lower:
                score += 120.0
            if "r1" in name_lower or "reason" in name_lower:
                score += 110.0

            # 5. Task affinity scoring
            if task_type == "code":
                if any(k in name_lower for k in ["code", "deepseek", "llama-3.3", "qwen", "120b"]):
                    score += 80.0
            elif task_type == "reasoning":
                if any(k in name_lower for k in ["r1", "reason", "120b", "70b", "gemini-2.0"]):
                    score += 80.0

            # 6. Context Window & Latency weighting
            context_len = m.get("context_length", 128000)
            score += min(100.0, context_len / 10000.0)

            latency = float(m.get("latency_ms", 200.0) or 200.0)
            score += max(0.0, 300.0 / (latency + 1.0))

            errors = int(m.get("error_count", 0))
            score -= (errors * 20.0)

            scored_candidates.append((score, m))

        scored_candidates.sort(key=lambda x: x[0], reverse=True)
        for _, m in scored_candidates:
            # Deduplicate by unique provider+model_name
            if not any(c["id"] == m["id"] for c in candidates):
                candidates.append(m)

        # If everything in DB is empty or in cooldown, fall back to default configs
        if not candidates:
            for fallback_id in self.config.harvester.default_fallback_models:
                parts = fallback_id.split("/", 1)
                provider = parts[0]
                model_name = parts[1] if len(parts) > 1 else fallback_id
                if provider not in cooldown_providers:
                    candidates.append({
                        "id": fallback_id,
                        "provider": provider,
                        "model_name": model_name,
                        "display_name": fallback_id,
                        "context_length": 128000,
                        "is_free": 1,
                        "is_active": 1,
                        "latency_ms": 200.0,
                    })

        return candidates

    def resolve_provider_request(
        self, candidate: Dict[str, Any], payload: Dict[str, Any]
    ) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        """
        Prepare target URL, HTTP headers, and JSON payload for the provider.
        """
        provider = candidate["provider"]
        model_name = candidate["model_name"]

        providers_dict = {
            "openrouter": self.config.providers.openrouter,
            "groq": self.config.providers.groq,
            "gemini": self.config.providers.gemini,
            "github": self.config.providers.github,
        }

        pcfg = providers_dict.get(provider)
        if not pcfg:
            raise ValueError(f"Unknown provider: {provider}")

        base_url = pcfg.base_url.rstrip("/")
        target_url = f"{base_url}/chat/completions"

        api_key = get_provider_api_key(provider, self.config)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ai-free-gateway/1.0",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # Provider-specific headers
        if provider == "openrouter":
            headers["HTTP-Referer"] = "http://localhost:8000"
            headers["X-Title"] = "AI Free Gateway"

        # Transform payload
        req_payload = dict(payload)
        req_payload["model"] = model_name

        return target_url, headers, req_payload
