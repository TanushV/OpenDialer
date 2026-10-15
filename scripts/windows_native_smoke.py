"""Run the installed Windows executable in its real WebView2; use fictional local data only."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
from selenium import webdriver
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


def button(text):
    return WebDriverWait(driver, 60).until(
        lambda d: next(
            (
                e
                for e in d.find_elements(By.TAG_NAME, "button")
                if e.text.strip() == text and e.is_displayed() and e.is_enabled()
            ),
            False,
        )
    )


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
    button("Try a simulated workspace").click()
    visible("SIMULATION")
    button("Go ready").click()
    button("Get next lead").click()
    button("Start call").click()
    visible("RINGING")
    button("Simulate human answer").click()
    visible("HUMAN")
    box = driver.find_element(By.CSS_SELECTOR, 'input[aria-label="Simulation transcript"]')
    box.send_keys("We already use another provider.")
    driver.find_element(By.CSS_SELECTOR, 'button[aria-label="Add prospect turn"]').click()
    visible("We already use another provider.")
    button("Hang up").click()
    visible("How did the conversation go?")
    Select(driver.find_element(By.CSS_SELECTOR, ".wrap-up select")).select_by_value("callback_requested")
    button("Save outcome").click()
    button("Review").click()
    WebDriverWait(driver, 30).until(
        lambda d: d.find_element(By.CSS_SELECTOR, 'button[aria-label^="Review call"]')
    ).click()
    textarea = WebDriverWait(driver, 30).until(lambda d: d.find_element(By.CSS_SELECTOR, "textarea"))
    textarea.clear()
    textarea.send_keys(note)
    button("Save notes").click()
    visible("Notes saved")
    driver.save_screenshot(str(output / "review-before-restart.png"))
    driver.quit()
    driver = None
    driver = webdriver.Remote("http://127.0.0.1:4444", options=options)
    visible("SIMULATION")
    button("Review").click()
    WebDriverWait(driver, 30).until(
        lambda d: d.find_element(By.CSS_SELECTOR, 'button[aria-label^="Review call"]')
    ).click()
    assert (
        WebDriverWait(driver, 30)
        .until(lambda d: d.find_element(By.CSS_SELECTOR, "textarea"))
        .get_attribute("value")
        == note
    )
    button("All conversations").click()
    button("Settings").click()
    button("Run preflight").click()
    visible("Simulation checks passed")
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
            },
            indent=2,
        )
    )
    print("PASS: installed Windows native workflow and persisted human notes through restart.")
except Exception:
    if driver:
        driver.save_screenshot(str(output / "failure.png"))
        (output / "failure-page.txt").write_text(driver.find_element(By.TAG_NAME, "body").text)
    raise
finally:
    if driver:
        driver.quit()
    process.terminate()
    process.wait(timeout=15)
