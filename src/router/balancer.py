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

    def evaluate_model_intelligence(
        self, model_name: str, context_length: int = 128000, task_type: str = "general"
    ) -> float:
        """
        Dynamically evaluate any model's intelligence, parameter size, and capability
        purely from metadata and heuristics WITHOUT requiring hardcoded list entries.
        Automatically recognizes new models harvested from OpenRouter/Groq/Gemini.
        """
        score = 0.0
        name_lower = model_name.lower()

        # 1. Automatic Parameter Size Extraction via Regex (e.g. 70b, 120b, 72b, 32b, 8b, 405b, 671b)
        # Check MoE patterns first (e.g. 8x7b, 8x22b)
        moe_match = re.search(r"(\d+)x(\d+(?:\.\d+)?)b", name_lower)
        if moe_match:
            experts = int(moe_match.group(1))
            size = float(moe_match.group(2))
            total_active = experts * size
            if total_active >= 100:
                score += 320.0
            elif total_active >= 40:
                score += 220.0
            else:
                score += 150.0
        else:
            # Regular parameter size matching
            b_matches = re.findall(r"(?:^|[^\w])(\d+(?:\.\d+)?)b(?:[^\w]|$)", name_lower)
            if b_matches:
                try:
                    sizes = [float(val) for val in b_matches]
                    max_b = max(sizes)
                    if max_b >= 100:      # 120B, 405B, 671B
                        score += 350.0
                    elif max_b >= 60:     # 70B, 72B
                        score += 270.0
                    elif max_b >= 25:     # 27B, 32B
                        score += 180.0
                    elif max_b >= 12:     # 14B, 16B
                        score += 120.0
                    elif max_b >= 6:      # 7B, 8B, 9B
                        score += 60.0
                    else:                 # 1.5B, 3B
                        score += 15.0
                except Exception:
                    pass

        # 2. Top-tier Model Family & Lab reputation boosts
        if "deepseek" in name_lower:
            score += 120.0
        if "qwen" in name_lower:
            score += 110.0
        if "llama-3" in name_lower or "llama3" in name_lower:
            score += 100.0
        if "gemini-2" in name_lower or "gemini-1.5-pro" in name_lower:
            score += 110.0
        elif "gemini" in name_lower:
            score += 80.0
        if "gpt-4" in name_lower or "gpt-oss" in name_lower:
            score += 110.0
        if "claude" in name_lower:
            score += 120.0
        if "mistral" in name_lower or "mixtral" in name_lower:
            score += 90.0

        # 3. Reasoning & Chain-of-Thought (CoT) specialists
        if any(term in name_lower for term in ["r1", "reason", "o1", "thought"]):
            score += 120.0

        # 4. Coding Specialists
        if any(term in name_lower for term in ["coder", "coding", "starcoder"]):
            score += 130.0

        # 5. Task-specific matching boost
        if task_type == "code":
            if any(term in name_lower for k in ["code", "coder", "deepseek", "qwen", "llama-3"] if k == term):
                score += 80.0
        elif task_type == "reasoning":
            if any(term in name_lower for k in ["r1", "reason", "120b", "70b", "gemini-2"] if k == term):
                score += 80.0

        # 6. Context Window capacity (useful for reading whole codebases)
        if context_length >= 1000000:
            score += 120.0
        elif context_length >= 128000:
            score += 70.0
        elif context_length >= 32000:
            score += 40.0

        return score

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

            # 1. Strongly prioritize providers with configured API keys
            if m["provider"] in configured_providers:
                score += 500.0
            elif configured_providers:
                score -= 500.0

            # 2. Dynamic Intelligence Evaluation (Parameters, Architecture, MoE, Coding, Context)
            context_len = m.get("context_length", 128000)
            score += self.evaluate_model_intelligence(m["model_name"], context_len, task_type)

            # 3. User-defined Priority List rank boost (Optional manual override)
            for rank_idx, p_id in enumerate(priority_models):
                if m["id"] == p_id or m["model_name"] == p_id or m["id"].endswith("/" + p_id):
                    score += (len(priority_models) - rank_idx) * 80.0
                    break

            # 4. Latency & health weighting
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
