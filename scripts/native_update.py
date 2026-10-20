"""Wait only for updater exit; the installed-version/data assertions follow it."""

import re
from pathlib import PureWindowsPath

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


def matching_updaters(rows, version, temporary):
    """Read-only identification of both installer formats in the owned update directory."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Invalid updater version")
    temporary = PureWindowsPath(temporary)

    def payload(value, extension):
        path = PureWindowsPath(value.strip('"'))
        return (
            path.parent.parent == temporary
            and path.parent.name.startswith(f"OpenDialer-{version}-updater-")
            and path.name == f"OpenDialer-{version}-installer.{extension}"
        )

    matches = []
    for row in rows:
        executable = PureWindowsPath(row["exe"])
        if payload(str(executable), "exe") or (
            executable.name.lower() == "msiexec.exe" and any(payload(arg, "msi") for arg in row["cmdline"])
        ):
            matches.append(row["pid"])
    return matches


def read_updaters(version, temporary):
    import psutil

    rows = []
    for process in psutil.process_iter(["pid", "name"]):
        name = (process.info["name"] or "").lower()
        if name != "msiexec.exe" and name != f"opendialer-{version}-installer.exe":
            continue
        try:
            rows.append({"pid": process.pid, "exe": process.exe(), "cmdline": process.cmdline()})
        except psutil.NoSuchProcess:
            pass
    # Never emit process arguments; inaccessible relevant processes fail the probe.
    return matching_updaters(rows, version, temporary)


def check_installer_manifest(manifest, kind, version, package_name, signature):
    if kind not in {"msi", "nsis"}:
        raise ValueError("Unsupported installer format")
    entry = manifest.get("platforms", {}).get("windows-x86_64-" + kind)
    expected = f"https://github.com/TanushV/OpenDialer/releases/download/v{version}/{package_name}"
    if (
        manifest.get("version") != version
        or not entry
        or entry.get("url") != expected
        or not signature.strip()
        or entry.get("signature") != signature.strip()
    ):
        raise ValueError("Published updater does not match the selected signed installer")
    return entry
