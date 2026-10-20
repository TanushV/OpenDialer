"""Native acceptance must preserve identity and exercise the delivered product."""

import importlib
import sqlite3
from pathlib import Path

import pytest


def native():
    return importlib.import_module("scripts.windows_native_smoke")


def test_native_harness_import_does_not_launch_or_require_an_installed_app(monkeypatch):
    monkeypatch.delenv("OPENDIALER_TEST_APPLICATION", raising=False)
    assert callable(native().main)


def test_native_child_environment_cannot_borrow_developer_tools_or_provider_keys():
    source = {
        "SystemRoot": r"C:\Windows",
        "APPDATA": r"C:\Users\runner\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\runner\AppData\Local",
        "USERPROFILE": r"C:\Users\runner",
        "TEMP": r"C:\Temp",
        "PATH": r"C:\Python;C:\node;C:\secret-tools",
        "OPENDIALER_OPENROUTER_API_KEY": "never-pass-this",
        "TELNYX_API_KEY": "never-pass-this-either",
        "OPENDIALER_DATABASE_URL": "an unrelated authority",
    }
    result = native().desktop_environment(source)
    assert result["APPDATA"] == source["APPDATA"]
    assert result["PATH"] == r"C:\Windows\System32"
    assert not any("never-pass" in value or "unrelated authority" in value for value in result.values())
    assert not any(key.startswith("OPENDIALER_") for key in result)


def test_saved_identity_inventory_is_read_only_and_has_no_raw_content(tmp_path):
    database = tmp_path / "fixture.db"
    tables = (
        "leads",
        "call_attempts",
        "notes",
        "suppressions",
        "followup_tasks",
        "sessions",
        "command_outbox",
    )
    with sqlite3.connect(database) as connection:
        for table in tables:
            connection.execute(f"CREATE TABLE {table}(id TEXT, data TEXT)")  # noqa: S608 - fixed fixture tables
            connection.execute(f"INSERT INTO {table} VALUES (?, ?)", (table + "-1", "secret raw sentinel"))  # noqa: S608
    result = native().saved_ids(database)
    assert result == {table: [table + "-1"] for table in tables}
    assert "secret raw sentinel" not in str(result)
    missing = tmp_path / "missing.db"
    with pytest.raises((FileNotFoundError, RuntimeError)):
        native().saved_ids(missing)
    assert not missing.exists()


@pytest.mark.parametrize(
    "changed", ["leads", "notes", "suppressions", "followup_tasks", "sessions", "command_outbox"]
)
def test_update_continuity_rejects_a_missing_identity(changed):
    before = {
        name: [name + "-1"]
        for name in ("leads", "notes", "suppressions", "followup_tasks", "sessions", "command_outbox")
    }
    after = {**before, changed: []}
    with pytest.raises(AssertionError, match="identity"):
        native().require_continuity(before, after)
    native().require_continuity(before, {**before, "new_derived_table": []})


@pytest.mark.parametrize("kind", ["local", "connected"])
def test_native_preflight_checks_the_actual_selected_authority(kind):
    text = f"Simulation checks passed · 0.3.0 · {kind}\nDatabase integrity ok"
    assert native().check_preflight(text, "0.3.0", kind) == "0.3.0"
    with pytest.raises(AssertionError):
        native().check_preflight(text, "0.2.1", kind)
    with pytest.raises(AssertionError):
        native().check_preflight(text, "0.3.0", "connected" if kind == "local" else "local")


def test_native_delivered_journey_keeps_both_modes_and_real_retention():
    module = native()
    assert module.RAW_EXPIRY_WAIT_SECONDS >= 301
    assert module.NATIVE_MODES == ("local", "connected")


def test_windows_package_acceptance_covers_both_advertised_formats():
    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/windows-acceptance.yml").read_text())
    assert set(workflow["jobs"]["native"]["strategy"]["matrix"]["installer"]) == {"nsis", "msi"}
    source = (root / ".github/workflows/windows.yml").read_text()
    assert "scripts/frozen_acceptance.py" in source
    assert "--expected-version" in source and "--output" in source


def test_release_builds_run_the_frozen_runtime_before_drafting():
    root = Path(__file__).resolve().parents[1]
    source = (root / ".github/workflows/release.yml").read_text()
    assert "scripts/frozen_acceptance.py" in source
    assert "--expected-version" in source and "--output" in source
