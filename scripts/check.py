"""Local CI: the same checks GitHub runs, in tiers sized for fast iteration.

uv run python scripts/check.py                 fast tier (each change, pre-commit hook)
uv run python scripts/check.py full            everything Verify runs (pre-push hook)
uv run python scripts/check.py package         frozen sidecars plus a smoke start
uv run python scripts/check.py KEYWORD         lint plus the backend and frontend tests matching KEYWORD
uv run python scripts/check.py run STEP...     named steps; GitHub CI calls these
uv run python scripts/check.py install-hooks   turn on the .githooks pre-commit and pre-push hooks

Steps in a tier run side by side as separate processes and report together. Nothing here reads
the installed app's data or credentials, and nothing places a call or installs anything.
"""

import glob
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "apps/desktop"
TAURI = DESKTOP / "src-tauri"
LINT_PATHS = ["conftest.py", "services/backend", "tests", "scripts", "deploy/backup"]
PYTEST = [sys.executable, "-m", "pytest", "-q", "-n", "auto", "-p", "no:cacheprovider"]


def node_tests():
    return ["node", "--test", *sorted(glob.glob(str(ROOT / "supabase/functions/*/*.test.mjs")))]


# Each step is a list of commands that run in order, with the folder they run in.
STEPS = {
    "lint": [
        (["uv", "run", "ruff", "check", *LINT_PATHS], ROOT),
        (["uv", "run", "ruff", "format", "--check", *LINT_PATHS], ROOT),
    ],
    "types": [(["npm", "run", "--silent", "typecheck"], DESKTOP)],
    "format": [(["npm", "run", "--silent", "format:check"], DESKTOP)],
    "frontend-fast": [(["npx", "vitest", "run", "--silent"], DESKTOP)],
    "frontend": [
        (["npm", "run", "--silent", "build"], DESKTOP),
        (["npm", "test", "--silent"], DESKTOP),
    ],
    "edge": [(node_tests(), ROOT)],
    # Size caps and code-rule bans; warnings until Phase 3 (R3.10) adds --strict.
    "rules": [([sys.executable, "scripts/rules.py"], ROOT)],
    "backend-fast": [([*PYTEST, "-m", "not slow"], ROOT)],
    "backend": [(PYTEST, ROOT)],
    "rust": [
        (["cargo", "fmt", "--check"], TAURI),
        (["cargo", "test", "--locked", "--quiet"], TAURI),
        (["cargo", "clippy", "--locked", "--quiet", "--all-targets", "--", "-D", "warnings"], TAURI),
    ],
    "browser": [(["npx", "playwright", "test"], DESKTOP)],
}

TIERS = {
    "fast": ["lint", "rules", "types", "format", "frontend-fast", "edge", "backend-fast"],
    "full": ["lint", "rules", "types", "format", "frontend", "edge", "backend", "rust", "browser"],
}


# The browser suite runs after the rest, alone: run beside the backend on a shared Mac it starves
# both, and the slow-test guard and browser timeouts fail on load, not on defects.
STAGED = {"browser"}


def tier_steps(name):
    return {step: STEPS[step] for step in TIERS[name]}


def run_tier(name):
    steps = tier_steps(name)
    first = {k: v for k, v in steps.items() if k not in STAGED}
    after = {k: v for k, v in steps.items() if k in STAGED}
    if not run_steps(first):
        return False
    return not after or run_steps(after)


def keyword_steps(keyword):
    return {
        "lint": STEPS["lint"],
        "backend-match": [([*PYTEST, "-k", keyword], ROOT)],
        "frontend-match": [(["npx", "vitest", "run", "--silent", "--passWithNoTests", keyword], DESKTOP)],
    }


