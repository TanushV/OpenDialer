"""Seal the Python sidecar with its own entitlement, then sign the final update bytes."""

import argparse
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--build", action="store_true")
args = parser.parse_args()
if platform.system() != "Darwin":
    raise SystemExit("Build macOS packages on macOS")
config = json.loads((root / "apps/desktop/src-tauri/tauri.conf.json").read_text())
version = config["version"]
identity = os.environ.get("APPLE_SIGNING_IDENTITY", config["bundle"]["macOS"]["signingIdentity"])
npm = shutil.which("npm")
if not npm:
    raise SystemExit("npm is required for the maintained Tauri signer")


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)  # noqa: S603 - fixed build/signing tools with structured path arguments


if args.build:
    run([npm, "run", "tauri", "build", "--", "--bundles", "app"], cwd=root / "apps/desktop")
app = root / "apps/desktop/src-tauri/target/release/bundle/macos/OpenDialer.app"
backend = app / "Contents/MacOS/opendialer-backend"
if not backend.is_file():
    raise SystemExit("Freeze and build the complete app before packaging")
stamp = "--timestamp=none" if identity == "-" else "--timestamp"
run(
    [
        "/usr/bin/codesign",
        "--force",
        "--sign",
        identity,
        "--options",
        "runtime",
        stamp,
        "--entitlements",
        str(root / "apps/desktop/src-tauri/BackendEntitlements.plist"),
        str(backend),
    ]
)
# Re-seal only the outer bundle; --deep here would overwrite the sidecar's scoped entitlement.
run(
    [
        "/usr/bin/codesign",
        "--force",
        "--sign",
        identity,
        "--options",
        "runtime",
        stamp,
        "--entitlements",
        str(root / "apps/desktop/src-tauri/Entitlements.plist"),
        str(app),
    ]
)
run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
run([str(backend), "--help"], stdout=subprocess.DEVNULL, timeout=30)
archive = app.parent / "OpenDialer.app.tar.gz"
with tarfile.open(archive, "w:gz") as tar:
    tar.add(app, arcname="OpenDialer.app")
signing_env = os.environ.copy()
key = signing_env.get("TAURI_SIGNING_PRIVATE_KEY", "")
if key and len(key) < 240 and Path(key).is_file():
    signing_env.pop("TAURI_SIGNING_PRIVATE_KEY")
    signing_env["TAURI_SIGNING_PRIVATE_KEY_PATH"] = key
run(
    [npm, "run", "tauri", "signer", "sign", "--", "--app-version", version, str(archive)],
    cwd=root / "apps/desktop",
    env=signing_env,
)
architecture = "aarch64" if platform.machine() == "arm64" else "x64"
dmg = root / f"apps/desktop/src-tauri/target/release/bundle/dmg/OpenDialer_{version}_{architecture}.dmg"
dmg.parent.mkdir(parents=True, exist_ok=True)
(root / "work").mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix="dmg-", dir=root / "work") as directory:
    staging = Path(directory)
    shutil.copytree(app, staging / "OpenDialer.app", symlinks=True)
    (staging / "Applications").symlink_to("/Applications")
    run(
        [
            "/usr/bin/hdiutil",
            "create",
            "-volname",
            "OpenDialer",
            "-srcfolder",
            str(staging),
            "-ov",
            "-format",
            "UDZO",
            str(dmg),
        ],
        stdout=subprocess.DEVNULL,
    )
run(["/usr/bin/hdiutil", "verify", str(dmg)], stdout=subprocess.DEVNULL)
print(
    f"Verified macOS {architecture} package and signed update {version}. Main webview retains library validation."
)
