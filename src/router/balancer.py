from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from src.config_loader import AppConfig, get_provider_api_key
from src.storage import db as storage_db

logger = logging.getLogger("ai_free_gateway.balancer")


class ModelNotFoundError(ValueError):
    """Raised when an explicit model requested is not found in the pool."""
    pass


class ContextLengthExceededError(ValueError):
    """Raised when the requested model does not have enough context length for the request budget."""
    pass


def estimate_payload_tokens(payload: Dict[str, Any]) -> int:
    """
    Conservatively estimate token budget from messages, tool calls, and output reserve.
    Uses conservative 1 token ~= 3 characters for code, symbols, and multilingual text.
    """
    total_chars = 0

    # 1. Messages
    messages = payload.get("messages", [])
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        c = msg.get("content")
        if isinstance(c, str):
            total_chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total_chars += len(part["text"])

        # Tool calls arguments
        tool_calls = msg.get("tool_calls", [])
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "")
                    if isinstance(args, str):
                        total_chars += len(args)

        # Reasoning content (thinking models)
        r_c = msg.get("reasoning_content")
        if isinstance(r_c, str):
            total_chars += len(r_c)

    # 2. Tools schemas and definitions
    tools = payload.get("tools", [])
    if isinstance(tools, list):
        for t in tools:
            if isinstance(t, dict):
                fn = t.get("function", {})
                if isinstance(fn.get("description"), str):
                    total_chars += len(fn["description"])
                if isinstance(fn.get("parameters"), dict):
                    total_chars += len(json.dumps(fn["parameters"]))

    input_tokens = (total_chars // 3) + 10

    # 3. Output reserve
    output_reserve = payload.get("max_tokens") or payload.get("max_completion_tokens") or 1000
    try:
        output_reserve = int(output_reserve)
    except (ValueError, TypeError):
        output_reserve = 1000

    return input_tokens + max(256, output_reserve)


class ModelBalancer:
    """
    Intelligent Model Balancer & Router.
    Selects optimal free models, monitors provider cooldowns, and manages fallback chains.
    Enforces strict context window budget and exact explicit model validation.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.db_path = config.db_path
        self.last_active_model: Optional[Dict[str, Any]] = None
        self.last_fallback_chain: List[Dict[str, Any]] = []
        self.last_routed_at: Optional[str] = None
        self.total_routed_requests: int = 0

    def record_active_model(
        self,
        candidate: Dict[str, Any],
        full_chain: List[Dict[str, Any]],
        failed_attempts: Optional[List[Dict[str, Any]]] = None,
        tokens_used: int = 0,
    ) -> None:
        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).isoformat()

        self.last_active_model = {
            "id": candidate.get("id"),
            "model_name": candidate.get("model_name"),
            "display_name": candidate.get("display_name") or candidate.get("model_name"),
            "provider": candidate.get("provider"),
            "context_length": candidate.get("context_length", 128000),
            "latency_ms": round(float(candidate.get("latency_ms", 0.0) or 0.0), 1),
            "last_used_at": now_str,
            "status": "ACTIVE",
            "tokens_last_request": tokens_used,
            "failed_attempts": failed_attempts or [],
        }

        # Deduplicate and record upcoming fallback models
        seen: Set[Tuple[str, str]] = {(candidate.get("provider", ""), candidate.get("model_name", ""))}
        fallbacks = []
        for c in full_chain:
            canonical = (c.get("provider", ""), c.get("model_name", ""))
            if canonical not in seen:
                seen.add(canonical)
                fallbacks.append(c)

        self.last_fallback_chain = fallbacks[:5]
        self.last_routed_at = now_str
        self.total_routed_requests += 1

    async def get_routing_status(self) -> Dict[str, Any]:
        quota_states = await storage_db.get_all_quota_states(self.db_path)
        cooldown_map = {q["provider"]: q.get("in_cooldown", False) for q in quota_states}
        remaining_map = {q["provider"]: q.get("cooldown_remaining_sec", 0) for q in quota_states}

        reset_cycle_map = {
            "groq": "TPM reset mỗi 60s | Ngày reset 07:00 VN",
            "gemini": "RPM reset mỗi 60s | Ngày reset 14:00 VN",
            "openrouter": "Free quota reset 07:00 VN",
            "github": "Hạn mức ngày reset 07:00 VN",
        }

        if self.last_active_model:
            active = dict(self.last_active_model)
            active["in_cooldown"] = cooldown_map.get(active["provider"], False)
            active["cooldown_remaining_sec"] = remaining_map.get(active["provider"], 0)
            if self.db_path:
                try:
                    m_db = await storage_db.get_model_by_id(self.db_path, active["id"])
                    if m_db:
                        active["total_tokens"] = m_db.get("total_tokens", 0)
                        active["success_count"] = m_db.get("success_count", 0)
                except Exception:
                    pass
            active["reset_cycle"] = reset_cycle_map.get(active["provider"], "Reset rolling theo chu kỳ Free Tier")

            chain = []
            for idx, c in enumerate(self.last_fallback_chain):
                is_cd = cooldown_map.get(c.get("provider"), False)
                chain.append({
                    "rank": idx + 1,
                    "id": c.get("id"),
                    "model_name": c.get("model_name"),
                    "display_name": c.get("display_name") or c.get("model_name"),
                    "provider": c.get("provider"),
                    "context_length": c.get("context_length", 128000),
                    "latency_ms": round(float(c.get("latency_ms", 0.0) or 0.0), 1),
                    "in_cooldown": is_cd,
                    "cooldown_remaining_sec": remaining_map.get(c.get("provider"), 0),
                    "status": "COOLDOWN (429)" if is_cd else "READY",
                })

            return {
                "has_served_request": True,
                "total_routed": self.total_routed_requests,
                "last_routed_at": self.last_routed_at,
                "active_model": active,
                "fallback_chain": chain,
            }

        # Default / Pre-flight state
        candidates = await self.get_candidate_chain("auto")
        if candidates:
            primary = candidates[0]
            fallbacks = [c for c in candidates[1:] if c.get("id") != primary.get("id")][:5]
            is_prim_cd = cooldown_map.get(primary.get("provider"), False)

            active = {
                "id": primary.get("id"),
                "model_name": primary.get("model_name"),
                "display_name": primary.get("display_name") or primary.get("model_name"),
                "provider": primary.get("provider"),
                "context_length": primary.get("context_length", 128000),
                "total_tokens": primary.get("total_tokens", 0),
                "success_count": primary.get("success_count", 0),
                "tokens_last_request": 0,
                "latency_ms": round(float(primary.get("latency_ms", 0.0) or 0.0), 1),
                "last_used_at": primary.get("last_checked_at"),
                "status": "READY (PRIMARY)",
                "reset_cycle": reset_cycle_map.get(primary.get("provider"), "Reset rolling theo chu kỳ Free Tier"),
                "in_cooldown": is_prim_cd,
                "cooldown_remaining_sec": remaining_map.get(primary.get("provider"), 0),
                "failed_attempts": [],
            }
            chain = []
            for idx, c in enumerate(fallbacks):
                is_cd = cooldown_map.get(c.get("provider"), False)
                chain.append({
                    "rank": idx + 1,
                    "id": c.get("id"),
                    "model_name": c.get("model_name"),
                    "display_name": c.get("display_name") or c.get("model_name"),
                    "provider": c.get("provider"),
                    "context_length": c.get("context_length", 128000),
                    "latency_ms": round(float(c.get("latency_ms", 0.0) or 0.0), 1),
                    "in_cooldown": is_cd,
                    "cooldown_remaining_sec": remaining_map.get(c.get("provider"), 0),
                    "status": "COOLDOWN (429)" if is_cd else "READY",
                })
            return {
                "has_served_request": False,
                "total_routed": 0,
                "last_routed_at": None,
                "active_model": active,
                "fallback_chain": chain,
            }

        return {
            "has_served_request": False,
            "total_routed": 0,
            "last_routed_at": None,
            "active_model": None,
            "fallback_chain": [],
        }

    def classify_task(self, prompt_text: str) -> str:
        text_lower = prompt_text.lower()
        if any(kw in text_lower for kw in [
            "def ", "function", "class ", "import ", "const ", "var ", "let ",
            "sql", "select ", "github", "stack trace", "exception", "bug", "refactor",
            "regex", "algorithm", "html", "css", "dockerfile", "api"
        ]):
            return "code"

        if any(kw in text_lower for kw in [
            "prove", "theorem", "step by step", "think carefully", "solve this puzzle",
            "reasoning", "math", "calculate", "deduce", "logic problem"
        ]):
            return "reasoning"

        return "general"

    def evaluate_model_intelligence(
        self, model_name: str, context_length: int = 128000, task_type: str = "general"
    ) -> float:
        score = 0.0
        name_lower = model_name.lower()

        # MoE patterns
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
            b_matches = re.findall(r"(?:^|[^\w])(\d+(?:\.\d+)?)b(?:[^\w]|$)", name_lower)
            if b_matches:
                try:
                    sizes = [float(val) for val in b_matches]
                    max_b = max(sizes)
                    if max_b >= 100:
                        score += 350.0
                    elif max_b >= 60:
                        score += 270.0
                    elif max_b >= 25:
                        score += 180.0
                    elif max_b >= 12:
                        score += 120.0
                    elif max_b >= 6:
                        score += 60.0
                    else:
                        score += 15.0
                except Exception:
                    pass

        # Top-tier family boosts
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

        if any(term in name_lower for term in ["r1", "reason", "o1", "thought"]):
            score += 120.0

        if any(term in name_lower for term in ["coder", "coding", "starcoder"]):
            score += 130.0

        if task_type == "code":
            if any(term in name_lower for term in ["code", "coder", "deepseek", "qwen", "llama-3"]):
                score += 80.0
        elif task_type == "reasoning":
            if any(term in name_lower for term in ["r1", "reason", "120b", "70b", "gemini-2"]):
                score += 80.0

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
        Build an ordered list of candidate models for routing and fallback.
        Deduplicates by (provider, model_name).
        Enforces strict validation:
        - If an explicit model is requested and not found in pool -> raises ModelNotFoundError.
        - If an explicit model is found but context_length < estimated_tokens -> raises ContextLengthExceededError.
        """
        task_type = self.classify_task(prompt_text)
        all_models = await storage_db.get_active_models(self.db_path)

        # Get cooldown status per provider
        quota_states = await storage_db.get_all_quota_states(self.db_path)
        cooldown_providers = {q["provider"] for q in quota_states if q.get("in_cooldown", False)}

        enabled_providers = set()
        providers_dict = {
            "openrouter": self.config.providers.openrouter,
            "groq": self.config.providers.groq,
            "gemini": self.config.providers.gemini,
            "github": self.config.providers.github,
        }
        configured_providers = set()
        for p_name, p_cfg in providers_dict.items():
            if p_cfg and p_cfg.enabled:
                enabled_providers.add(p_name)
                if get_provider_api_key(p_name, self.config):
                    configured_providers.add(p_name)

        available_models = [
            m for m in all_models
            if m["provider"] in enabled_providers and m["provider"] not in cooldown_providers
        ]

        clean_req = requested_model.strip()
        is_auto_mode = clean_req.lower() in ("auto", "default", "best", "free", "")

        # EXPLICIT MODEL HANDLING (R2-09)
        if not is_auto_mode:
            # Check if model exists in pool at all (active or cooling down)
            matching_in_all = [m for m in all_models if m["id"] == clean_req or m["model_name"] == clean_req]
            if not matching_in_all:
                raise ModelNotFoundError(f"Model '{clean_req}' not found in active free model pool.")

            target_model = matching_in_all[0]
            # Context length check on explicit model
            if target_model.get("context_length", 128000) < estimated_tokens:
                raise ContextLengthExceededError(
                    f"Model '{clean_req}' context length ({target_model.get('context_length')}) "
                    f"is smaller than required request budget ({estimated_tokens} tokens)."
                )

            # Check if in cooldown
            if target_model["provider"] in cooldown_providers:
                # If target model is cooling down, fallback to other available models with sufficient context
                fallback_models = [
                    m for m in available_models
                    if m["id"] != target_model["id"] and m.get("context_length", 128000) >= estimated_tokens
                ]
                return fallback_models

            # Target model is primary, followed by fallback models for 429 resiliency
            fallback_models = [
                m for m in available_models
                if m["id"] != target_model["id"] and m.get("context_length", 128000) >= estimated_tokens
            ]
            return [target_model] + fallback_models

        # AUTO MODE: Filter and rank available models
        candidates: List[Dict[str, Any]] = []
        seen_canonical: Set[Tuple[str, str]] = set()

        priority_models = getattr(self.config.harvester, "priority_models", [])
        scored_candidates: List[Tuple[float, Dict[str, Any]]] = []

        for m in available_models:
            canonical = (m["provider"], m["model_name"])
            if canonical in seen_canonical:
                continue

            # Context window budget check
            if m.get("context_length", 128000) < estimated_tokens:
                continue

            score = 100.0

            # Provider credentials priority
            if m["provider"] in configured_providers:
                score += 500.0
            elif configured_providers:
                score -= 500.0

            context_len = m.get("context_length", 128000)
            score += self.evaluate_model_intelligence(m["model_name"], context_len, task_type)

            for rank_idx, p_id in enumerate(priority_models):
                if m["id"] == p_id or m["model_name"] == p_id or m["id"].endswith("/" + p_id):
                    score += (len(priority_models) - rank_idx) * 80.0
                    break

            latency = float(m.get("latency_ms", 200.0) or 200.0)
            score += max(0.0, 300.0 / (latency + 1.0))

            errors = int(m.get("error_count", 0))
            score -= (errors * 20.0)

            scored_candidates.append((score, m))

        scored_candidates.sort(key=lambda x: x[0], reverse=True)

        for _, m in scored_candidates:
            canonical = (m["provider"], m["model_name"])
            if canonical not in seen_canonical:
                candidates.append(m)
                seen_canonical.add(canonical)

        return candidates

    def resolve_provider_request(
        self, candidate: Dict[str, Any], payload: Dict[str, Any]
    ) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        provider = candidate["provider"]
        model_name = candidate["model_name"]

        providers_dict = {
            "openrouter": self.config.providers.openrouter,
            "groq": self.config.providers.groq,
            "gemini": self.config.providers.gemini,
            "github": self.config.providers.github,
        }

        pcfg = providers_dict.get(provider)
        if not pcfg or not pcfg.enabled:
            raise ValueError(f"Provider '{provider}' is disabled or unconfigured")

        base_url = pcfg.base_url.rstrip("/")
        target_url = f"{base_url}/chat/completions"

        api_key = get_provider_api_key(provider, self.config)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ai-free-gateway/1.0",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        if provider == "openrouter":
            headers["HTTP-Referer"] = "http://localhost:8000"
            headers["X-Title"] = "AI Free Gateway"

        req_payload = dict(payload)
        req_payload["model"] = model_name

        return target_url, headers, req_payload
