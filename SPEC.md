# PROJECT SPECIFICATION: `ai-free-gateway`
## Role & Objective
You are a Senior Backend & Security Engineer. Build a production-ready, modular Python project named `ai-free-gateway`. 
The system is a **Local Zero-Trust OpenAI-Compatible Proxy & Free-Tier AI Harvester**. It automatically discovers/routes requests to free-tier LLM APIs (OpenRouter `:free`, Groq, Gemini Free, GitHub Models) while enforcing strict local Data Loss Prevention (DLP) so sensitive data, secrets, and internal IPs/code never leave the machine unauthorized.

## Strict Coding Rules (CRITICAL)
1. **NO PLACEHOLDERS**: Write complete, working code. Never use `# TODO`, `pass`, or mock implementations.
2. **ZERO EXTERNAL TELEMETRY**: All DLP scanning, regex matching, and database operations MUST run 100% locally offline.
3. **STREAMING SUPPORT**: The proxy must support both standard JSON responses and Server-Sent Events (`stream=True`) while still applying sanitization.
4. **MODULAR DESIGN**: Follow the exact directory structure specified below. Use Python 3.11+, type hints, and `pydantic` v2.

---

## 1. Tech Stack
- **Framework**: `FastAPI` + `uvicorn` (Async REST API)
- **HTTP Client**: `httpx` (Async client with strict domain whitelist & `follow_redirects=False`)
- **Database**: `sqlite3` / `aiosqlite` (Local state for provider quotas, cooldowns, and DLP audit logs)
- **Config**: `PyYAML` + `pydantic-settings`
- **Frontend UI**: Single-file Vanilla HTML/TailwindCSS/JS (`ui/index.html`) served directly by FastAPI for real-time monitoring and Human-in-the-Loop request approval.

---

## 2. Directory Structure
Generate the project with this exact structure:
```text
ai-free-gateway/
├── config/
│   ├── settings.yaml            # Providers config, rate limits, whitelist domains, DLP blocklist
│   └── .env.example             # API keys template (OPENROUTER_API_KEY, GROQ_API_KEY, etc.)
├── src/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app initialization, CORS, static UI mount, background tasks
│   ├── config_loader.py         # Pydantic models for loading and validating settings.yaml & .env
│   ├── storage/
│   │   └── db.py                # SQLite setup: tables for `models_pool`, `quota_state`, `dlp_audit_logs`
│   ├── security/
│   │   ├── sanitizer.py         # Regex + Shannon Entropy scanner, reversible pseudonymization (<ENTITY_ID>)
│   │   ├── approval_gate.py     # Async queue for Human-in-the-Loop approval when medium-risk data is detected
│   │   └── egress_guard.py      # httpx wrapper that blocks non-whitelisted domains & logs outbound byte size
│   ├── discovery/
│   │   └── harvester.py         # Polls OpenRouter `/api/v1/models` for $0 models & checks provider health/quotas
│   └── router/
│       ├── balancer.py          # Selects best free model based on task, latency, privacy tier, and 429 cooldown
│       └── openai_proxy.py      # `/v1/chat/completions` & `/v1/models` endpoints
├── ui/
│   └── index.html               # Clean Dark-Mode Dashboard (Quota status, DLP Audit Logs, Pending Approvals)
└── requirements.txt