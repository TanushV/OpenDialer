"""Wait only for updater exit; the installed-version/data assertions follow it."""

from selenium.webdriver.common.by import By


def update_installed_or_window_closed(driver):
    body = driver.find_element(By.TAG_NAME, "body")
    return body is None or "Update installed." in (body.text or "")
