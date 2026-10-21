"""Native acceptance must preserve identity and exercise the delivered product."""

import html
import importlib
import inspect
import sqlite3
import sys
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
        SimpleNamespace(
            get_attribute=lambda _: "Review call Old lead",
            click=lambda: opened.append("old"),
            is_displayed=lambda: True,
        ),
        SimpleNamespace(
            get_attribute=lambda _: "Review call Current lead",
            click=lambda: opened.append("current"),
            is_displayed=lambda: True,
        ),
    ]

    def find_elements(by, query):
        exact = [b for b in buttons if f'[aria-label="{b.get_attribute("aria-label")}"]' in query]
        return exact if 'aria-label="' in query else buttons

    ui = native().NativeUI(Path("unused"), Path("unused"))
    ui.driver = SimpleNamespace(find_elements=find_elements, execute_script=lambda *_: None)
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
    labels = set(re.findall(r'\.(?:click|visible|field|button|date|open_details)\("([^"]+)"', harness))
    dynamic = {"1 imported", "HUMAN", "RINGING"}  # Rendered from data, not literal source text.
    source = "\n".join(p.read_text() for p in (ROOT / "apps/desktop/src").rglob("*.tsx")) + "\n".join(
        p.read_text() for p in (ROOT / "apps/desktop/src").rglob("*.ts")
    )
    source = html.unescape(source)  # JSX writes "&" in text as "&amp;".
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


def test_the_collapsed_live_transcript_is_opened_once_before_its_text_is_read():
    """The ephemeral transcript is an optional <details> expansion; clicking an open one closes it."""
    m = native()
    clicks = []

    class Details:
        def __init__(self):
            self.open = False

        def get_attribute(self, name):
            return "" if name == "open" and self.open else None

    details = Details()

    class Summary:
        def is_displayed(self):
            return True

        def find_element(self, by, query):
            return details

        def click(self):
            clicks.append(True)
            details.open = not details.open

    class Driver:
        def find_elements(self, by, query):
            return [Summary()] if "summary" in query and "Live transcript" in query else []

        def execute_script(self, *args):
            return None

    ui = m.NativeUI.__new__(m.NativeUI)
    ui.driver = Driver()
    ui.wait = lambda action, timeout=60: action()
    ui.open_details("Live transcript")
    ui.open_details("Live transcript")
    assert details.open and len(clicks) == 1
    journey = inspect.getsource(m.current_journey)
    assert journey.index('ui.open_details("Live transcript")') < journey.index("ui.visible(raw)")


def test_a_control_under_the_call_dock_is_scrolled_into_view_before_its_click():
    """The keep-present click on the simulation input was covered by the call dock at 1024x720."""
    from selenium.common.exceptions import ElementClickInterceptedException

    m = native()
    state = {"scrolled": False, "clicked": 0}

    class Input:
        def is_displayed(self):
            return True

        def click(self):
            if not state["scrolled"]:
                raise ElementClickInterceptedException("Other element would receive the click: dock-tool")
            state["clicked"] += 1

    class Driver:
        def find_elements(self, by, query):
            return [Input()] if query == 'input[aria-label="Simulation transcript"]' else []

        def execute_script(self, script, *args):
            state["scrolled"] = state["scrolled"] or "scrollIntoView" in script

    ui = m.NativeUI.__new__(m.NativeUI)
    ui.driver = Driver()
    original = m.WebDriverWait

    class Immediate(original):
        def __init__(self, driver, timeout, **kwargs):
            super().__init__(driver, 1, poll_frequency=0.01, **kwargs)

    m.WebDriverWait = Immediate
    try:
        ui.press('input[aria-label="Simulation transcript"]')
    finally:
        m.WebDriverWait = original
    assert state == {"scrolled": True, "clicked": 1}
    source = (ROOT / "scripts/windows_native_smoke.py").read_text()
    assert "ui.element(" not in "\n".join(
        line for line in source.splitlines() if line.rstrip().endswith(".click()")
    )


