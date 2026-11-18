"""Local CI: the same checks GitHub runs, in tiers sized for fast iteration.

uv run python scripts/check.py                 fast tier (each change, pre-commit hook)
uv run python scripts/check.py full            everything Verify runs (pre-push hook)
uv run python scripts/check.py package         frozen sidecars plus a smoke start
uv run python scripts/check.py sidecars        check the backend sidecar already built: it starts and loads search
uv run python scripts/check.py stress          long, seeded calling stress runs against disposable data
uv run python scripts/check.py KEYWORD         lint plus the backend and frontend tests matching KEYWORD
uv run python scripts/check.py run STEP...     named steps; GitHub CI calls these
uv run python scripts/check.py install-hooks   turn on the .githooks pre-commit and pre-push hooks

Steps in a tier run side by side as separate processes and report together. Nothing here reads
the installed app's data or credentials, and nothing places a call or installs anything.
"""

import glob
import os
import platform
import secrets
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
LINT_PATHS = ["conftest.py", "services/backend", "tests", "scripts"]
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
    "rules": [
        ([sys.executable, "scripts/rules.py"], ROOT),
        ([sys.executable, "scripts/calling_budget.py"], ROOT),
    ],
    "backend-fast": [([*PYTEST, "-m", "not slow"], ROOT)],
    "backend": [(PYTEST, ROOT)],
    "rust": [
        (["cargo", "fmt", "--check"], TAURI),
        (["cargo", "test", "--locked", "--quiet"], TAURI),
        (["cargo", "clippy", "--locked", "--quiet", "--all-targets", "--", "-D", "warnings"], TAURI),
    ],
    "browser": [(["npx", "playwright", "test"], DESKTOP)],
    # R8.10: checks sidecars already built (CI's Mac job builds them first); not part of any tier.
    "sidecars": [([sys.executable, "scripts/check.py", "sidecars"], ROOT)],
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
    logs = tempfile.TemporaryDirectory(prefix="opendialer-check-", ignore_cleanup_errors=True)
    running = {}
    for name, commands in steps.items():
        log = Path(logs.name) / f"{name}.log"
        # One shell per step keeps a step's commands in order while steps run side by side.
        cd = "cd /d" if os.name == "nt" else "cd"
        shell = " && ".join(f"{cd} {_quote(str(folder))} && {_join(command)}" for command, folder in commands)
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
    if os.name == "nt":
        return subprocess.list2cmdline([text])  # cmd.exe takes double quotes, never single ones (R9D.1).
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
    # CI keeps the whole log of a failing step; locally the end is enough.
    keep = rows if os.environ.get("CI") else rows[-lines:]
    return "\n".join("        " + row for row in keep)


def install_hooks():
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=ROOT, check=True)  # noqa: S607
    print("Hooks on: pre-commit runs the fast tier, pre-push runs the full tier.")
    return True


def package():
    """Freeze both sidecars, then prove they run (see `sidecars`)."""
    if platform.system() != "Darwin":
        raise SystemExit("The package tier runs on macOS")
    if not run_steps({"sidecars": [([sys.executable, "scripts/build_sidecar.py"], ROOT)]}):
        return False
    return sidecars()


def sidecars():
    """The frozen backend loads web search and answers /health. Checks the sidecar already built, as CI's Mac job
    does. (The trainer it once checked was archived with StatML, D31.)"""
    host = ["rustc", "--print", "host-tuple"]  # toolchain on PATH, as in CI
    triple = subprocess.run(host, capture_output=True, text=True, check=True)  # noqa: S603
    binaries = TAURI / "binaries"
    suffix = ".exe" if os.name == "nt" else ""  # R9D.1: the Windows sidecars are .exe files.
    backend = binaries / f"opendialer-backend-{triple.stdout.strip()}{suffix}"
    searched = subprocess.run(  # noqa: S603 - the sidecar this tier just built
        [str(backend), "self-check"], capture_output=True, text=True, timeout=120, check=False
    )
    if searched.returncode or not searched.stdout.startswith("ok: ddgs"):
        print(
            "  FAIL  frozen backend cannot load web search: " + (searched.stderr or searched.stdout)[-1500:]
        )
        return False
    print("  pass  backend self-check (web search loads)")
    return smoke(backend)


def smoke(backend):
    import httpx

    # Windows can hold a just-stopped sidecar's files for a moment; a leftover temp folder is not a failure (R9D.1).
    with tempfile.TemporaryDirectory(prefix="opendialer-smoke-", ignore_cleanup_errors=True) as data:
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
                        break
                except OSError:
                    time.sleep(0.5)
            else:
                print("  FAIL  frozen backend did not answer /health within 90 seconds")
                return False
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False) as client:
                packaged_calls(client, env["OPENDIALER_LOCAL_TOKEN"])
            print("  pass  frozen backend placed and settled two isolated test attempts")
            return True
        finally:
            stop(process)


