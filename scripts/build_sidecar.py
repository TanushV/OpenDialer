"""Freeze the local authority and its separate CPU trainer into self-contained sidecars.

The desktop authority only runs local mode on SQLite and serves promoted funnel models with a
pure-Python evaluator, so it ships without the numeric stack, the PostgreSQL drivers or the phone
geocoding/carrier/timezone tables. A onefile binary re-extracts its whole payload on every launch,
so everything left out here is startup time saved. Fitting runs in the separate trainer binary,
which carries NumPy, SciPy and scikit-learn and is extracted only when a training child starts.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]

# Never in the authority: fitting libraries (trainer only), PostgreSQL drivers (connected mode is
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
# The trainer runs app.ml.worker only: fitting never imports the server, database, encryption,
# phone or research stacks (static analysis of app would otherwise pull them all in).
TRAINER_EXCLUDES = (
    "psutil",
    "psycopg",
    "psycopg_binary",
    "psycopg_c",
    "psycopg_pool",
    "fastapi",
    "starlette",
    "uvicorn",
    "uvloop",
    "httptools",
    "watchfiles",
    "websockets",
    "sqlalchemy",
    "alembic",
    "mako",
    "cryptography",
    "phonenumbers",
    "httpx",
    "httpcore",
    "h11",
    "bs4",
    "soupsieve",
    "charset_normalizer",
    "pwdlib",
    "argon2",
    "_argon2_cffi_bindings",
    "cffi",
    "jwt",
    "yaml",
    "setuptools",
    "_distutils_hack",
    "pytest",
    "_pytest",
    "pygments",
    "rich",
    "markdown_it",
    "trio",
)
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
TRAINER_IMPORTS = (
    "app.ml.worker",
    "app.ml.training",
    "app.ml.serving",
    "sklearn.ensemble",
    "sklearn.calibration",
    "sklearn.frozen",
    "sklearn.metrics",
    "threadpoolctl",
)


def host_triple():
    output = subprocess.check_output(  # noqa: S603 - fixed toolchain probe
        [shutil.which("rustc") or "/usr/bin/rustc", "-vV"], text=True
    )
    return output.split("host: ")[1].splitlines()[0]


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
        "--add-data",
        f"{root / 'services/backend/migrations'}{os.pathsep}migrations",
        "--add-data",
        f"{root / 'services/backend/alembic.ini'}{os.pathsep}alembic.ini",
        str(root / "scripts/backend_entry.py"),
    ]


def trainer_arguments():
    return [
        *flags("--exclude-module", TRAINER_EXCLUDES),
        "--name",
        "opendialer-trainer",
        *flags("--hidden-import", TRAINER_IMPORTS),
        "--collect-all",
        "tzdata",
        str(root / "scripts/trainer_entry.py"),
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
    parser.add_argument("--only", choices=("authority", "trainer"))
    args = parser.parse_args(argv)
    triple = host_triple()
    suffix = ".exe" if platform.system() == "Windows" else ""
    if args.only != "trainer":
        freeze("opendialer-backend", authority_arguments(), triple, suffix)
    if args.only != "authority":
        freeze("opendialer-trainer", trainer_arguments(), triple, suffix)


if __name__ == "__main__":
    main()
