"""Seal the Python sidecars with their own entitlement, then sign the final update bytes."""

import argparse
import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
IDENTIFIER = "com.opendialer.desktop"
BUNDLE = "OpenDialer.app"
# The local authority and its separate CPU trainer; both are frozen Python sidecars.
SIDECARS = ("opendialer-backend", "opendialer-trainer")


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)  # noqa: S603 - fixed build/signing tools with structured path arguments


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_archive(archive, version, sealed_sidecars, scratch):
    """Verify the shipped update bytes as the updater will install them.

    The bundle directory alone is not evidence: tar drops extended attributes and could omit or
    alter files. Extract the archive, then check its signature seal, identity, version and that
    it carries exactly each sidecar that was sealed with its scoped entitlement.
    """
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="archive-verify-", dir=scratch) as directory:
        destination = Path(directory)
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
            if not names or any(name != BUNDLE and not name.startswith(BUNDLE + "/") for name in names):
                raise SystemExit("Update archive contains entries outside OpenDialer.app")
            tar.extractall(destination, filter="data")
        extracted = destination / BUNDLE
        run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(extracted)])
        info = plistlib.loads((extracted / "Contents/Info.plist").read_bytes())
        if info.get("CFBundleIdentifier") != IDENTIFIER:
            raise SystemExit(f"Update archive bundle identifier is not {IDENTIFIER}")
        if info.get("CFBundleShortVersionString") != version:
            raise SystemExit(
                f"Update archive version {info.get('CFBundleShortVersionString')!r} is not {version!r}"
            )
        executable = extracted / "Contents/MacOS" / info.get("CFBundleExecutable", "")
        if not (executable.is_file() and os.access(executable, os.X_OK)):
            raise SystemExit("Update archive main executable is missing or not executable")
        for name in SIDECARS:
            shipped = extracted / "Contents/MacOS" / name
            if not (shipped.is_file() and os.access(shipped, os.X_OK)):
                raise SystemExit(f"Update archive sidecar {name} is missing or not executable")
            if sha256(shipped) != sha256(sealed_sidecars[name]):
                raise SystemExit(f"Update archive sidecar {name} differs from the sealed sidecar")


def main():
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
    if args.build:
        run([npm, "run", "tauri", "build", "--", "--bundles", "app"], cwd=root / "apps/desktop")
    app = root / "apps/desktop/src-tauri/target/release/bundle/macos" / BUNDLE
    sidecars = {name: app / "Contents/MacOS" / name for name in SIDECARS}
    if not all(path.is_file() for path in sidecars.values()):
        raise SystemExit("Freeze both sidecars and build the complete app before packaging")
    stamp = "--timestamp=none" if identity == "-" else "--timestamp"
    for sidecar in sidecars.values():
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
                str(sidecar),
            ]
        )
    # Re-seal only the outer bundle; --deep here would overwrite the sidecars' scoped entitlement.
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
    run([str(sidecars["opendialer-backend"]), "--help"], stdout=subprocess.DEVNULL, timeout=30)
    # The signed trainer must load its own fitting libraries under the hardened runtime.
    run([str(sidecars["opendialer-trainer"]), "--self-check"], stdout=subprocess.DEVNULL, timeout=300)
    archive = app.parent / "OpenDialer.app.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(app, arcname=BUNDLE)
    (root / "work").mkdir(exist_ok=True)
    # Verify the exact bytes before the updater key signs them.
    verify_archive(archive, version, sidecars, root / "work")
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
    if not Path(str(archive) + ".sig").is_file():
        raise SystemExit("The updater signature for the verified archive was not produced")
    architecture = "aarch64" if platform.machine() == "arm64" else "x64"
    dmg = root / f"apps/desktop/src-tauri/target/release/bundle/dmg/OpenDialer_{version}_{architecture}.dmg"
    dmg.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dmg-", dir=root / "work") as directory:
        staging = Path(directory)
        shutil.copytree(app, staging / BUNDLE, symlinks=True)
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


if __name__ == "__main__":
    main()
