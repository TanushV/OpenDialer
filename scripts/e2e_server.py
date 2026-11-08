"""Ephemeral real-HTTP authorities for Playwright; no access to installed app data."""

import os
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/backend"))
import uvicorn
from app.main import create_app
from app.platform.config import Settings
from cryptography.fernet import Fernet

os.environ["OPENDIALER_SECRET_KEY"] = Fernet.generate_key().decode()
os.environ.pop("OPENDIALER_SECRET_KEY_FILE", None)

with tempfile.TemporaryDirectory(prefix="opendialer-e2e-") as directory:
    # Browser journeys never see the ChatGPT login Codex keeps for this computer's user.
    os.environ["CODEX_HOME"] = str(Path(directory) / "codex-home")
    database_url = f"sqlite:///{directory}/test.db"
    settings = Settings(
        simulation=True,
        data_dir=Path(directory),
        worker_enabled=True,
        database_url=database_url,
        local_token="e2e-local-token",
    )
    if origins := os.environ.get("OPENDIALER_E2E_ORIGIN"):
        # Browser-test workers serve their frontends on their own ports (apps/desktop/tests/fixtures.ts).
        settings.allowed_origins = [*settings.allowed_origins, *origins.split(",")]

    began = time.monotonic()

    def clock():
        return datetime(2026, 10, 14, 18, tzinfo=UTC) + timedelta(seconds=time.monotonic() - began)

    app = create_app(settings, clock=clock)
    # Browser tests give each worker its own port (apps/desktop/tests/fixtures.ts).
    port = int(os.environ.get("OPENDIALER_E2E_PORT") or 8765)
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
