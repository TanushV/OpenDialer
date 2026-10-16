"""Run the installed Windows executable in its real WebView2; use fictional local data only."""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.options import ArgOptions
from selenium.webdriver.support.ui import Select, WebDriverWait

application = Path(os.environ["OPENDIALER_TEST_APPLICATION"]).resolve()
native_driver = Path(os.environ["OPENDIALER_EDGE_DRIVER"]).resolve()
output = Path("work/windows-native")
output.mkdir(parents=True, exist_ok=True)
launcher = shutil.which("tauri-driver")
if not launcher or not application.is_file() or not native_driver.is_file():
    raise SystemExit("An installed test app and official native drivers are required")
process = subprocess.Popen(  # noqa: S603 - validated official driver executable and fixed arguments
    [launcher, "--native-driver", str(native_driver)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
)
options = ArgOptions()
options.set_capability("browserName", "wry")
options.set_capability("tauri:options", {"application": str(application)})
driver = None
note = "Windows native acceptance: preserve this human note across restart."
update_to = os.environ.get("OPENDIALER_UPDATE_TO_VERSION")
baseline_ids = None


def saved_ids():
    database = Path(os.environ["APPDATA"]) / "com.opendialer.desktop" / "simulation" / "opendialer.db"
    if not database.is_file():
        raise RuntimeError("Native sample database is missing")
    with sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True) as connection:
        return {
            table: sorted(row[0] for row in connection.execute("SELECT id FROM " + table))  # noqa: S608 - fixed table allowlist
            for table in ("leads", "call_attempts", "notes", "suppressions")
        }


def click_button(text):
    def click_current(d):
        element = d.find_element(By.XPATH, f"//button[normalize-space(.)='{text}']")
        if not element.is_displayed() or not element.is_enabled():
            return False
        # Leave the fixed call controls clear in the runner's small desktop viewport.
        # Keep the actual interaction a WebDriver click, with normal hit testing.
        d.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", element)
        element.click()
        return True

    # React can replace a button between lookup and click while refreshing a packet.
    WebDriverWait(driver, 60, ignored_exceptions=(StaleElementReferenceException,)).until(click_current)


def visible(text):
    WebDriverWait(driver, 60).until(lambda d: text in d.find_element(By.TAG_NAME, "body").text)


