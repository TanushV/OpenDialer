"""Use the installed Windows WebView2 in both modes, never reinstall/reset it."""

import hashlib
import json
import ntpath
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.options import ArgOptions
from selenium.webdriver.support.ui import Select, WebDriverWait

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.disposable_database import validate_database_url
from scripts.frozen_acceptance import (
    await_ready,
    isolated_environment,
    request,
    start_authority,
    start_owned,
    stop_owned,
)
from scripts.native_update import installation_ready, read_updaters, update_installed_or_window_closed

ROOT = Path(__file__).resolve().parents[1]
RAW_EXPIRY_WAIT_SECONDS = 305
NATIVE_MODES = ("local", "connected")
IDENTITY_TABLES = (
    "leads",
    "call_attempts",
    "notes",
    "suppressions",
    "followup_tasks",
    "sessions",
    "command_outbox",
)
NOTE = "Windows native acceptance: preserve this human note across restart."


def desktop_environment(source):
    # Windows reports environment keys upper-cased (SYSTEMROOT); match case-insensitively.
    allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "COMSPEC"}
    kept = {k: v for k, v in source.items() if k.upper() in allowed}
    system_root = next(v for k, v in kept.items() if k.upper() == "SYSTEMROOT")
    return {
        **kept,
        "PATH": ntpath.join(system_root, "System32"),
        "PYTHONNOUSERSITE": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
    }


def saved_ids(database):
    if not database.is_file():
        raise FileNotFoundError("Native sample database is missing")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {
            table: sorted(row[0] for row in connection.execute("SELECT id FROM " + table))  # noqa: S608 - fixed allowlist
            for table in IDENTITY_TABLES
            if table in present
        }


def require_continuity(before, after):
    for table, ids in before.items():
        assert set(ids) <= set(after.get(table, [])), f"Update changed a saved {table} identity"


def check_preflight(text, expected_version, mode):
    match = re.search(r"·\s*(\d+\.\d+\.\d+)\s*·\s*(local|connected)", text)
    assert match and match.group(1) == expected_version and match.group(2) == mode, (
        "Wrong native authority/version"
    )
    assert "Simulation checks passed" in text and "integrity" in text.lower(), "Native preflight failed"
    return match.group(1)