def packaged_calls(client, token):
    """Exercise the packaged authority through its ordinary API on an empty practice workspace."""

    def request(method, path, data=None):
        response = client.request(method, path, json=data)
        assert response.is_success, f"{method} {path}: {response.status_code} {response.text[:400]}"
        return response.json()

    health = request("GET", "/health")
    assert health["simulation"] and not health["initialized"], "Calling smoke requires a fresh simulator"
    client.headers["X-Local-Token"] = token
    setup = request("POST", "/v1/setup", {"seller_name": "Packaged calling test", "campaign_name": "Smoke"})
    client.headers["Authorization"] = "Bearer " + setup["access_token"]
    seeded = request("POST", "/v1/simulation/seed")
    phones = ["+12025550181", "+12025550182"]
    request(
        "PUT",
        "/v1/settings/test-list",
        {"rows": [{"name": f"Fixture {i}", "phone": phone} for i, phone in enumerate(phones)]},
    )
    run = request("POST", "/v1/settings/test-list/start")
    queue = {"campaign_id": seeded["campaign_id"], "test_run_id": run["id"], "test_only": True}

    def event(call_id, kind, **payload):
        request(
            "POST",
            "/v1/simulation/event",
            {"call_id": call_id, "event_type": kind, "event_id": f"{call_id}:{kind}", "payload": payload},
        )

    called = []
    for index, outcome in enumerate(("voicemail", "no_answer")):
        agent = request("POST", "/v1/telephony/agent")["agent_call_control_id"]
        request(
            "POST",
            "/v1/rep/presence",
            {
                "status": "ready",
                "audio_ready": True,
                "telephony_ready": True,
                "agent_call_control_id": agent,
            },
        )
        reservation = request("POST", "/v1/queue/next", queue)
        called.append(reservation["lead"]["phone"])
        reservation_id = reservation["reservation_id"]
        data = {"reservation_id": reservation_id, "idempotency_key": reservation_id}
        call = request("POST", "/v1/calls/start", data)
        assert call["is_test"] and call["provider_id"], call
        assert request("POST", "/v1/calls/start", data)["id"] == call["id"]
        call_id = call["id"]
        path = f"/v1/calls/{call_id}"
        if outcome == "voicemail":
            event(call_id, "call.answered")
            event(call_id, "call.machine.premium.detection.ended:machine")
            event(call_id, "call.machine.premium.greeting.ended:beep_detected")
            assert request("GET", path)["state"] == "VOICEMAIL_READY"
            request("POST", path + "/hangup", {"mark_voicemail": True})
        else:
            event(call_id, "call.hangup", hangup_source="callee", hangup_cause="no_answer")
        settled = request("GET", path)
        assert (settled["state"], settled["outcome"]) == ("FINALIZED", outcome)
        assert len(request("GET", "/v1/workspace/calls")["items"]) == index + 1
    assert sorted(called) == phones


def stop(process):
    """End a sidecar. On Windows a onefile sidecar is a parent and child, so the whole tree is ended."""
    if os.name == "nt":
        # The system's own taskkill, on the process this script started.
        tree = ["taskkill", "/T", "/F", "/PID", str(process.pid)]
        subprocess.run(tree, capture_output=True, check=False)  # noqa: S603, S607
    else:
        process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def stress():
    env = _env()
    defaults = {
        "SOAK_SEED_START": str(secrets.randbits(32)),
        "SOAK_RUNS": "512",
        "SOAK_STEPS": "200",
        "SOAK_SHARED_RUNS": "48",
        "SOAK_SHARED_EVENTS": "80",
        "SOAK_WORKERS": "6",
        "OPENDIALER_REQUIRE_RELAY_TESTS": "1",
    }
    for name, value in defaults.items():
        env.setdefault(name, value)
    print("Calling stress " + " ".join(f"{name}={env[name]}" for name in defaults), flush=True)
    if "SOAK_SEED" in env:
        print(f"Replaying only SOAK_SEED={env['SOAK_SEED']}", flush=True)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-n",
        env["SOAK_WORKERS"],
        "-p",
        "no:cacheprovider",
        "-x",
        "--tb=short",
        "services/backend/tests/test_fault_soak.py",
        "tests/test_shared_calling_stress.py",
    ]
    return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode == 0  # noqa: S603


def main(argv):
    if not argv or argv[0] == "fast":
        return run_tier("fast")
    command, rest = argv[0], argv[1:]
    if command == "full":
        return run_tier("full")
    if command == "package":
        return package()
    if command == "sidecars":
        return sidecars()
    if command == "stress":
        return stress()
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
