from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
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


class RuntimeLimits(BaseModel):
    """One immutable set of engineering bounds; domain policies remain seller owned."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    live_active: int = Field(8, ge=1, le=32)
    live_pending: int = Field(32, ge=1, le=256)
    live_per_rep: int = Field(2, ge=1, le=4)
    prospect_debounce_ms: int = Field(300, ge=0, le=500)
    automatic_per_call_minute: int = Field(30, ge=1, le=60)
    background_http: int = Field(4, ge=1, le=16)
    company_units: int = Field(2, ge=1, le=4)
    script_generations: int = Field(2, ge=1, le=4)
    postcall_active: int = Field(2, ge=1, le=8)
    automatic_deadline_ms: int = Field(1500, ge=1, le=5000)
    manual_connect_ms: int = Field(1000, ge=1, le=15000)
    manual_first_ms: int = Field(3000, ge=1, le=15000)
    manual_full_ms: int = Field(8000, ge=1, le=15000)
    conversation_bytes: int = Field(65536, ge=1024, le=262144)
    observation_count: int = Field(48, ge=1, le=96)
    observation_per_kind: int = Field(12, ge=1, le=24)
    review_fields: int = Field(20, ge=1, le=40)
    review_interval_seconds: int = Field(30, ge=5, le=60)
    research_pages: int = Field(3, ge=1, le=3)
    research_bytes: int = Field(1048576, ge=1, le=1048576)
    research_searches: int = Field(2, ge=0, le=2)
    planning_grid_seconds: int = Field(30, ge=30, le=30)
    planning_time_buckets: int = Field(10000, ge=1, le=10000)
    planning_operations: int = Field(200000, ge=1, le=200000)
    planning_nodes: int = Field(20000, ge=1, le=20000)
    projection_batch: int = Field(100, ge=1, le=100)
    rebuild_batch: int = Field(500, ge=1, le=500)
    registry_lru_bytes: int = Field(16777216, ge=1, le=16777216)

    @model_validator(mode="after")
    def ordered_deadlines(self):
        if not self.manual_connect_ms <= self.manual_first_ms <= self.manual_full_ms:
            raise ValueError("Manual deadlines must satisfy connect <= first useful <= full")
        return self


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
    runtime_limits: RuntimeLimits = Field(default_factory=RuntimeLimits)
    mode: Literal["local", "connected"] = "local"
    database_url: str = "sqlite:///opendialer.db"
    data_dir: Path = Path("work/data")
    backup_copy_dir: Path | None = None
    local_token: str = ""
    bootstrap_token: str = ""
    secret_key: str = ""
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
    version: str = "0.3.2"
