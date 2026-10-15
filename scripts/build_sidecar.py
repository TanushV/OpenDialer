"""Freeze the actual backend and CPU model libraries into a self-contained sidecar."""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
os.chdir(root)
triple = (
    subprocess.check_output([shutil.which("rustc") or "/usr/bin/rustc", "-vV"], text=True)  # noqa: S603 - fixed toolchain probe
    .split("host: ")[1]
    .splitlines()[0]
)  # noqa: S603 - fixed toolchain probe
suffix = ".exe" if platform.system() == "Windows" else ""
subprocess.run(  # noqa: S603 - fixed current interpreter and repository build entrypoint
    [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "opendialer-backend",
        "--paths",
        str(root / "services/backend"),
        "--collect-submodules",
        "app",
        "--collect-all",
        "phonenumbers",
        "--collect-all",
        "tzdata",
        "--add-data",
        f"{root / 'services/backend/migrations'}{os.pathsep}migrations",
        "--add-data",
        f"{root / 'services/backend/alembic.ini'}{os.pathsep}alembic.ini",
        "--distpath",
        "work/sidecar-dist",
        "--workpath",
        "work/sidecar-build",
        "--specpath",
        "work",
        str(root / "scripts/backend_entry.py"),
    ],
    check=True,
)
out = root / "apps/desktop/src-tauri/binaries" / f"opendialer-backend-{triple}{suffix}"
out.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(root / "work/sidecar-dist" / f"opendialer-backend{suffix}", out)
print(f"Bundled backend: {out.name} ({out.stat().st_size // 1024 // 1024} MiB)")
