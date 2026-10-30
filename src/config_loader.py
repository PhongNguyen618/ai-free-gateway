from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Literal, Optional
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env if present in current directory, project root, or config directory
load_dotenv(dotenv_path=PROJECT_ROOT / "config" / ".env")
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")
load_dotenv()


class ServerConfig(BaseModel):
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8000, ge=1, le=65535)
    debug: bool = Field(default=False)
    gateway_api_key: str = Field(default="")
    cors_allowed_origins: List[str] = Field(default_factory=list)
    allowed_hosts: List[str] = Field(default_factory=list)
    allow_insecure_external: bool = Field(default=False)
    record_pid: bool = Field(default=True)
    max_body_bytes: int = Field(default=2097152, gt=0)  # 2 MiB
    max_concurrent_completions: int = Field(default=8, gt=0)
    max_pending_approvals: int = Field(default=32, gt=0)
    max_attempts: int = Field(default=3, ge=1)
    first_event_timeout_seconds: float = Field(default=30.0, gt=0)
    idle_timeout_seconds: float = Field(default=60.0, gt=0)
    max_stream_duration_seconds: float = Field(default=600.0, gt=0)
    total_completion_timeout_seconds: float = Field(default=120.0, gt=0)
    max_sse_line_bytes: int = Field(default=262144, gt=0)  # 256 KiB
    max_sse_event_bytes: int = Field(default=1048576, gt=0)  # 1 MiB
    max_tool_buffer_bytes: int = Field(default=1048576, gt=0)  # 1 MiB


class EgressConfig(BaseModel):
    whitelist_domains: List[str] = Field(
        default_factory=lambda: [
            "openrouter.ai",
            "api.groq.com",
            "generativelanguage.googleapis.com",
            "models.inference.ai.azure.com",
            "api.github.com",
        ]
    )
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_request_bytes: int = Field(default=10485760, gt=0)  # 10MB
    max_response_bytes: int = Field(default=16777216, gt=0)  # 16 MiB
    block_private_ips: bool = Field(default=True)
    allow_http: bool = Field(default=False)


class EntropyScanConfig(BaseModel):
    enabled: bool = Field(default=True)
    threshold: float = Field(default=4.5, ge=0.0)
    min_length: int = Field(default=20, ge=1)


class RiskActionConfig(BaseModel):
    high: Literal["reject", "approve", "redact"] = Field(default="reject")
    medium: Literal["approve", "redact", "allow"] = Field(default="approve")
    low: Literal["redact", "allow"] = Field(default="redact")


class DLPConfig(BaseModel):
    enabled: bool = Field(default=True)
    entropy_scan: EntropyScanConfig = Field(default_factory=EntropyScanConfig)
    risk_action: RiskActionConfig = Field(default_factory=RiskActionConfig)
    pseudonymize_reversible: bool = Field(default=True)
    approval_timeout_seconds: int = Field(default=60, gt=0)
    approval_timeout_action: Literal["reject", "redact"] = Field(default="reject")
    redacted_token_format: str = Field(default="<REDACTED_{TYPE}_{INDEX}>")
    custom_blocklist: List[str] = Field(default_factory=list)


class StaticModelConfig(BaseModel):
    id: str
    context_length: int = Field(default=128000, gt=0)


class ProviderDetail(BaseModel):
    enabled: bool = Field(default=True)
    base_url: str
    models_endpoint: Optional[str] = None
    api_key_env: str
    cooldown_seconds_on_429: int = Field(default=300, ge=0)
    free_suffix: Optional[str] = None
    static_free_models: List[StaticModelConfig] = Field(default_factory=list)


class ProvidersConfig(BaseModel):
    openrouter: Optional[ProviderDetail] = None
    groq: Optional[ProviderDetail] = None
    gemini: Optional[ProviderDetail] = None
    github: Optional[ProviderDetail] = None


class HarvesterConfig(BaseModel):
    poll_interval_seconds: int = Field(default=300, ge=1)
    auto_discover_openrouter: bool = Field(default=True)
    health_check_interval_seconds: int = Field(default=600, ge=1)
    priority_models: List[str] = Field(
        default_factory=lambda: [
            "groq/openai/gpt-oss-120b",
            "groq/qwen/qwen3.8-27b",
            "openrouter/deepseek/deepseek-r1:free",
            "openrouter/qwen/qwen-2.5-coder-32b-instruct:free",
            "openrouter/meta-llama/llama-3.3-70b-instruct:free",
            "gemini/gemini-2.0-flash-exp",
            "gemini/gemini-1.5-flash",
            "groq/openai/gpt-oss-20b",
            "groq/llama-3.1-8b-instant",
        ]
    )
    default_fallback_models: List[str] = Field(
        default_factory=lambda: [
            "groq/openai/gpt-oss-120b",
            "groq/qwen/qwen3.8-27b",
            "openrouter/auto",
            "gemini/gemini-1.5-flash",
        ]
    )


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    egress: EgressConfig = Field(default_factory=EgressConfig)
    dlp: DLPConfig = Field(default_factory=DLPConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    harvester: HarvesterConfig = Field(default_factory=HarvesterConfig)
    db_path: str = Field(default="storage/gateway.db")

    def deep_copy(self) -> AppConfig:
        return self.model_copy(deep=True)


def load_yaml_config(config_path: Optional[str] = None) -> dict:
    if config_path:
        p = Path(config_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"Specified configuration file does not exist: {config_path}")
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data or {}

    env_path = os.getenv("CONFIG_PATH")
    if env_path:
        p = Path(env_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"CONFIG_PATH environment file does not exist: {env_path}")
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data or {}

    candidates = [
        PROJECT_ROOT / "config" / "settings.yaml",
        PROJECT_ROOT / "settings.yaml",
        Path("config/settings.yaml"),
        Path("settings.yaml"),
    ]

    for p in candidates:
        if p.exists() and p.is_file():
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                return data or {}
    return {}


def _build_config(config_path: Optional[str] = None) -> AppConfig:
    raw_data = load_yaml_config(config_path)

    # Allow environment variables override
    if os.getenv("GATEWAY_HOST"):
        raw_data.setdefault("server", {})["host"] = os.getenv("GATEWAY_HOST")
    if os.getenv("GATEWAY_PORT"):
        try:
            raw_data.setdefault("server", {})["port"] = int(os.getenv("GATEWAY_PORT"))
        except ValueError:
            pass
    if os.getenv("GATEWAY_API_KEY"):
        raw_data.setdefault("server", {})["gateway_api_key"] = os.getenv("GATEWAY_API_KEY")
    if os.getenv("SQLITE_DB_PATH"):
        raw_data["db_path"] = os.getenv("SQLITE_DB_PATH")

    return AppConfig(**raw_data)


@lru_cache(maxsize=4)
def _get_base_config_cached(config_path: Optional[str] = None) -> AppConfig:
    return _build_config(config_path)


def get_config(config_path: Optional[str] = None) -> AppConfig:
    """Always return an isolated deep copy to prevent tests from mutating shared state."""
    base = _get_base_config_cached(config_path)
    return base.deep_copy()


def get_provider_api_key(provider_name: str, config: AppConfig) -> str:
    """Retrieve API key for a given provider from environment variables."""
    provider_dict = config.providers.model_dump()
    provider_info = provider_dict.get(provider_name.lower())
    if not provider_info:
        return ""
    api_key_env = provider_info.get("api_key_env", "")
    if not api_key_env:
        return ""
    val = os.getenv(api_key_env, "").strip()
    return val.strip('"').strip("'")
