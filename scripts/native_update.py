"""Wait only for updater exit; the installed-version/data assertions follow it."""

from selenium.webdriver.common.by import By


def update_installed_or_window_closed(driver):
    body = driver.find_element(By.TAG_NAME, "body")
    return body is None or "Update installed." in (body.text or "")


def installation_ready(status, expected_version, expected_backend_hash):
    return (
        status.get("version") in {expected_version, expected_version + ".0"}
        and status.get("active_updaters") == 0
        and bool(expected_backend_hash)
        and status.get("backend_sha256") == expected_backend_hash
    )