class NativeUI:
    def __init__(self, application, output):
        self.application, self.output = application, output
        self.driver = None
        self.startup_ms = []
        self.messages = []  # Every error toast seen; they vanish after five seconds.

    def open(self, *, restored=False):
        options = ArgOptions()
        options.set_capability("browserName", "wry")
        options.set_capability("tauri:options", {"application": str(self.application)})
        began = time.monotonic()
        self.driver = webdriver.Remote("http://127.0.0.1:4444", options=options)
        self.driver.set_window_size(1024, 720)
        self.wait(
            lambda: (
                self.note_toasts() is None
                and "Opening your workspace…" not in self.body()
                and ("SIMULATION" in self.body() or "Try a simulated workspace" in self.body())
            )
        )
        if restored:
            # Do not conceal a lost native session with another login or demo setup.
            self.visible("SIMULATION")
        self.startup_ms.append((time.monotonic() - began) * 1000)

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            finally:
                self.driver = None

    def wait(self, action, timeout=60):
        # A transient toast (dismissed after 5 s) may cover a control: retry, as a person would.
        return WebDriverWait(
            self.driver,
            timeout,
            ignored_exceptions=(StaleElementReferenceException, ElementClickInterceptedException),
        ).until(lambda _driver: action())

    def body(self):
        try:
            return self.driver.find_element(By.TAG_NAME, "body").text or ""
        except WebDriverException as exc:
            # While the app loads, the document can be replaced between finding <body> and
            # reading it; that is a stale read to retry, not a failure.
            if "does not belong to the document" in str(exc):
                return ""
            raise

    def note_toasts(self):
        try:
            shown = [e.text for e in self.driver.find_elements(By.CSS_SELECTOR, ".toast") if e.is_displayed()]
        except WebDriverException:
            return
        self.messages.extend(text for text in dict.fromkeys(shown) if text and text not in self.messages)

    def shows(self, text, timeout=60):
        """Whether `text` appears, keeping any error toast shown meanwhile."""
        try:
            self.wait(lambda: self.note_toasts() is None and text in self.body(), timeout=timeout)
        except TimeoutException:
            return False
        return True

    def visible(self, text):
        return self.wait(lambda: text in self.body())

    def element(self, selector):
        return self.wait(
            lambda: next(
                (e for e in self.driver.find_elements(By.CSS_SELECTOR, selector) if e.is_displayed()), None
            )
        )

    def button(self, text):
        return self.wait(
            lambda: next(
                (
                    e
                    # The accessible name: a navigation item's label stays exact beside its count badge.
                    for e in self.driver.find_elements(
                        By.XPATH,
                        # The accessible name, the exact text, or a choice card's <strong> title.
                        f"//button[@aria-label='{text}' or normalize-space(.)='{text}'"
                        f" or normalize-space(.//strong)='{text}']",
                    )
                    if e.is_displayed() and e.is_enabled()
                ),
                None,
            )
        )

    def click(self, text):
        def perform():
            element = self.button(text)
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
            element.click()
            return True

        self.wait(perform)

    def open_details(self, summary):
        """Open a collapsed <details> section once; clicking an open one would close it."""

        def opened():
            element = next(
                (
                    e
                    for e in self.driver.find_elements(
                        By.XPATH, f"//summary[contains(normalize-space(.), '{summary}')]"
                    )
                    if e.is_displayed()
                ),
                None,
            )
            if element is None:
                return False
            details = element.find_element(By.XPATH, "..")
            if details.get_attribute("open") is None:
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                element.click()
            return details.get_attribute("open") is not None

        self.wait(opened)

    def sign_out(self):
        """Sign out, failing with the reason when the app keeps the control disabled."""
        selector = 'button[title="Sign out"]'
        button = next(
            (e for e in self.driver.find_elements(By.CSS_SELECTOR, selector) if e.is_displayed()), None
        )
        if button is not None and button.get_attribute("disabled") is not None:
            raise RuntimeError(
                "Sign out is disabled: the workspace reports an unresolved obligation (Settings shows the reason)"
            )
        self.press(selector)

    def press(self, selector):
        """Click a control by CSS selector as a person would: bring it into view, retry if covered."""

        def perform():
            element = next(
                (e for e in self.driver.find_elements(By.CSS_SELECTOR, selector) if e.is_displayed()), None
            )
            if element is None:
                return False
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
            element.click()
            return True

        self.wait(perform)

    def field(self, label, *, prefix=False):
        # A prefix match covers labels that carry a live caption, such as the callback time zone.
        test = (
            f"starts-with(normalize-space(text()), '{label}')"
            if prefix
            else f"normalize-space(text())='{label}'"
        )
        found = self.wait(
            lambda: next(
                (
                    e
                    for e in self.driver.find_elements(
                        By.XPATH,
                        f"//label[{test}]/*[self::input or self::select or self::textarea]",
                    )
                    if e.is_displayed()
                ),
                None,
            )
        )
        # A select's option click or typing must not land on the call dock that may cover it.
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", found)
        return found

    def fill(self, element, value):
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(value)

    def date(self, label, value, *, prefix=False):
        element = self.field(label, prefix=prefix)
        # HTML datetime-local's segmented keyboard format varies by Windows locale.
        # Use its DOM input event, never an HTTP/domain-state shortcut.
        self.driver.execute_script(
            "Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(arguments[0],arguments[1]);"
            "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
            element,
            value,
        )
        assert element.get_attribute("value") == value

    def screenshot(self, name):
        self.driver.save_screenshot(str(self.output / (name + ".png")))

    def in_view(self, selector):
        element = self.element(selector)
        assert self.driver.execute_script(
            "const r=arguments[0].getBoundingClientRect();return r.top>=0&&r.left>=0&&r.bottom<=innerHeight&&r.right<=innerWidth;",
            element,
        ), f"Required control is outside the native viewport: {selector}"

    def pause(self):
        self.click("Dialer")
        for attempt in range(3):
            # 0.2.1 can drop the presence request; press Pause again, as a rep would.
            if "Pause dialing" in self.body():
                self.click("Pause dialing")
            try:
                self.wait(lambda: "Go ready" in self.body(), timeout=20)
                break
            except TimeoutException:
                if attempt == 2:
                    raise
        assert "Hang up" not in self.body(), "Native journey left an active call"

    def review(self, lead_name, note=None, *, legacy=False):
        self.click("Review")
        self.element('button[aria-label^="Review call"]')
        matches = [
            element
            for element in self.driver.find_elements(By.CSS_SELECTOR, 'button[aria-label^="Review call"]')
            if element.get_attribute("aria-label") == "Review call " + lead_name
        ]
        assert len(matches) == 1, "Selected journey review is missing or ambiguous"
        self.press(f'button[aria-label="Review call {lead_name}"]')
        box = self.element("textarea" if legacy else 'textarea[aria-label="Your notes"]')
        if note is not None:
            self.fill(box, note)
            self.click("Save notes")
            self.visible("Notes saved")
        return box.get_attribute("value")

    def save_followup(self, due):
        call_id = self.element(".followup-task").get_attribute("data-call-id")
        assert call_id
        self.date("Callback time", due, prefix=True)
        self.click("Save follow-up")
        self.visible("Follow-up saved")
        current = self.element(f'.followup-task[data-call-id="{call_id}"]')
        assert current.get_attribute("data-call-id") == call_id
        assert self.field("Callback time", prefix=True).get_attribute("value") == due
        return call_id

    def correct_contact_role(self, note):
        self.open_details("Correct confirmed outcome")
        assert self.element('.wrap-up button[aria-pressed="true"]').text == "Talked: callback requested"
        assert self.field("Wrap-up note").get_attribute("value") == note
        Select(self.field("Who did you speak with?")).select_by_value("gatekeeper")
        self.click("Save outcome")
        self.visible("Outcome saved")
        self.wait(lambda: self.field("Who did you speak with?").get_attribute("value") == "gatekeeper")
        assert self.element('textarea[aria-label="Your notes"]').get_attribute("value") == note

    def check_next_call_context(self, receipt):
        self.click("Leads")
        matches = [
            item
            for item in self.driver.find_elements(By.CSS_SELECTOR, 'button[aria-label^="Open lead "]')
            if item.get_attribute("aria-label") == "Open lead " + receipt["lead_name"]
        ]
        assert len(matches) == 1, "Next-call lead identity is missing or ambiguous"
        self.press(f'button[aria-label="Open lead {receipt["lead_name"]}"]')
        self.element(".modal")
        self.open_details("Prior conversation & next step")
        brief = self.element('.modal [data-testid="prospect-brief"]').text
        assert receipt["note"] in brief and "Last outcome: Callback requested" in brief
        assert "Next question:" in brief
        self.screenshot("next-call-context")
        self.press(".modal .close-modal")

    def preflight(self, version, mode):
        # Readiness includes the rep's audio check, which a restart resets: go ready first.
        self.click("Dialer")
        readied = "Go ready" in self.body()
        if readied:
            self.click("Go ready")
        self.visible("Pause dialing")
        self.click("Settings")
        self.click("Run preflight")
        self.visible("Database integrity verified")
        result = check_preflight(self.element(".preflight").text, version, mode)
        self.screenshot(mode + "-preflight")
        if readied:
            self.pause()  # Leave the rep as found: paused, with no active claim.
            self.click("Settings")  # Callers continue in Settings.
        return result


