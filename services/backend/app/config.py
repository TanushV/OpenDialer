from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENDIALER_", extra="ignore")
    mode: Literal["local", "connected"] = "local"
    database_url: str = "sqlite:///opendialer.db"
    data_dir: Path = Path("work/data")
    local_token: str = ""
    bootstrap_token: str = ""
    secret_key: str = ""
    simulation: bool = False
    worker_enabled: bool = True
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
    version: str = "0.2.0"