try:
    for _attempt in range(100):
        try:
            if httpx.get("http://127.0.0.1:4444/status", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    driver = webdriver.Remote("http://127.0.0.1:4444", options=options)
    click_button("Try a simulated workspace")
    visible("SIMULATION")
    click_button("Go ready")
    click_button("Get next lead")
    click_button("Start call")
    visible("RINGING")
    click_button("Simulate human answer")
    visible("HUMAN")
    box = driver.find_element(By.CSS_SELECTOR, 'input[aria-label="Simulation transcript"]')
    box.send_keys("We already use another provider.")
    driver.find_element(By.CSS_SELECTOR, 'button[aria-label="Add prospect turn"]').click()
    visible("We already use another provider.")
    click_button("Opt out")
    visible("How did the conversation go?")
    Select(driver.find_element(By.CSS_SELECTOR, ".wrap-up select")).select_by_value("callback_requested")
    click_button("Save outcome")
    click_button("Review")
    WebDriverWait(driver, 30).until(
        lambda d: d.find_element(By.CSS_SELECTOR, 'button[aria-label^="Review call"]')
    ).click()
    textarea = WebDriverWait(driver, 30).until(lambda d: d.find_element(By.CSS_SELECTOR, "textarea"))
    textarea.clear()
    textarea.send_keys(note)
    click_button("Save notes")
    visible("Notes saved")
    driver.save_screenshot(str(output / "review-before-restart.png"))
    if update_to:
        baseline_ids = saved_ids()
        assert baseline_ids["notes"] and baseline_ids["suppressions"]
        click_button("All conversations")
        click_button("Settings")
        click_button("App updates")
        click_button("Check for updates")
        visible("Version " + update_to)
        driver.save_screenshot(str(output / "signed-update-offer.png"))
        try:
            click_button("Install signed update")
            WebDriverWait(driver, 120).until(
                lambda d: "Update installed." in (d.find_element(By.TAG_NAME, "body").text or "")
            )
        except WebDriverException:
            pass  # Windows updater may exit the old process after verified download.
    try:
        driver.quit()
    except WebDriverException:
        if not update_to:
            raise
    driver = None
    if update_to:
        # The updater exits the old app and finishes its verified installer asynchronously.
        probe_env = {**os.environ, "OPENDIALER_INSTALL_PATH": str(application)}
        powershell = shutil.which("powershell.exe")
        if not powershell:
            raise RuntimeError("The official Windows version probe is unavailable")
        for _attempt in range(120):
            installed = subprocess.check_output(  # noqa: S603 - fixed read-only version probe
                [
                    powershell,
                    "-NoProfile",
                    "-Command",
                    "[Diagnostics.FileVersionInfo]::GetVersionInfo($env:OPENDIALER_INSTALL_PATH).ProductVersion",
                ],
                env=probe_env,
                text=True,
            ).strip()
            if installed.startswith(update_to):
                break
            time.sleep(1)
        else:
            raise RuntimeError("The verified update did not replace the installed executable")
    driver = webdriver.Remote("http://127.0.0.1:4444", options=options)
    # A new WebDriver session may use a fresh webview profile; the native store is independent.
    WebDriverWait(driver, 60).until(
        lambda d: (
            "SIMULATION" in d.find_element(By.TAG_NAME, "body").text
            or d.find_elements(By.XPATH, "//button[normalize-space(.)='Try a simulated workspace']")
        )
    )
    if driver.find_elements(By.XPATH, "//button[normalize-space(.)='Try a simulated workspace']"):
        click_button("Try a simulated workspace")
    visible("SIMULATION")
    if baseline_ids:
        assert saved_ids() == baseline_ids, "Update changed saved identities, notes or opt-outs"
        click_button("Settings")
        click_button("App updates")
        visible("OpenDialer " + update_to)
        click_button("Dialer")
    # Exercise optimization in the actual installed/updated WebView2.
    if json.loads(Path("apps/desktop/src-tauri/tauri.conf.json").read_text())["version"] != "0.1.1":
        click_button("Intelligence")
        visible("Reviewed model pilot")
        Select(
            driver.find_element(By.XPATH, "//label[contains(.,'Conversion goal')]/select")
        ).select_by_value("sale_won")
        click_button("Save optimization settings")
        visible("Optimization settings saved")
        driver.save_screenshot(str(output / "optimization.png"))
        click_button("Dialer")
    click_button("Review")
    WebDriverWait(driver, 30).until(
        lambda d: d.find_element(By.CSS_SELECTOR, 'button[aria-label^="Review call"]')
    ).click()
    assert (
        WebDriverWait(driver, 30)
        .until(lambda d: d.find_element(By.CSS_SELECTOR, "textarea"))
        .get_attribute("value")
        == note
    )
    click_button("All conversations")
    click_button("Settings")
    click_button("Run preflight")
    visible("Simulation checks passed")
    expected_version = (
        update_to or json.loads(Path("apps/desktop/src-tauri/tauri.conf.json").read_text())["version"]
    )
    preflight_text = driver.find_element(By.CSS_SELECTOR, ".preflight").text
    reported_version = re.search(r"·\s*(\d+\.\d+\.\d+)\s*·\s*local", preflight_text)
    assert reported_version, "Native preflight did not report a local backend version"
    assert reported_version.group(1) == expected_version, "Updated backend version differs from the package"
    driver.save_screenshot(str(output / "preflight.png"))
    (output / "result.json").write_text(
        json.dumps(
            {
                "passed": True,
                "installed_executable": application.name,
                "mode": "native Windows WebView2 local simulation",
                "checks": [
                    "installer",
                    "bundled backend",
                    "timezone database",
                    "readiness",
                    "reservation",
                    "call",
                    "answer",
                    "transcript",
                    "hangup",
                    "disposition",
                    "review",
                    "human note",
                    "restart persistence",
                    "preflight",
                ],
                "provider_calls": 0,
                "hardware_audio_certified": False,
                "signed_in_place_update_to": update_to,
                "backend_version": reported_version.group(1),
                "saved_ids_preserved": baseline_ids is not None,
                "restart_scope": "reopen the native sample store after a new WebDriver session",
            },
            indent=2,
        )
    )
    print("PASS: installed Windows native workflow and persisted human notes through restart.")
except Exception:
    if driver:
        try:
            driver.save_screenshot(str(output / "failure.png"))
            (output / "failure-page.txt").write_text(driver.find_element(By.TAG_NAME, "body").text or "")
        except WebDriverException:
            pass  # Preserve the original error when the old updater window has already exited.
    raise
finally:
    if driver:
        driver.quit()
    process.terminate()
    process.wait(timeout=15)
