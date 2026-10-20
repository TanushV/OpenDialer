import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "native_update", Path(__file__).resolve().parents[1] / "scripts" / "native_update.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (None, True),
        (SimpleNamespace(text=None), False),
        (SimpleNamespace(text="Downloading update"), False),
        (SimpleNamespace(text="Update installed. Quit and reopen."), True),
    ],
)
def test_updater_wait_handles_the_native_window_closing(body, expected):
    driver = SimpleNamespace(find_element=lambda *_args: body)
    assert module.update_installed_or_window_closed(driver) is expected


@pytest.mark.parametrize(
    ("version", "active", "backend_hash", "expected"),
    [
        ("0.2.1", 1, "new", False),
        ("0.1.1", 0, "new", False),
        ("0.2.1", 0, "old", False),
        ("0.2.1", 0, None, False),
        ("0.2.1", 0, "new", True),
    ],
)
def test_updated_executable_alone_is_not_completed_installation(version, active, backend_hash, expected):
    status = {"version": version, "active_updaters": active, "backend_sha256": backend_hash}
    assert module.installation_ready(status, "0.2.1", "new") is expected


@pytest.mark.parametrize("updaters", [0, 1, 2])
@pytest.mark.parametrize("installed_hash", [None, "a" * 64, "b" * 64])
def test_installed_exact_backend_after_exit(updaters, installed_hash):
    """C055: the new release keeps both updater-exit and exact-byte fences."""
    status = {"version": "0.3.0", "active_updaters": updaters, "backend_sha256": installed_hash}
    assert module.installation_ready(status, "0.3.0", "b" * 64) is (
        updaters == 0 and installed_hash == "b" * 64
    )


def test_both_scoped_installer_formats_remain_active_until_their_process_exits():
    temporary = r"C:\Users\runner\AppData\Local\Temp"
    msi = temporary + r"\OpenDialer-0.3.0-updater-ABC\OpenDialer-0.3.0-installer.msi"
    exe = temporary + r"\OpenDialer-0.3.0-updater-DEF\OpenDialer-0.3.0-installer.exe"
    rows = [
        {"pid": 1, "exe": r"C:\Windows\System32\msiexec.exe", "cmdline": ["msiexec.exe", "/i", msi]},
        {"pid": 2, "exe": exe, "cmdline": [exe, "/UPDATE"]},
        {
            "pid": 3,
            "exe": r"C:\Windows\System32\msiexec.exe",
            "cmdline": ["msiexec.exe", "/i", r"C:\other.msi"],
        },
        {"pid": 4, "exe": exe.replace("0.3.0", "0.2.1"), "cmdline": []},
        {"pid": 5, "exe": exe.replace(temporary, r"C:\unrelated"), "cmdline": []},
    ]
    assert module.matching_updaters(rows, "0.3.0", temporary) == [1, 2]
    assert module.matching_updaters(rows[2:], "0.3.0", temporary) == []


@pytest.mark.parametrize("kind", ["msi", "nsis"])
def test_update_manifest_requires_the_selected_installer_and_its_signature(kind):
    name = "OpenDialer_0.3.0_x64_en-US.msi" if kind == "msi" else "OpenDialer_0.3.0_x64-setup.exe"
    entry = {
        "url": "https://github.com/TanushV/OpenDialer/releases/download/v0.3.0/" + name,
        "signature": "signed-fixture",
    }
    manifest = {"version": "0.3.0", "platforms": {"windows-x86_64": entry}}
    with pytest.raises(ValueError):
        module.check_installer_manifest(manifest, kind, "0.3.0", name, "signed-fixture")
    manifest["platforms"]["windows-x86_64-" + kind] = entry
    assert module.check_installer_manifest(manifest, kind, "0.3.0", name, "signed-fixture") == entry
    with pytest.raises(ValueError):
        module.check_installer_manifest(manifest, kind, "0.3.0", name, "different-signature")
    entry["url"] = entry["url"].replace("github.com/", "elsewhere.invalid/")
    with pytest.raises(ValueError):
        module.check_installer_manifest(manifest, kind, "0.3.0", name, "signed-fixture")
