"""Native acceptance must preserve identity and exercise the delivered product."""

import importlib
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


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


def test_review_requires_an_unambiguous_selected_journey_instead_of_history_order():
    from types import SimpleNamespace

    opened = []
    buttons = [
        SimpleNamespace(get_attribute=lambda _: "Review call Old lead", click=lambda: opened.append("old")),
        SimpleNamespace(
            get_attribute=lambda _: "Review call Current lead", click=lambda: opened.append("current")
        ),
    ]
    ui = native().NativeUI(Path("unused"), Path("unused"))
    ui.driver = SimpleNamespace(find_elements=lambda *_: buttons)
    ui.click = lambda _: None
    ui.visible = lambda _: None
    ui.fill = lambda *_: None
    ui.wait = lambda action, **_: action()
    ui.element = lambda selector: (
        buttons[0]
        if selector.startswith("button[")
        else SimpleNamespace(get_attribute=lambda _: "existing human note")
    )
    assert ui.review("Current lead") == "existing human note"
    assert opened == ["current"]
    buttons.append(buttons[-1])
    with pytest.raises(AssertionError, match="ambiguous"):
        ui.review("Current lead")


def test_followup_save_reacquires_the_remounted_row():
    from types import SimpleNamespace

    saved = []

    def old_identity(_):
        assert not saved, "Pre-save element was reused after remount"
        return "selected-call"

    before = SimpleNamespace(get_attribute=old_identity)
    after = SimpleNamespace(get_attribute=lambda _: "selected-call")
    ui = native().NativeUI(Path("unused"), Path("unused"))
    ui.element = lambda _: after if saved else before
    ui.click = lambda _: saved.append(True)
    ui.visible = lambda _: None
    ui.date = lambda *_, **__: None
    ui.field = lambda _, **__: SimpleNamespace(get_attribute=lambda _: "2026-10-22T14:00")
    assert ui.save_followup("2026-10-22T14:00") == "selected-call"


def test_recovery_assertion_uses_the_existing_visible_heading():
    import inspect

    assert '"Work needing recovery"' in inspect.getsource(native().check_journey_restart)


def test_msi_acceptance_extracts_its_own_bytes_and_checks_the_exact_manifest_entry():
    root = Path(__file__).resolve().parents[1]
    source = (root / ".github/workflows/windows-acceptance.yml").read_text()
    assert "TARGETDIR=" in source and "'/a'" in source
    assert "check_installer_manifest" in source
    assert "OPENDIALER_INSTALLER_KIND" in source


def test_installed_journey_requires_outcome_correction_and_next_call_context():
    import inspect

    journey = inspect.getsource(native().current_journey)
    restart = inspect.getsource(native().check_journey_restart)
    assert "correct_contact_role" in journey
    assert "check_next_call_context" in restart
    assert '"source_correction_verified"' in restart


def test_native_child_environment_keeps_system_root_with_windows_key_case():
    source = {"SYSTEMROOT": r"C:\Windows", "LOCALAPPDATA": r"C:\Users\u\AppData\Local", "PATH": r"C:\dev"}
    result = native().desktop_environment(source)
    assert result["SYSTEMROOT"] == r"C:\Windows" and result["LOCALAPPDATA"].endswith("Local")
    assert result["PATH"] == r"C:\Windows\System32"


def test_native_buttons_are_found_by_accessible_name_beside_a_count_badge():
    """The Leads navigation item reads "Leads 4" as text; its accessible name stays "Leads"."""
    m = native()
    queries = []

    class Button:
        def is_displayed(self):
            return True

        def is_enabled(self):
            return True

    class Driver:
        def find_elements(self, by, query):
            queries.append(query)
            return [Button()] if "@aria-label='Leads'" in query else []

    ui = m.NativeUI.__new__(m.NativeUI)
    ui.driver = Driver()
    ui.wait = lambda action, timeout=60: action()
    assert isinstance(ui.button("Leads"), Button)
    assert any("normalize-space(.)='Leads'" in q for q in queries)


def test_every_label_the_native_journey_uses_exists_in_the_desktop_source():
    """Removed or renamed controls broke the installed-app journey only on a Windows runner."""
    import re

    harness = (ROOT / "scripts/windows_native_smoke.py").read_text()
    labels = set(re.findall(r'\.(?:click|visible|field|button|date)\("([^"]+)"', harness))
    dynamic = {"1 imported", "HUMAN", "RINGING"}  # Rendered from data, not literal source text.
    source = "\n".join(p.read_text() for p in (ROOT / "apps/desktop/src").rglob("*.tsx")) + "\n".join(
        p.read_text() for p in (ROOT / "apps/desktop/src").rglob("*.ts")
    )
    missing = sorted(label for label in labels - dynamic if label not in source)
    assert labels and not missing, missing


def test_a_click_covered_by_a_transient_toast_is_retried():
    from selenium.common.exceptions import ElementClickInterceptedException

    m = native()
    attempts = []

    class Button:
        def is_displayed(self):
            return True

        def is_enabled(self):
            return True

        def click(self):
            attempts.append(True)
            if len(attempts) == 1:
                raise ElementClickInterceptedException("covered by a toast")

    class Driver:
        def find_elements(self, by, query):
            return [Button()]

        def execute_script(self, *args):
            return None

    ui = m.NativeUI.__new__(m.NativeUI)
    ui.driver = Driver()
    original = m.WebDriverWait

    class Immediate(original):
        def __init__(self, driver, timeout, **kwargs):
            super().__init__(driver, 1, poll_frequency=0.01, **kwargs)

    m.WebDriverWait = Immediate
    try:
        ui.click("Start call")
    finally:
        m.WebDriverWait = original
    assert len(attempts) == 2
