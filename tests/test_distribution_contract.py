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