def legacy_baseline(ui):
    """Create the fixed 0.2.1 fixture using only that released UI."""
    ui.click("Try a simulated workspace")
    ui.visible("SIMULATION")
    for attempt in range(3):
        # 0.2.1 marks the rep ready asynchronously and can drop a ready or next-lead request;
        # press it again, as a rep would.
        try:
            if "Go ready" in ui.body():
                ui.click("Go ready")
            ui.wait(lambda: "Pause dialing" in ui.body(), timeout=30)
            if "Start call" not in ui.body():
                ui.click("Get next lead")
            ui.wait(lambda: "Start call" in ui.body(), timeout=30)
            break
        except TimeoutException:
            if attempt == 2:
                raise
    lead_name = ui.element('[data-testid="lead-name"]').text
    ui.click("Start call")
    ui.visible("RINGING")
    ui.click("Simulate human answer")
    ui.visible("HUMAN")
    ui.click("Opt out")
    ui.visible("How did the conversation go?")
    Select(ui.element(".wrap-up select")).select_by_value("opt_out")
    ui.click("Save outcome")
    ui.pause()
    assert ui.review(lead_name, NOTE, legacy=True) == NOTE
    ui.screenshot("baseline-note")
    ui.click("All conversations")
    return lead_name


def driver_creationflags(os_name):
    """A new process group starts with CTRL_C disabled, inherited by the driver's children: the
    exiting 0.2.1 MSI updater's console CTRL_C stopped tauri-driver in run 39694452712."""
    return 0x00000200 if os_name == "nt" else 0  # CREATE_NEW_PROCESS_GROUP