def test_wrap_up_uses_the_rep_visible_outcome_chip_and_the_zoned_callback_label():
    """The Disposition select sits in a collapsed More outcomes section; the callback label
    carries its time-zone caption. The journey uses what a rep sees."""
    journey = inspect.getsource(native().current_journey)
    correction = inspect.getsource(native().NativeUI.correct_contact_role)
    assert 'ui.click("Talked: callback requested")' in journey
    assert 'field("Disposition")' not in journey + correction
    assert 'ui.date("Confirmed callback time", "2026-10-21T14:00", prefix=True)' in journey
    source = "\n".join(p.read_text() for p in (ROOT / "apps/desktop/src/components").glob("WrapUp.tsx"))
    assert (
        '"Talked: callback requested"' in source and "Confirmed callback time ({zoneCaption(zone)})" in source
    )


def test_native_preflight_readies_the_rep_first_as_a_person_would_after_restart():
    """After a restart the rep is not audio-ready, so preflight honestly reports a blocker;
    the journey goes ready before it expects the simulation checks to pass."""
    source = inspect.getsource(native().NativeUI.preflight)
    assert source.index('self.click("Go ready")') < source.index('self.click("Run preflight")')
    # Pausing again goes through the Dialer; the journey's next step is a Settings tab.
    assert source.rindex("self.pause()") < source.rindex('self.click("Settings")')


def test_reading_the_page_while_the_document_is_replaced_is_retried():
    """Reopening the app replaced the document between finding <body> and reading it."""
    from selenium.common.exceptions import WebDriverException

    m = native()
    reads = []

    class Body:
        @property
        def text(self):
            reads.append(True)
            if len(reads) == 1:
                raise WebDriverException(
                    'unknown error: unhandled inspector error: {"code":-32000,'
                    '"message":"Node with given id does not belong to the document"}'
                )
            return "SIMULATION"

    class Driver:
        def find_element(self, by, value):
            return Body()

    ui = m.NativeUI.__new__(m.NativeUI)
    ui.driver = Driver()
    ui.wait = lambda action, timeout=60: next(v for v in iter(action, None) if v)
    assert ui.body() == ""
    assert ui.body() == "SIMULATION"

    class Closed(Driver):
        def find_element(self, by, value):
            raise WebDriverException("chrome not reachable")

    ui.driver = Closed()
    with pytest.raises(WebDriverException):
        ui.body()


def test_the_connected_journey_serves_from_the_server_runtime_not_the_desktop_sidecar():
    """The slim desktop sidecar has no PostgreSQL driver and refuses connected mode; the
    native check is the installed desktop connecting to a server, so the harness runs the
    server from the checkout as the server image does."""
    m = native()
    command = m.connected_server_command(4321, "C:/temp/authority")
    assert command[:4] == [sys.executable, "-m", "app.cli", "serve"]
    assert "opendialer-backend" not in " ".join(command)
    assert command[command.index("--mode") + 1] == "connected"
    assert command[command.index("--port") + 1] == "4321"


def test_a_choice_card_button_is_found_by_its_title():
    """Welcome choice cards read "Connect to a server Shared leads, ..." as text; the title
    is the card's <strong> element."""
    m = native()
    queries = []

    class Driver:
        def find_elements(self, by, query):
            queries.append(query)
            return []

    ui = m.NativeUI.__new__(m.NativeUI)
    ui.driver = Driver()
    ui.wait = lambda action, timeout=60: action()
    ui.button("Connect to a server")
    assert any("normalize-space(.//strong)='Connect to a server'" in q for q in queries)


def test_after_an_update_a_lost_session_is_told_apart_from_a_reset_driver_profile():
    """A probe planted before the update decides whether the app itself had to restore its
    saved session. Run 39666476735 kept the profile, so a lost session is the app's failure."""
    source = inspect.getsource(native().upgrade)
    assert "localStorage.setItem('opendialer.harness-profile'" in source
    assert source.index("if kept:") < source.index('if "SIMULATION" not in ui.body():')
    assert '"session_restored": kept' in source


def test_the_install_probe_waits_through_files_an_installer_has_briefly_removed():
    """An MSI update removes the old executables before writing new ones; reading a missing
    desktop or backend is "not installed yet", not a failed probe."""
    probe = inspect.getsource(native().wait_installed)
    assert "if(Test-Path $env:OPENDIALER_INSTALL_PATH)" in probe
    assert "try {$hash=(Get-FileHash" in probe


