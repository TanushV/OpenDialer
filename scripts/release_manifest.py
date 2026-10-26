"""Collect verified native artifacts and create one versioned multi-platform update manifest."""

import argparse
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--artifacts", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
version = json.loads((root / "apps/desktop/src-tauri/tauri.conf.json").read_text())["version"]
args.output.mkdir(parents=True, exist_ok=True)
files = {}
for path in args.artifacts.rglob("*"):
    if path.is_file() and (path.name.endswith((".dmg", ".exe", ".msi", ".tar.gz", ".sig"))):
        if path.name in files:
            raise SystemExit("Artifact filename collision: " + path.name)
        destination = args.output / path.name
        if path.resolve() != destination.resolve():
            shutil.copy2(path, destination)
        files[path.name] = destination
base = f"https://github.com/TanushV/OpenDialer/releases/download/v{version}/"
platforms = {}


def entry(filename):
    if filename not in files or filename + ".sig" not in files:
        raise SystemExit("Missing artifact or signature: " + filename)
    return {"url": base + filename, "signature": files[filename + ".sig"].read_text().strip()}


archive = next((name for name in files if name.endswith(".app.tar.gz")), None)
dmg = next((name for name in files if name.endswith(".dmg")), None)
if archive and dmg:
    platforms["darwin-aarch64" if "_aarch64" in dmg else "darwin-x86_64"] = entry(archive)
exe = next((name for name in files if name.endswith("-setup.exe")), None)
msi = next((name for name in files if name.endswith(".msi")), None)
if exe:
    platforms["windows-x86_64"] = entry(exe)
    platforms["windows-x86_64-nsis"] = entry(exe)
if msi:
    platforms["windows-x86_64-msi"] = entry(msi)
if not archive or not exe:
    raise SystemExit("Both Mac and Windows signed artifacts are required for this release")
manifest = {
    "version": version,
    "notes": f"OpenDialer {version}: voicemail drop removed; new workspaces default to calling from 8 a.m. until 9 p.m. in each prospect's timezone. Existing published hours remain editable in Settings. Updates from 0.4.x and 0.5.x keep the existing workspace. Versions 0.2/0.3 start a fresh workspace; old data stays untouched in its previous folder and is not migrated. Update through the signed updater.",
    "pub_date": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "platforms": platforms,
}
(args.output / "latest.json").write_text(json.dumps(manifest, indent=2) + "\n")
(args.output / "SHA256SUMS.txt").write_text(
    "".join(
        hashlib.sha256(p.read_bytes()).hexdigest() + "  " + name + "\n" for name, p in sorted(files.items())
    )
)
print("Prepared signed update manifest for " + ", ".join(platforms))
