from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    inference_mode: Literal["api", "litellm", "chatgpt_plan"] = "api"
    model: str = "gpt-6-luna"
    plan_model: str = "gpt-6.1-sol"
    coach_provider: Literal["openrouter", "static"] = "openrouter"
    coach_model: str = "openai/gpt-oss-120b"
    research_search: Literal["disabled", "openai_web_search"] = "disabled"
    # Budget hold for one optional metered request before its receipt settles the actual cost.
    optional_request_estimate_usd: float = 0.10


PROVIDER_DEFAULTS = ProviderDefaults()


def inference_model(config, role=None):
    default = (
        PROVIDER_DEFAULTS.plan_model
        if config.get("inference_mode") == "chatgpt_plan"
        else PROVIDER_DEFAULTS.model
    )
    return config.get(f"{role}_model") or config.get("model") or default


class LIMITS:
    """Engineering bounds; domain policies remain seller owned."""

    live_active = 8
    live_pending = 32
    live_per_rep = 2
    prospect_debounce_ms = 300
    automatic_per_call_minute = 30
    company_units = 2
    script_generations = 2
    postcall_active = 2
    automatic_deadline_ms = 1500
    manual_connect_ms = 1000
    manual_first_ms = 3000
    manual_full_ms = 8000
    conversation_bytes = 65536
    observation_count = 48
    observation_per_kind = 12
    review_fields = 20
    review_interval_seconds = 30
    research_pages = 3
    research_bytes = 1048576
    research_searches = 2


class CapabilityStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    state: Literal[
        "unconfigured", "configured", "software_verified", "live_verified", "degraded", "unavailable"
    ]
    blocks: Literal["voice", "transcription", "help", "preparation", "none"]
    reason_codes: list[str]
    evidence_at: str | None = None
    remedy: str
    source_version: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENDIALER_", env_nested_delimiter="__", extra="ignore")
    mode: Literal["local", "connected"] = "local"
    database_url: str = "sqlite:///opendialer.db"
    data_dir: Path = Path("work/data")
    backup_copy_dir: Path | None = None
    local_token: str = ""
    bootstrap_token: str = ""
    simulation: bool = False
    worker_enabled: bool = True
    worker_queues: list[
        Literal[
            "webhook", "live_coach", "postcall", "maintenance", "call_control", "research", "scripts", "ml"
        ]
    ] = ["webhook", "live_coach", "postcall", "maintenance", "research", "scripts", "ml"]
    allowed_origins: list[str] = [
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
        "http://127.0.0.1:1420",
    ]
    webhook_public_url: str = ""
    max_call_seconds: int = 7200
    session_seconds: int = 28800
    max_import_rows: int = 10000
    version: str = "0.4.0"