@contextmanager
def console_interrupts_ignored(kernel32=None):
    """The exiting 0.2.1 updater can deliver CTRL_C to every process on the runner's console
    (run 39668123126 interrupted the install probe). Ignore it only while the update installs;
    probes started meanwhile inherit this, and an unanswered install still fails on its own bound.
    """
    if kernel32 is None and os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
    if kernel32 is None:
        yield
        return
    kernel32.SetConsoleCtrlHandler(None, True)
    try:
        yield
    finally:
        kernel32.SetConsoleCtrlHandler(None, False)


def wait_installed(application, version, output, *, not_before):
    powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if not powershell:
        raise RuntimeError("Official Windows installation probe is unavailable")
    env = {**os.environ, "OPENDIALER_INSTALL_PATH": str(application)}
    env.pop("PSModulePath", None)
    expected_hash = os.environ["OPENDIALER_EXPECTED_BACKEND_SHA256"]
    temporary = Path(os.environ["TEMP"])
    extension = "msi" if os.environ["OPENDIALER_INSTALLER_KIND"] == "msi" else "exe"
    expected_installer = os.environ["OPENDIALER_EXPECTED_INSTALLER_SHA256"]
    for _ in range(180):
        active_before = read_updaters(version, str(temporary))
        value = subprocess.check_output(  # noqa: S603 - read-only exact-installation probe
            [
                powershell,
                "-NoProfile",
                "-Command",
                "$ErrorActionPreference='Stop';$version=$null;$hash=$null;"
                # The installer removes and rewrites these files: absent or locked is "not yet".
                "if(Test-Path $env:OPENDIALER_INSTALL_PATH) {"
                "$version=[Diagnostics.FileVersionInfo]::GetVersionInfo($env:OPENDIALER_INSTALL_PATH).ProductVersion};"
                "$backend=Join-Path (Split-Path $env:OPENDIALER_INSTALL_PATH) 'opendialer-backend.exe';"
                "if(Test-Path $backend) {"
                "try {$hash=(Get-FileHash $backend -Algorithm SHA256).Hash.ToLower()} catch {$hash=$null}};"
                "@{version=$version;backend_sha256=$hash}|ConvertTo-Json -Compress",
            ],
            env=env,
            text=True,
        )
        active = sorted(set(active_before + read_updaters(version, str(temporary))))
        payloads = [
            path
            for path in temporary.glob(
                f"OpenDialer-{version}-updater-*/OpenDialer-{version}-installer.{extension}"
            )
            if path.stat().st_mtime >= not_before - 1
        ]
        payload_ok = (
            len(payloads) == 1 and hashlib.sha256(payloads[0].read_bytes()).hexdigest() == expected_installer
        )
        status = {
            **json.loads(value),
            "active_updaters": len(active),
            "expected_backend_sha256": expected_hash,
            "installer_format": extension,
            "actual_update_payload_verified": payload_ok,
        }
        (output / "installation-probe.json").write_text(json.dumps(status, indent=2))
        if payload_ok and installation_ready(status, version, expected_hash):
            (output / "completed-installation.json").write_text(json.dumps(status, indent=2))
            return status
        time.sleep(1)
    raise RuntimeError(
        "Updater has not exited with the exact accepted backend; preserve installation for diagnosis"
    )


