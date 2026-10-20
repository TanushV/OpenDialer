"""The desktop authority boots without the fitting stack, PostgreSQL drivers or lazy extras.

A onefile sidecar re-extracts its native libraries on every launch and macOS verifies each one
on first load, so whatever authority startup imports is paid on every launch.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

DEFERRED = [
    "numpy",
    "scipy",
    "sklearn",
    "joblib",
    "threadpoolctl",
    "psycopg",
    "psycopg_binary",
    "bs4",
    "charset_normalizer",
    "pwdlib",
    "argon2",
    "phonenumbers.geocoder",
    "phonenumbers.carrier",
    "phonenumbers.timezone",
]


def test_authority_startup_imports_none_of_the_deferred_libraries(tmp_path):
    script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "import app.cli, app.main\n"
        "from app.config import Settings\n"
        "settings = Settings(data_dir=Path(sys.argv[1]), mode='local', local_token='x' * 40,\n"
        "                    secret_key='y' * 64, simulation=True)\n"
        "settings.database_url = 'sqlite:///' + str(Path(sys.argv[1]) / 'opendialer.db')\n"
        "app.main.create_app(settings).state.db.engine.dispose()\n"
        f"print(json.dumps(sorted(n for n in {DEFERRED!r} if n in sys.modules)))\n"
    )
    result = subprocess.run(  # noqa: S603 - current interpreter, fixed probe script
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    assert json.loads(result.stdout.splitlines()[-1]) == []


def test_login_still_hashes_and_verifies_with_argon2():
    from app.security import PASSWORDS, dummy_hash

    hashed = PASSWORDS.hash("correct horse")
    assert hashed.startswith("$argon2")
    assert PASSWORDS.verify("correct horse", hashed)
    assert not PASSWORDS.verify("wrong", hashed)
    assert dummy_hash() is dummy_hash() and dummy_hash().startswith("$argon2")


def test_frozen_authority_refuses_connected_mode_with_a_clear_reason(monkeypatch, tmp_path):
    from argparse import Namespace

    from app import cli
    from app.config import Settings

    url = "postgresql+psycopg://authority.invalid/opendialer"
    monkeypatch.setenv("OPENDIALER_DATABASE_URL", url)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    settings = Settings(data_dir=tmp_path, mode="connected", database_url=url, secret_key="y" * 64)
    with pytest.raises(SystemExit, match="server image"):
        cli.run(Namespace(command="serve"), settings, False)
