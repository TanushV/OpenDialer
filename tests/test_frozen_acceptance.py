"""The frozen-runtime acceptance tool must itself enforce its advertised boundary."""

import importlib
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_importing_frozen_acceptance_does_not_parse_cli_or_launch_an_authority():
    result = subprocess.run(
        [sys.executable, "-c", "import scripts.frozen_acceptance"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def module():
    return importlib.import_module("scripts.frozen_acceptance")


def test_packaged_process_cannot_inherit_source_runtime_or_provider_credentials():
    source = {
        "PATH": "/developer/python:/developer/node",
        "PYTHONPATH": str(ROOT),
        "PYTHONHOME": "/developer/python",
        "VIRTUAL_ENV": "/developer/venv",
        "TELNYX_API_KEY": "synthetic-not-a-key",
        "OPENROUTER_API_KEY": "synthetic-not-a-key",
        "OPENDIALER_DATABASE_URL": "sqlite:///not-the-fixture.db",
        "SystemRoot": "C:\\Windows",
        "TEMP": "/fixture-temp",
        "LANG": "en_US.UTF-8",
    }
    result = module().isolated_environment(source)
    assert result["PATH"] == ""
    assert not any(k in result for k in source if k not in {"PATH", "SystemRoot", "TEMP", "LANG"})
    assert result["SystemRoot"] == "C:\\Windows"
    assert result["OPENBLAS_NUM_THREADS"] == "1"


def test_frozen_health_does_not_accept_the_backend_as_its_own_version_oracle():
    with (
        httpx.Client(
            base_url="http://fixture",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"version": "0.2.1"})),
        ) as client,
        pytest.raises(ValueError, match="version"),
    ):
        module().await_ready(client, "0.3.0", timeout=0.1)


def test_windows_cleanup_targets_only_the_started_process_tree(monkeypatch, tmp_path):
    from types import SimpleNamespace

    calls = []
    process = SimpleNamespace(pid=43210, poll=lambda: None, wait=lambda **kwargs: 0)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or SimpleNamespace(returncode=0),
    )
    module().stop_owned(process, platform_name="nt", system_root=tmp_path)
    command = calls[0][0]
    assert Path(command[0]) == tmp_path / "System32" / "taskkill.exe"
    assert command[1:] == ["/PID", "43210", "/T", "/F"]
    assert not calls[0][1].get("shell", False)


def test_cleanup_does_not_signal_an_already_reaped_process(monkeypatch):
    from types import SimpleNamespace

    def forbidden(*args, **kwargs):
        raise AssertionError("An ended process must not authorize a reused PID")

    monkeypatch.setattr(os, "killpg", forbidden, raising=False)
    monkeypatch.setattr(subprocess, "run", forbidden)
    module().stop_owned(SimpleNamespace(pid=43210, poll=lambda: 0))


@pytest.mark.parametrize("change", ["checksum", "pid", "threads", "artifact"])
def test_training_proof_rejects_wrong_executable_receipt(tmp_path, change):
    import hashlib
    import json

    artifact = tmp_path / "challenger.joblib"
    artifact.write_bytes(b"synthetic-artifact-not-deserialized")
    result = {
        "artifact": str(artifact),
        "checksum": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "training_runtime": {"pid": 54321, "native_threads": 1, "priority": "low"},
    }
    if change == "checksum":
        result["checksum"] = "0" * 64
    elif change == "pid":
        result["training_runtime"]["pid"] = 43210
    elif change == "threads":
        result["training_runtime"]["native_threads"] = 8
    else:
        result["artifact"] = str(tmp_path / "unrelated.joblib")
    with pytest.raises(ValueError):
        module().check_training_reply(json.dumps(result), tmp_path, 43210)