def diagnose_store(application, directory, output, version, *, timeout=90):
    """After a failed open, list the store and start the installed authority on it, as the
    app's next launch would, keeping that log.

    It runs on the store itself because the upgrade stage is bound to the store's path (a copy
    elsewhere is refused before the real failure, as run 39669158235 showed). The log holds only
    the authority's startup output; the key travels on stdin and file contents are never read.
    """
    files = sorted(
        (
            {"path": path.relative_to(directory).as_posix(), "bytes": path.stat().st_size}
            for path in directory.rglob("*")
            if path.is_file()
        ),
        key=lambda row: row["path"],
    )
    result = {"files": files}
    log = output / "post-update-authority.log"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    secrets_line = (
        json.dumps(
            {
                "local_token": secrets.token_urlsafe(32),
                "secret_key": (directory / "simulation-master").read_text(),
            }
        )
        + "\n"
    ).encode()
    command = [
        str(application.parent / "opendialer-backend.exe"),
        "serve",
        "--secrets-stdin",
        "--simulation",
        "--mode",
        "local",
        "--port",
        str(port),
        "--data-dir",
        str(directory),
    ]
    with log.open("wb") as handle:
        process = start_authority(  # noqa: S603 - the installed backend, as the app launches it
            command,
            secrets_line,
            env=isolated_environment(os.environ),
            cwd=output,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
                health = await_ready(client, version, timeout=timeout, process=process, log=log)
            result["authority"] = {"ready": True, "health": health}
        except (RuntimeError, TimeoutError, ValueError) as exc:
            result["authority"] = {"ready": False, "error": str(exc)[-4000:]}
        finally:
            stop_owned(process)
    (output / "post-update-diagnosis.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def upgrade(ui, version, database):
    lead_name = legacy_baseline(ui)
    before = saved_ids(database)
    assert before["notes"] and before["suppressions"] and before["sessions"]
    ui.click("Settings")
    ui.click("App updates")
    ui.click("Check for updates")
    ui.visible("Version " + version)
    ui.screenshot("signed-update-offer")
    # Edge WebDriver may give each session a fresh WebView2 profile, which an installed app never
    # has; this probe tells that apart from the app losing its own saved session.
    probe = secrets.token_hex(8)
    ui.driver.execute_script("localStorage.setItem('opendialer.harness-profile', arguments[0])", probe)
    not_before = time.time()
    with console_interrupts_ignored():
        try:
            ui.click("Install signed update")
            WebDriverWait(ui.driver, 120).until(update_installed_or_window_closed)
        except WebDriverException:
            pass  # The authoritative exit-and-byte probe below must still succeed.
        try:
            ui.close()
        except WebDriverException:
            pass
        installed = wait_installed(ui.application, version, ui.output, not_before=not_before)
    ui.open()
    kept = ui.driver.execute_script("return localStorage.getItem('opendialer.harness-profile')") == probe
    if not kept:
        # The driver reset the WebView profile (runs 39665326098, 39666476735), so no saved choice
        # can survive; re-entering the simulated workspace reopens the same migrated store.
        ui.click("Try a simulated workspace")
    # With the profile kept, the saved 0.2.1 session itself must restore.
    if not ui.shows("SIMULATION", timeout=120):
        ui.screenshot("post-update-open")
        (ui.output / "post-update-open.txt").write_text(ui.body())
        ui.close()  # Let the app's authority exit before copying its store.
        diagnosis = diagnose_store(ui.application, database.parent, ui.output, version)
        raise RuntimeError(
            "The updated app did not open the 0.2.1 workspace; "
            f"app messages: {ui.messages or 'none'}; authority: {diagnosis['authority']}"
        )
    require_continuity(before, saved_ids(database))
    assert ui.review(lead_name) == NOTE
    ui.screenshot("updated-baseline-note")
    ui.click("All conversations")
    ui.preflight(version, "local")
    return {
        "installed": installed,
        "webview_profile_kept": kept,
        "session_restored": kept,
        "identity_counts": {k: len(v) for k, v in before.items()},
        "human_note_sha256": hashlib.sha256(NOTE.encode()).hexdigest(),
        "lead_name": lead_name,
    }


def current_journey(ui, mode):
    """Actual clicks, retained notes/tasks and five minutes of real elapsed expiry."""
    ui.click("Leads")
    name = "Native " + mode + " continuity"
    phone = "+141555509" + ("71" if mode == "local" else "72")
    ui.fill(
        ui.field("CSV lead data"),
        f"name,phone,company,timezone,region\n{name},{phone},Example,America/Los_Angeles,415",
    )
    ui.click("Import leads")
    ui.visible("1 imported")
    ui.click("Prepare displayed lead packets")
    ui.click("Queue preparation")  # Preparation is previewed with its count and budget first.
    ui.visible("Preparation queued")  # The authority's own lanes prepare the packet.
    ui.click("Dialer")
    if "Go ready" in ui.body():
        ui.click("Go ready")
    ui.click("Get next lead")
    ui.visible("Start call")
    lead_name = ui.element('[data-testid="lead-name"]').text
    ui.in_view('[data-testid="live-coaching-strip"]')
    ui.in_view(".script-opening")
    assert ui.element('[data-testid="prepared-script"]').text
    ui.click("Start call")
    ui.visible("RINGING")
    ui.click("Simulate human answer")
    ui.visible("HUMAN")
    ui.in_view('[data-testid="live-coaching-strip"]')
    ui.in_view('button[aria-label="Hang up"]') if ui.driver.find_elements(
        By.CSS_SELECTOR, 'button[aria-label="Hang up"]'
    ) else ui.in_view(".call-dock")
    raw = "Synthetic native speech sentinel; our next review is Tuesday."
    ui.fill(ui.element('input[aria-label="Simulation transcript"]'), raw)
    ui.press('button[aria-label="Add prospect turn"]')
    ui.open_details("Live transcript")  # The ephemeral transcript is an optional expansion during the call.
    ui.visible(raw)
    ui.click("Mute tips")
    ui.visible("Automatic tips muted")
    ui.click("Private help")
    ui.fill(ui.element('input[aria-label="Private helper question"]'), "What useful question can I ask next?")
    ui.click("Ask privately")
    ui.wait(lambda: "Thinking with your call context" not in ui.body())
    ui.screenshot(mode + "-helper")
    ui.click("Less")
    began = time.monotonic()
    while time.monotonic() - began < RAW_EXPIRY_WAIT_SECONDS:
        time.sleep(5)
        # Real input keeps this synthetic rep present; no API heartbeat shortcuts.
        ui.press('input[aria-label="Simulation transcript"]')
    ui.wait(lambda: raw not in ui.element(".transcript-scroll").text)
    ui.click("Hang up")
    ui.visible("How did the conversation go?")
    ui.click("Talked: callback requested")  # The primary outcome chip; Disposition is under More outcomes.
    Select(ui.field("Who did you speak with?")).select_by_value("right_party")
    ui.date("Confirmed callback time", "2026-10-21T14:00", prefix=True)
    note = NOTE + " " + mode
    ui.fill(ui.field("Wrap-up note"), note)
    ui.click("Save outcome")
    ui.visible("Outcome saved")
    ui.pause()
    assert ui.review(lead_name) == note
    ui.correct_contact_role(note)
    ui.screenshot(mode + "-rundown")
    ui.click("All conversations")
    ui.click("Intelligence")
    ui.element(".followup-task")
    assert len(ui.driver.find_elements(By.CSS_SELECTOR, ".followup-task")) == 1, "Duplicate callback task"
    due = ui.field("Callback time", prefix=True).get_attribute("value")
    assert due
    task_call_id = ui.save_followup("2026-10-22T14:00")
    ui.screenshot(mode + "-followup")
    return {
        "note": note,
        "lead_name": lead_name,
        "task_call_id": task_call_id,
        "due": "2026-10-22T14:00",
        "raw_expiry_elapsed_seconds": time.monotonic() - began,
    }


def check_journey_restart(ui, receipt, mode, version):
    ui.close()
    ui.open(restored=True)
    ui.click("Intelligence")
    tasks = ui.driver.find_elements(By.CSS_SELECTOR, ".followup-task")
    if not tasks:
        ui.element(".followup-task")
        tasks = ui.driver.find_elements(By.CSS_SELECTOR, ".followup-task")
    assert len(tasks) == 1 and tasks[0].get_attribute("data-call-id") == receipt["task_call_id"]
    assert ui.field("Callback time", prefix=True).get_attribute("value") == receipt["due"]
    assert ui.review(receipt["lead_name"]) == receipt["note"]
    ui.open_details("Correct confirmed outcome")
    assert ui.field("Who did you speak with?").get_attribute("value") == "gatekeeper"
    ui.click("All conversations")
    ui.check_next_call_context(receipt)
    ui.click("Numbers")
    ui.click("Plan regional capacity")
    ui.element('[data-testid="capacity-proposal"]')
    ui.screenshot(mode + "-capacity")
    ui.preflight(version, mode)
    ui.click("Policy & DNC")
    ui.visible("National")
    ui.screenshot(mode + "-dnc-costs")
    ui.click("Operations")
    ui.visible("Work needing recovery")
    ui.screenshot(mode + "-recovery")
    ui.pause()
    return {
        "mode": mode,
        "lead_name": receipt["lead_name"],
        "task_call_id": receipt["task_call_id"],
        "human_note_sha256": hashlib.sha256(receipt["note"].encode()).hexdigest(),
        "callback_time_preserved": True,
        "raw_expiry_elapsed_seconds": receipt["raw_expiry_elapsed_seconds"],
        "session_restored": True,
        "source_correction_verified": True,
    }


def connected_server_command(port, directory):
    """The server runtime, as the server image runs it. The installed desktop sidecar has no
    PostgreSQL driver and refuses connected mode; what this journey checks is the installed
    desktop client connecting to a server."""
    return [
        sys.executable,
        "-m",
        "app.cli",
        "serve",
        "--mode",
        "connected",
        "--simulation",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--data-dir",
        str(directory),
    ]


@contextmanager
def connected_authority(application, version, output):
    url = validate_database_url(os.environ["OPENDIALER_E2E_DATABASE_URL"], purpose="browser")
    with tempfile.TemporaryDirectory(prefix="opendialer-native-connected-") as directory:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        environment = isolated_environment(os.environ)
        bootstrap = secrets.token_urlsafe(32)
        environment.update(
            OPENDIALER_DATABASE_URL=url,
            OPENDIALER_BOOTSTRAP_TOKEN=bootstrap,
            PYTHONPATH=str(ROOT / "services/backend"),
        )
        with (output / "connected-authority.log").open("w") as log:
            process = start_owned(  # noqa: S603 - the checkout's server runtime, owned temporary data
                connected_server_command(port, directory),
                env=environment,
                cwd=directory,
                stdout=log,
                stderr=log,
            )
            try:
                address = f"http://127.0.0.1:{port}"
                with httpx.Client(base_url=address, timeout=30) as client:
                    await_ready(client, version)
                    token = request(
                        client,
                        {"X-Bootstrap-Token": bootstrap},
                        "POST",
                        "/v1/setup",
                        json={
                            "seller_name": "Native Team",
                            "campaign_name": "Native connected",
                            "email": "native@example.test",
                            "password": "Disposable-native-test-password",
                        },
                    )
                    headers = {"Authorization": "Bearer " + token["access_token"]}
                    request(client, headers, "POST", "/v1/simulation/seed")
                    yield address
                    state = request(client, headers, "GET", "/v1/workspace")
                    assert not any(
                        c["state"] not in {"FINALIZED", "ENDED", "FAILED"} for c in state["calls"]
                    ), "Connected native journey left a call"
            finally:
                stop_owned(process)


def main():
    application = Path(os.environ["OPENDIALER_TEST_APPLICATION"]).resolve(strict=True)
    native_driver = Path(os.environ["OPENDIALER_EDGE_DRIVER"]).resolve(strict=True)
    output = Path("work/windows-native")
    output.mkdir(parents=True, exist_ok=True)
    version = json.loads(Path("apps/desktop/src-tauri/tauri.conf.json").read_text())["version"]
    launcher = shutil.which("tauri-driver")
    if not launcher:
        raise RuntimeError("The official tauri-driver is required")
    with (output / "driver.log").open("w") as log:
        process = subprocess.Popen(  # noqa: S603 - official driver, isolated child environment
            [launcher, "--native-driver", str(native_driver)],
            env=desktop_environment(os.environ),
            stdout=log,
            stderr=log,
            creationflags=driver_creationflags(os.name),
        )
        ui = NativeUI(application, output)
        try:
            for _ in range(100):
                try:
                    if httpx.get("http://127.0.0.1:4444/status", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            ui.open()
            database = Path(os.environ["APPDATA"]) / "com.opendialer.desktop" / "simulation" / "opendialer.db"
            update_to = os.environ.get("OPENDIALER_UPDATE_TO_VERSION")
            update = upgrade(ui, update_to, database) if update_to else None
            if not update_to:
                ui.click("Try a simulated workspace")
                ui.visible("SIMULATION")
            local = check_journey_restart(ui, current_journey(ui, "local"), "local", version)
            import psutil

            from scripts.measure_completion import measure_native_resources

            native_root = next(
                child
                for child in psutil.Process(process.pid).children(recursive=True)
                if Path(child.exe()).resolve() == application
            )
            resources = measure_native_resources(
                native_root.pid, application, application.parent / "opendialer-backend.exe"
            )
            (output / "native-resources.json").write_text(json.dumps(resources, indent=2) + "\n")
            with connected_authority(application, version, output) as address:
                ui.sign_out()
                ui.click("Connect to a server")
                ui.fill(ui.field("Server URL"), address)
                ui.fill(ui.field("Email"), "native@example.test")
                ui.fill(ui.field("Password"), "Disposable-native-test-password")
                ui.click("Sign in")
                ui.visible("SIMULATION")
                shared = check_journey_restart(ui, current_journey(ui, "connected"), "connected", version)
                ui.sign_out()
                ui.click("Try a simulated workspace")
                ui.visible("SIMULATION")
                assert (
                    hashlib.sha256(ui.review(local["lead_name"]).encode()).hexdigest()
                    == local["human_note_sha256"]
                )
                ui.click("All conversations")
                if update:
                    assert ui.review(update["lead_name"]) == NOTE
                    ui.click("All conversations")
                ui.pause()
                ui.preflight(version, "local")
            result = {
                "passed": resources["gates"]["status"] == "passed",
                "installed_executable": application.name,
                "version": version,
                "update": update,
                "journeys": [local, shared],
                "startup_ms": ui.startup_ms,
                "resource_gates": resources["gates"],
                "source_runtime_on_path": False,
                "provider_calls": 0,
                "hardware_audio_certified": False,
            }
            (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            assert resources["gates"]["status"] == "passed", "Native resource acceptance remains unpassed"
            print("PASS: installed native local/connected journeys and retained identity after restart.")
        except Exception:
            if ui.driver:
                try:
                    ui.screenshot("failure")
                    (output / "failure-page.txt").write_text(ui.body())
                except WebDriverException:
                    pass
            raise
        finally:
            try:
                ui.close()
            finally:
                stop_owned(process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