class LegacyDialer:
    """0.2.1's simulated dialer, which can drop a ready or next-lead request."""

    class Reached(Exception):
        pass

    def __init__(self, dropped_ready=0, dropped_lead=0):
        self.dropped = {"Go ready": dropped_ready, "Get next lead": dropped_lead}
        self.ready = self.lead = False

    def body(self):
        if not self.ready:
            return "SIMULATION Go ready"
        return "SIMULATION Pause dialing " + ("Start call" if self.lead else "Get next lead")

    def click(self, label):
        if label == "Start call":
            raise self.Reached
        if self.dropped.get(label):
            self.dropped[label] -= 1
        elif label == "Go ready":
            self.ready = True
        elif label == "Get next lead":
            self.lead = True

    def wait(self, action, timeout=60):
        from selenium.common.exceptions import TimeoutException

        if not action():
            raise TimeoutException("synthetic")
        return True

    def visible(self, text):
        return self.wait(lambda: text in self.body())

    def element(self, _selector):
        return type("Lead", (), {"text": "Synthetic lead"})()


@pytest.mark.parametrize(("dropped_ready", "dropped_lead"), [(0, 0), (1, 0), (0, 1), (2, 0), (1, 1)])
def test_the_legacy_baseline_retries_a_dropped_ready_or_lead_request(dropped_ready, dropped_lead):
    """Run 39666476735: the MSI runner's 0.2.1 app ignored the first Go ready, and the baseline
    gave up waiting for Pause dialing instead of pressing it again as a rep would."""
    dialer = LegacyDialer(dropped_ready, dropped_lead)
    with pytest.raises(LegacyDialer.Reached):
        native().legacy_baseline(dialer)
    assert dialer.ready and dialer.lead


def test_the_legacy_baseline_still_fails_when_the_dialer_never_becomes_ready():
    from selenium.common.exceptions import TimeoutException

    with pytest.raises(TimeoutException):
        native().legacy_baseline(LegacyDialer(dropped_ready=5))


class ToastDriver:
    """A WebDriver whose page shows the given error toasts."""

    def __init__(self, toasts):
        self.toasts = toasts

    def find_elements(self, _by, selector):
        assert selector == ".toast"
        return [
            type("Toast", (), {"text": text, "is_displayed": lambda self: True})() for text in self.toasts
        ]


def test_the_native_ui_keeps_each_error_message_the_app_shows():
    """The app explains a failed restore in a toast that vanishes after five seconds; the
    harness must keep it, or a failed update carries no reason (run 39666476735)."""
    ui = native().NativeUI(Path("app.exe"), Path("out"))
    ui.driver = ToastDriver(["The bundled local service did not start."])
    ui.note_toasts()
    ui.note_toasts()
    ui.driver = ToastDriver([])
    ui.note_toasts()
    assert ui.messages == ["The bundled local service did not start."]


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in authority is a POSIX script")
def test_a_failed_restore_is_diagnosed_on_a_copy_of_the_store(tmp_path):
    """After a failed restore the harness starts the installed authority on a copy of the store
    and keeps its log and the store's file listing, never its contents or key."""
    application = tmp_path / "install" / "opendialer-desktop.exe"
    application.parent.mkdir()
    backend = application.parent / "opendialer-backend.exe"
    backend.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "sys.stdin.readline()\n"
        "print('Traceback (most recent call last):')\n"
        "print('PermissionError: [WinError 32] stand-in startup failure')\n"
        "sys.exit(3)\n"
    )
    backend.chmod(0o755)
    store = tmp_path / "simulation"
    (store / ".opendialer-upgrade-v1").mkdir(parents=True)
    (store / "opendialer.db").write_bytes(b"synthetic database")
    (store / "simulation-master").write_text("synthetic-key-never-logged")
    (store / ".opendialer-upgrade-v1" / "candidate.db").write_bytes(b"x" * 7)
    output = tmp_path / "output"
    output.mkdir()
    result = native().diagnose_store(application, store, output, "0.3.0", timeout=20)
    assert result["authority"]["ready"] is False
    assert "WinError 32" in result["authority"]["error"]
    assert {"path": ".opendialer-upgrade-v1/candidate.db", "bytes": 7} in result["files"]
    saved = (output / "post-update-diagnosis.json").read_text()
    assert "synthetic-key-never-logged" not in saved
    assert "synthetic-key-never-logged" not in (output / "post-update-authority.log").read_text()
    assert (store / "opendialer.db").read_bytes() == b"synthetic database", "The original store is untouched"


def test_an_update_that_loses_the_saved_session_is_diagnosed_and_fails():
    source = inspect.getsource(native().upgrade)
    assert "diagnose_store(" in source
    assert "ui.messages" in source
