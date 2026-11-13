"""Freeze the local authority into a self-contained sidecar and bundle the pinned Codex runtime.

The desktop authority only runs local mode on SQLite, so it ships without the numeric stack, the PostgreSQL drivers
or the phone geocoding/carrier/timezone tables. A onefile binary re-extracts its whole payload on every launch, so
everything left out here is startup time saved. The model trainer was archived with StatML (D31).
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]

# Never in the authority: fitting libraries, PostgreSQL drivers (connected mode is
# the server image), phone tables that app/compliance/phone.py never reads, and test-only probes.
AUTHORITY_EXCLUDES = (
    "psutil",
    "numpy",
    "scipy",
    "sklearn",
    "joblib",
    "threadpoolctl",
    "psycopg",
    "psycopg_binary",
    "psycopg_c",
    "psycopg_pool",
    "phonenumbers.geodata",
    "phonenumbers.carrierdata",
    "phonenumbers.tzdata",
    "phonenumbers.geocoder",
    "phonenumbers.carrier",
    "phonenumbers.timezone",
    # Optional native accelerators. macOS verifies every freshly extracted native image on its first
    # load (about 0.2 s each), so each one costs startup on every launch; all have fallbacks.
    "uvloop",
    "httptools",
    "watchfiles",
    "websockets.speedups",
    "markupsafe._speedups",
    "yaml._yaml",
    "charset_normalizer",
    # Development tools reached only through optional imports; setuptools also adds a runtime hook
    # that imports it on every launch.
    "setuptools",
    "_distutils_hack",
    "pytest",
    "_pytest",
    "pygments",
    "rich",
    "markdown_it",
    "trio",
)


def host_triple():
    output = subprocess.check_output(  # noqa: S603 - fixed toolchain probe
        [shutil.which("rustc") or "/usr/bin/rustc", "-vV"], text=True
    )
    return output.split("host: ")[1].splitlines()[0]


# SQLAlchemy 2.1 compiles these pure-Python modules with Cython. The authority ships their sources
# instead: eight fewer native images to verify per launch, same documented behavior.
SQLALCHEMY_CYTHON = (
    "sqlalchemy.util._collections_cy",
    "sqlalchemy.util._immutabledict_cy",
    "sqlalchemy.engine._processors_cy",
    "sqlalchemy.engine._result_cy",
    "sqlalchemy.engine._row_cy",
    "sqlalchemy.engine._util_cy",
    "sqlalchemy.sql._cache_key_cy",
    "sqlalchemy.sql._util_cy",
)


def flags(option, values):
    return [part for value in values for part in (option, value)]


def pure_sqlalchemy_arguments():
    import sqlalchemy

    base = Path(sqlalchemy.__file__).resolve().parent.parent
    arguments = ["--hidden-import", "sqlalchemy.util.cython"]
    for name in SQLALCHEMY_CYTHON:
        source = base.joinpath(*name.split(".")).with_suffix(".py")
        if not source.is_file():
            raise SystemExit(f"SQLAlchemy pure-Python source is missing: {name}")
        arguments += [
            "--exclude-module",
            name,
            "--add-data",
            f"{source}{os.pathsep}{source.parent.relative_to(base).as_posix()}",
        ]
    return arguments


def authority_arguments():
    return [
        *flags("--exclude-module", AUTHORITY_EXCLUDES),
        *pure_sqlalchemy_arguments(),
        "--name",
        "opendialer-backend",
        "--collect-submodules",
        "app",
        # Phone parsing metadata only (see scripts/pyinstaller_hooks/hook-phonenumbers.py).
        "--additional-hooks-dir",
        str(root / "scripts/pyinstaller_hooks"),
        "--collect-all",
        "tzdata",
        # R9.11: the fallback agent's search loads its engines by name and needs primp's native library.
        "--collect-all",
        "ddgs",
        "--collect-all",
        "primp",
        str(root / "scripts/backend_entry.py"),
    ]


def freeze(name, arguments, triple, suffix):
    work = root / "work"
    subprocess.run(  # noqa: S603 - fixed current interpreter and repository build entrypoint
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--paths",
            str(root / "services/backend"),
            "--distpath",
            str(work / "sidecar-dist"),
            "--workpath",
            str(work / "sidecar-build"),
            "--specpath",
            str(work),
            *arguments,
        ],
        check=True,
        cwd=root,
    )
    out = root / "apps/desktop/src-tauri/binaries" / f"{name}-{triple}{suffix}"
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(work / "sidecar-dist" / f"{name}{suffix}", out)
    print(f"Bundled {name}: {out.name} ({out.stat().st_size // 1024 // 1024} MiB)")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    triple = host_triple()
    suffix = ".exe" if platform.system() == "Windows" else ""
    freeze("opendialer-backend", authority_arguments(), triple, suffix)
    bundle_codex(triple, suffix)


def bundle_codex(triple, suffix):
    """Copy the npm-lockfile-pinned native runtime; no Node dependency on the user's PC."""
    # Codex ships static Linux executables even when the Tauri host uses glibc.
    vendor_target = triple.replace("-linux-gnu", "-linux-musl")
    candidates = list(
        (root / "apps/desktop/node_modules/@openai").glob(f"codex-*/vendor/{vendor_target}/bin/codex{suffix}")
    )
    if len(candidates) != 1:
        raise SystemExit("Run npm ci in apps/desktop to install the pinned Codex runtime for this platform")
    destination = root / "apps/desktop/src-tauri/binaries" / f"opendialer-codex-{triple}{suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(candidates[0], destination)
    helper = candidates[0].with_name("codex-code-mode-host" + suffix)
    shutil.copy2(helper, destination.with_name(f"codex-code-mode-host-{triple}{suffix}"))
    return destination


if __name__ == "__main__":
    main()
