import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
