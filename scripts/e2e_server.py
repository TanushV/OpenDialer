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
from app.config import Settings
from app.main import create_app
from cryptography.fernet import Fernet

from scripts.disposable_database import validate_database_url

os.environ["OPENDIALER_SECRET_KEY"] = Fernet.generate_key().decode()
os.environ.pop("OPENDIALER_SECRET_KEY_FILE", None)

mode = sys.argv[1]
with tempfile.TemporaryDirectory(prefix="opendialer-e2e-") as directory:
    database_url = f"sqlite:///{directory}/test.db"
    if mode == "connected":
        database_url = validate_database_url(os.environ["OPENDIALER_E2E_DATABASE_URL"], purpose="browser")
    settings = Settings(
        mode=mode,
        simulation=True,
        data_dir=Path(directory),
        worker_enabled=True,
        database_url=database_url,
        local_token="e2e-local-token",
        bootstrap_token="e2e-bootstrap-token",
    )

    began = time.monotonic()

    def clock():
        return datetime(2026, 10, 14, 18, tzinfo=UTC) + timedelta(seconds=time.monotonic() - began)

    app = create_app(settings, clock=clock)
    if mode == "connected":
        app.state.db.reset()
        token = app.state.service.setup(
            {
                "seller_name": "Example Team",
                "campaign_name": "Team campaign",
                "email": "team@example.test",
                "password": "Connected-test-password-123",
            }
        )
        app.state.service.seed(
            {
                "tenant_id": token["tenant_id"],
                "seller_id": token["seller_id"],
                "user_id": token["user_id"],
                "role": "owner",
            }
        )
    uvicorn.run(
        app, host="127.0.0.1", port=8765 if mode == "local" else 8766, access_log=False, log_level="warning"
    )
