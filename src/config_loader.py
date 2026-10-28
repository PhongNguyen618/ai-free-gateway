from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Load .env if present in current directory or config directory
load_dotenv(dotenv_path=Path("config/.env"))
load_dotenv()


class ServerConfig(BaseModel):
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8000)
    debug: bool = Field(default=False)
    gateway_api_key: str = Field(default="")


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
    timeout_seconds: float = Field(default=60.0)
    max_request_bytes: int = Field(default=10485760)  # 10MB
    block_private_ips: bool = Field(default=True)


class EntropyScanConfig(BaseModel):
    enabled: bool = Field(default=True)
    threshold: float = Field(default=4.5)
    min_length: int = Field(default=20)


class RiskActionConfig(BaseModel):
    high: str = Field(default="reject")      # reject | approve | redact
    medium: str = Field(default="approve")   # approve | redact | allow
    low: str = Field(default="redact")       # redact | allow


class DLPConfig(BaseModel):
    enabled: bool = Field(default=True)
    entropy_scan: EntropyScanConfig = Field(default_factory=EntropyScanConfig)
    risk_action: RiskActionConfig = Field(default_factory=RiskActionConfig)
    pseudonymize_reversible: bool = Field(default=True)
    approval_timeout_seconds: int = Field(default=60)
    approval_timeout_action: str = Field(default="reject")
    redacted_token_format: str = Field(default="<REDACTED_{TYPE}_{INDEX}>")
    custom_blocklist: List[str] = Field(default_factory=list)


class StaticModelConfig(BaseModel):
    id: str
    context_length: int = Field(default=128000)


class ProviderDetail(BaseModel):
    enabled: bool = Field(default=True)
    base_url: str
    models_endpoint: Optional[str] = None
    api_key_env: str
    cooldown_seconds_on_429: int = Field(default=300)
    free_suffix: Optional[str] = None
    static_free_models: List[StaticModelConfig] = Field(default_factory=list)


class ProvidersConfig(BaseModel):
    openrouter: Optional[ProviderDetail] = None
    groq: Optional[ProviderDetail] = None
    gemini: Optional[ProviderDetail] = None
    github: Optional[ProviderDetail] = None


class HarvesterConfig(BaseModel):
    poll_interval_seconds: int = Field(default=300)
    auto_discover_openrouter: bool = Field(default=True)
    health_check_interval_seconds: int = Field(default=600)
    default_fallback_models: List[str] = Field(
        default_factory=lambda: [
            "groq/llama-3.3-70b-versatile",
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


def load_yaml_config(config_path: Optional[str] = None) -> dict:
    candidates = []
    if config_path:
        candidates.append(Path(config_path))
    env_path = os.getenv("CONFIG_PATH")
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend([
        Path("config/settings.yaml"),
        Path("settings.yaml"),
    ])

    for p in candidates:
        if p.exists() and p.is_file():
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                return data or {}
    return {}


@lru_cache()
def get_config(config_path: Optional[str] = None) -> AppConfig:
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


def get_provider_api_key(provider_name: str, config: AppConfig) -> str:
    """Retrieve API key for a given provider from environment variables."""
    provider_dict = config.providers.model_dump()
    provider_info = provider_dict.get(provider_name.lower())
    if not provider_info:
        return ""
    api_key_env = provider_info.get("api_key_env", "")
    if not api_key_env:
        return ""
    return os.getenv(api_key_env, "").strip()