def run_steps(steps, note=None):
    """Start every step at once, then report each one; returns True only when all pass."""
    began = time.monotonic()
    logs = tempfile.TemporaryDirectory(prefix="opendialer-check-")
    running = {}
    for name, commands in steps.items():
        log = Path(logs.name) / f"{name}.log"
        # One shell per step keeps a step's commands in order while steps run side by side.
        shell = " && ".join(f"cd {_quote(str(folder))} && {_join(command)}" for command, folder in commands)
        handle = log.open("wb")
        process = subprocess.Popen(  # noqa: S602 - fixed commands; a keyword is quoted
            shell, shell=True, stdout=handle, stderr=subprocess.STDOUT, env=_env()
        )
        running[name] = (process, handle, log)
    failed = []
    # Report each step as it finishes, so its time is its own.
    while running:
        for name, (process, handle, log) in list(running.items()):
            code = process.poll()
            if code is None:
                continue
            handle.close()
            del running[name]
            took = time.monotonic() - began
            if code == 0:
                print(f"  pass  {name:15} {took:6.1f}s", flush=True)
            else:
                failed.append(name)
                print(f"  FAIL  {name:15} {took:6.1f}s", flush=True)
                print(_tail(log.read_text(errors="replace")), flush=True)
        time.sleep(0.1)
    if note:
        print(f"  skip  {note}")
    total = time.monotonic() - began
    print(("FAILED: " + ", ".join(failed) if failed else "All checks passed") + f" in {total:.1f}s")
    logs.cleanup()
    return not failed


def _join(command):
    return " ".join(_quote(part) for part in command)


def _quote(text):
    safe = text and all(c.isalnum() or c in "-_./=:,@+" for c in text)
    return text if safe else "'" + text.replace("'", "'\\''") + "'"


def _env():
    env = os.environ.copy()
    # Checks never see this Mac's saved credentials, app data or ChatGPT login.
    for name in ("OPENDIALER_SECRET_KEY_FILE", "OPENDIALER_DATA_DIR", "OPENDIALER_LOCAL_TOKEN"):
        env.pop(name, None)
    return env


def _tail(text, lines=60):
    rows = text.rstrip().splitlines()
    return "\n".join("        " + row for row in rows[-lines:])


def install_hooks():
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=ROOT, check=True)  # noqa: S607
    print("Hooks on: pre-commit runs the fast tier, pre-push runs the full tier.")
    return True


def package():
    """Freeze both sidecars, then prove the frozen backend serves /health and the trainer loads."""
    if platform.system() != "Darwin":
        raise SystemExit("The package tier runs on macOS")
    if not run_steps({"sidecars": [([sys.executable, "scripts/build_sidecar.py"], ROOT)]}):
        return False
    host = ["rustc", "--print", "host-tuple"]  # toolchain on PATH, as in CI
    triple = subprocess.run(host, capture_output=True, text=True, check=True)  # noqa: S603
    binaries = TAURI / "binaries"
    backend = binaries / f"opendialer-backend-{triple.stdout.strip()}"
    trainer = binaries / f"opendialer-trainer-{triple.stdout.strip()}"
    subprocess.run(  # noqa: S603 - the sidecar this tier just built
        [str(trainer), "--self-check"], check=True, stdout=subprocess.DEVNULL, timeout=300
    )
    print("  pass  trainer self-check")
    return smoke(backend)


def smoke(backend):
    with tempfile.TemporaryDirectory(prefix="opendialer-smoke-") as data:
        port = _free_port()
        env = _env()
        env["OPENDIALER_LOCAL_TOKEN"] = "smoke-local-token"  # noqa: S105 - disposable smoke-test token
        env["OPENDIALER_SECRET_KEY_FILE"] = str(Path(data) / "secret.key")
        process = subprocess.Popen(  # noqa: S603 - the sidecar this tier just built
            [str(backend), "serve", "--port", str(port), "--data-dir", data, "--simulation"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    print("  FAIL  backend exited: " + process.stderr.read().decode(errors="replace")[-2000:])
                    return False
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as reply:  # noqa: S310
                        if reply.status == 200:
                            print("  pass  frozen backend answered /health")
                            return True
                except OSError:
                    time.sleep(0.5)
            print("  FAIL  frozen backend did not answer /health within 90 seconds")
            return False
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main(argv):
    if not argv or argv[0] == "fast":
        return run_tier("fast")
    command, rest = argv[0], argv[1:]
    if command == "full":
        return run_tier("full")
    if command == "package":
        return package()
    if command == "install-hooks":
        return install_hooks()
    if command == "run":
        unknown = [step for step in rest if step not in STEPS]
        if not rest or unknown:
            raise SystemExit(f"Unknown or missing step {unknown}; steps: {', '.join(STEPS)}")
        return run_steps({step: STEPS[step] for step in rest})
    return run_steps(keyword_steps(command))


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1:]) else 1)
