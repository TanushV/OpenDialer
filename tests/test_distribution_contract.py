import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_native_resource_probe_is_test_only_and_excluded_from_the_sidecar():
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert not any(value.startswith("psutil") for value in config["project"]["dependencies"])
    assert "psutil==7.2.2" in config["dependency-groups"]["dev"]
    build = (ROOT / "scripts/build_sidecar.py").read_text()
    assert '"--exclude-module",\n        "psutil"' in build


def test_native_minimum_window_supports_the_required_rep_journey():
    config = json.loads((ROOT / "apps/desktop/src-tauri/tauri.conf.json").read_text())
    assert config["app"]["windows"][0]["minWidth"] == 1024
    assert config["app"]["windows"][0]["minHeight"] == 720


def test_version_identity_artifact_contract():
    """C054: all release inputs agree while the installed updater identity stays stable."""
    import hashlib
    import tomllib

    from app.config import Settings

    tauri = json.loads((ROOT / "apps/desktop/src-tauri/tauri.conf.json").read_text())
    package = json.loads((ROOT / "apps/desktop/package.json").read_text())
    lock = json.loads((ROOT / "apps/desktop/package-lock.json").read_text())
    cargo = tomllib.loads((ROOT / "apps/desktop/src-tauri/Cargo.toml").read_text())
    python = tomllib.loads((ROOT / "pyproject.toml").read_text())
    python_lock = tomllib.loads((ROOT / "uv.lock").read_text())
    cargo_lock = tomllib.loads((ROOT / "apps/desktop/src-tauri/Cargo.lock").read_text())
    versions = [
        tauri["version"],
        package["version"],
        lock["version"],
        lock["packages"][""]["version"],
        cargo["package"]["version"],
        python["project"]["version"],
        Settings().version,
        next(p["version"] for p in python_lock["package"] if p["name"] == "opendialer"),
        next(p["version"] for p in cargo_lock["package"] if p["name"] == "opendialer-desktop"),
    ]
    assert versions == ["0.3.0"] * len(versions)
    assert tauri["identifier"] == "com.opendialer.desktop"
    updater = tauri["plugins"]["updater"]
    assert (
        hashlib.sha256(updater["pubkey"].encode()).hexdigest()
        == "562e88f1045a31e34b8dc05381b2c3b1645ad06c023772588a24946186af5aa4"
    )
    assert updater["endpoints"] == [
        "https://github.com/TanushV/OpenDialer/releases/latest/download/latest.json"
    ]
    source = (ROOT / "scripts/frozen_acceptance.py").read_text()
    assert "--expected-version" in source
    assert '== "0.2.1"' not in source


def test_desktop_is_tauri_with_scoped_capabilities_and_microphone_metadata():
    config = json.loads((ROOT / "apps/desktop/src-tauri/tauri.conf.json").read_text())
    assert config["identifier"] == "com.opendialer.desktop"
    assert config["app"]["security"]["csp"]
    capabilities = (ROOT / "apps/desktop/src-tauri/capabilities/default.json").read_text()
    assert "shell:allow-execute" not in capabilities
    assert "fs:allow-read" not in capabilities
    assert "NSMicrophoneUsageDescription" in (ROOT / "apps/desktop/src-tauri/Info.plist").read_text()


def test_updater_uses_signature_verification_and_stable_identity():
    config = json.loads((ROOT / "apps/desktop/src-tauri/tauri.conf.json").read_text())
    updater = config["plugins"]["updater"]
    assert updater["pubkey"] and "PLACEHOLDER" not in updater["pubkey"]
    assert all(url.startswith("https://") for url in updater["endpoints"])
    assert config["bundle"]["externalBin"]
    assert config["bundle"]["createUpdaterArtifacts"]


def test_deployment_database_is_private_and_workers_have_separate_lanes():
    compose = (ROOT / "deploy/compose.yml").read_text()
    assert "5432:5432" not in compose
    for lane in ["call_control", "live_coach", "research", "postcall"]:
        assert lane in compose
    assert (ROOT / "deploy/backup/restore.py").exists()
    assert (ROOT / "docs/PLAN.md").exists()


def test_deployment_orders_migration_before_all_authority_workers():
    compose = (ROOT / "deploy/compose.yml").read_text()
    assert "service_completed_successfully" in compose
    assert "command: [migrate" in compose


def test_declared_macos_minimum_covers_the_bundled_numeric_wheels():
    config = json.loads((ROOT / "apps/desktop/src-tauri/tauri.conf.json").read_text())
    assert int(config["bundle"]["macOS"]["minimumSystemVersion"].split(".")[0]) >= 14


def test_windows_package_bundles_webview_and_timezone_data_for_clean_install():
    config = json.loads((ROOT / "apps/desktop/src-tauri/tauri.conf.json").read_text())
    assert config["bundle"]["windows"]["webviewInstallMode"]["type"] == "offlineInstaller"
    assert "tzdata" in (ROOT / "pyproject.toml").read_text()
    assert '"tzdata"' in (ROOT / "scripts/build_sidecar.py").read_text()


def test_development_macos_bundle_is_explicitly_signed_as_a_complete_bundle():
    config = json.loads((ROOT / "apps/desktop/src-tauri/tauri.conf.json").read_text())
    assert config["bundle"]["macOS"]["signingIdentity"] == "-"


def test_frozen_python_library_entitlement_is_scoped_away_from_the_webview():
    import plistlib

    app = plistlib.loads((ROOT / "apps/desktop/src-tauri/Entitlements.plist").read_bytes())
    backend = plistlib.loads((ROOT / "apps/desktop/src-tauri/BackendEntitlements.plist").read_bytes())
    assert backend["com.apple.security.cs.disable-library-validation"] is True
    assert not app.get("com.apple.security.cs.disable-library-validation")
    assert (ROOT / "scripts/package_macos.py").exists()
