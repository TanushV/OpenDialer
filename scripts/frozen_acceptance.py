"""Prove a packaged sidecar in a disposable directory with no developer runtime on PATH."""

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx


def isolated_environment(source):
    # Windows reports environment keys upper-cased (SYSTEMROOT); match case-insensitively.
    allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
    return {
        **{k: v for k, v in source.items() if k.upper() in allowed},
        "PATH": "",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        **dict.fromkeys(
            (
                "OMP_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "MKL_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS",
                "NUMEXPR_NUM_THREADS",
            ),
            "1",
        ),
    }


def log_tail(log, lines=40):
    try:
        return "\n".join(log.read_text(errors="replace").splitlines()[-lines:]) if log else ""
    except OSError:
        return ""


def await_ready(client, expected_version, *, timeout=60, process=None, log=None):
    """The reported startup time is measured separately; this bound only stops a stuck run."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(
                f"Frozen authority exited with code {process.returncode} before it was ready\n{log_tail(log)}"
            )
        try:
            response = client.get("/health")
            response.raise_for_status()
        except httpx.HTTPError:
            time.sleep(0.1)
            continue
        health = response.json()
        if health.get("version") != expected_version:
            raise ValueError("Frozen backend version differs from the frozen release input")
        return health
    raise TimeoutError(f"Frozen authority did not become ready\n{log_tail(log)}")


def start_owned(*args, **kwargs):
    """Retain descendant birth identities before a launcher can exit/reparent them."""
    import psutil

    kwargs.setdefault("start_new_session", True)
    process = subprocess.Popen(*args, **kwargs)  # noqa: S603 - acceptance callers supply fixed commands
    process._owned = {process.pid: psutil.Process(process.pid)}
    process._ownership_stop = threading.Event()
    process._ownership_errors = []

    def observe():
        while not process._ownership_stop.is_set():
            for parent in list(process._owned.values()):
                try:
                    if parent.is_running():
                        for child in parent.children(recursive=True):
                            child.create_time()  # Cache identity before PID reuse.
                            process._owned.setdefault(child.pid, child)
                except psutil.NoSuchProcess:
                    pass
                except psutil.AccessDenied:
                    process._ownership_errors.append("descendant_access_denied")
            process._ownership_stop.wait(0.1)

    process._ownership_thread = threading.Thread(target=observe, daemon=True)
    process._ownership_thread.start()
    return process


def start_authority(command, secrets_line, **kwargs):
    """Launch like the desktop: secrets as one stdin JSON line, never environment variables.

    The pipe stays open for the authority's life; closing it is the launcher-loss signal.
    """
    process = start_owned(command, stdin=subprocess.PIPE, **kwargs)
    process.stdin.write(secrets_line)
    process.stdin.flush()
    return process


def stop_owned(process, *, platform_name=os.name, system_root=None):
    import psutil

    tracked = getattr(process, "_owned", None)
    if tracked is None and process.poll() is not None:
        return  # An arbitrary, already-reaped PID is never ownership proof.
    if tracked is None and platform_name == "nt":
        tool = Path(system_root or os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
        subprocess.run(  # noqa: S603 - absolute OS utility; exact owned PID, never a process-name kill
            [str(tool), "/PID", str(process.pid), "/T", "/F"], check=True, capture_output=True, timeout=20
        )
    else:
        if tracked is not None:
            process._ownership_stop.set()
            process._ownership_thread.join(timeout=2)
            if process._ownership_thread.is_alive():
                raise RuntimeError("Owned process watcher did not stop")
        else:
            root = psutil.Process(process.pid)
            tracked = {child.pid: child for child in [root, *root.children(recursive=True)]}
        # Capture last still-parented descendants, retaining reparented handles.
        for parent in list(tracked.values()):
            try:
                for child in parent.children(recursive=True):
                    tracked.setdefault(child.pid, child)
            except psutil.NoSuchProcess:
                pass

        def alive():
            result = []
            for child in tracked.values():
                try:
                    if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                        result.append(child)
                except psutil.NoSuchProcess:
                    pass
            return result

        for child in reversed(alive()):
            try:
                child.terminate()  # psutil rechecks the retained PID/birth identity.
            except psutil.NoSuchProcess:
                pass
        deadline = time.monotonic() + 2
        while alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        for child in alive():
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        deadline = time.monotonic() + 10
        while alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if alive() or getattr(process, "_ownership_errors", []):
            raise RuntimeError("Owned process cleanup is incomplete")
    process.wait(timeout=10)


def request(client, headers, method, path, **kwargs):
    response = client.request(method, path, headers=headers, **kwargs)
    response.raise_for_status()
    return response.json()


def call_journey(client, headers, campaign, *, pause=time.sleep):
    def api(method, path, **kwargs):
        return request(client, headers, method, path, **kwargs)

    leads = api("GET", "/v1/workspace")["leads"]
    api("POST", "/v1/research/batches", json={"lead_ids": [lead["id"] for lead in leads]})
    for _ in range(4):
        work = api("POST", "/v1/admin/jobs/run")
        assert not work.get("errors"), work.get("errors")
    api(
        "POST",
        "/v1/rep/presence",
        json={
            "status": "ready",
            "audio_ready": True,
            "telephony_ready": True,
            "agent_call_control_id": "agent-frozen",
        },
    )
    reservation = api("POST", "/v1/queue/next", json={"campaign_id": campaign})
    call = api(
        "POST",
        "/v1/calls/start",
        json={
            "reservation_id": reservation["reservation_id"],
            "idempotency_key": "frozen-journey",
        },
    )
    cid, lead_id = call["id"], call["lead_id"]
    packet = api("GET", f"/v1/leads/{lead_id}/packet")
    assert packet["script"]["ready_for_rep"]
    api(
        "POST",
        "/v1/simulation/event",
        json={
            "event_id": "frozen-answer",
            "event_type": "call.answered",
            "call_id": cid,
        },
    )
    analysis = api("GET", f"/v1/calls/{cid}/analysis")
    human_note = "Rep confirmed an approval discussion for Tuesday."
    api(
        "PATCH",
        f"/v1/calls/{cid}/notes",
        json={
            "expected_version": analysis["notes_version"],
            "text": human_note,
        },
    )
    api(
        "POST",
        f"/v1/calls/{cid}/transcript",
        json={
            "segment_id": "early",
            "revision": 1,
            "side": "remote",
            "is_final": True,
            "text": "Please call me Tuesday at two Pacific about approval times.",
        },
    )
    identity = str(uuid4())
    help_result = api(
        "POST",
        f"/v1/calls/{cid}/coach",
        json={
            "request_id": identity,
            "question": "Help clarify the next step.",
        },
    )
    assert help_result["request_id"] == identity
    cancelled = api("POST", f"/v1/calls/{cid}/coach/cancel", json={"request_id": identity})
    assert cancelled["status"] == "cancelled"
    # Actual frozen acceptance waits twelve wall-clock minutes. Source tests use
    # an injected clock; neither path relaxes the production 300-second lifetime.
    pause(360)
    assert api("GET", f"/v1/calls/{cid}/analysis")["transcript"] == []
    api(
        "POST",
        f"/v1/calls/{cid}/transcript",
        json={
            "segment_id": "later",
            "revision": 1,
            "side": "remote",
            "is_final": True,
            "text": "The budget is not approved, but the next discussion is still useful.",
        },
    )
    pause(360)
    assert api("GET", f"/v1/calls/{cid}/analysis")["transcript"] == []
    api("POST", f"/v1/calls/{cid}/hangup")
    current = api("GET", f"/v1/calls/{cid}")
    due = "2026-10-20T21:00:00+00:00"
    body = {
        "request_id": str(uuid4()),
        "expected_version": current["version"],
        "outcome": "callback_requested",
        "contact_role": "right_party",
        "callback_at": due,
        "callback_note": human_note,
    }
    api("POST", f"/v1/calls/{cid}/disposition", json=body)
    api("POST", f"/v1/calls/{cid}/disposition", json=body)
    api("POST", "/v1/rep/presence", json={"status": "paused"})
    for _ in range(8):
        work = api("POST", "/v1/admin/jobs/run")
        assert not work.get("errors"), work.get("errors")
        metrics = api("GET", "/v1/metrics")
        if metrics["projection"]["status"] == "ready":
            break
    else:
        raise AssertionError("Projection did not finish")
    tasks = [t for t in api("GET", "/v1/workspace")["tasks"] if t.get("lead_id") == lead_id]
    assert len(tasks) == 1 and tasks[0]["due_at"] == due
    result = {
        "call_id": cid,
        "lead_id": lead_id,
        "task_id": tasks[0]["id"],
        "callback_at": due,
        "note_hash": hashlib.sha256((human_note + "\n\nWrap-up: " + human_note).encode()).hexdigest(),
        "raw_expired": True,
        "helper_cancelled": True,
        "helper_evidence": "simulation fallback and request lifecycle; no funded inference",
        "elapsed_call_seconds": 720,
    }
    check_persisted(client, headers, result)
    return result


def check_persisted(client, headers, result):
    analysis = request(client, headers, "GET", f"/v1/calls/{result['call_id']}/analysis")
    assert not analysis["transcript"]
    assert hashlib.sha256(analysis["human_notes"].encode()).hexdigest() == result["note_hash"]
    workspace = request(client, headers, "GET", "/v1/workspace")
    tasks = [t for t in workspace["tasks"] if t.get("lead_id") == result["lead_id"]]
    assert len(tasks) == 1 and tasks[0]["id"] == result["task_id"]
    assert tasks[0]["due_at"] == result["callback_at"]
    packet = request(client, headers, "GET", f"/v1/leads/{result['lead_id']}/packet")
    assert result["callback_at"] in json.dumps(packet)
    preflight = request(client, headers, "GET", "/v1/admin/preflight")
    assert preflight["integrity_ok"]
    return {"task_id": tasks[0]["id"], "note_hash": result["note_hash"], "integrity_ok": True}


ARTIFACT_FORMAT = "opendialer.funnel-trees.v1"


def trainer_for(binary, override=None):
    """The trainer installed beside the authority: same target-triple and .exe suffix."""
    if override:
        return Path(override).resolve(strict=True)
    if not binary.name.startswith("opendialer-backend"):
        raise ValueError("Select the authority binary (opendialer-backend...) to locate its trainer")
    trainer = binary.with_name(binary.name.replace("opendialer-backend", "opendialer-trainer", 1))
    if not trainer.is_file():
        raise FileNotFoundError(f"Packaged trainer {trainer.name} is missing beside the authority")
    return trainer


def check_training_reply(contents, output_dir, authority_pid):
    result = json.loads(contents)
    artifact = output_dir / "challenger.json"
    runtime = result.get("training_runtime", {})
    if (
        result.get("artifact") != str(artifact)
        or artifact.is_symlink()
        or artifact.stat().st_size > 64 * 1024 * 1024
        or hashlib.sha256(artifact.read_bytes()).hexdigest() != result.get("checksum")
        or not isinstance(runtime.get("pid"), int)
        or runtime["pid"] <= 0
        or runtime["pid"] == authority_pid
        or runtime.get("native_threads") != 1
        or runtime.get("priority") != "low"
    ):
        raise ValueError("Invalid frozen training child receipt")
    try:
        document = json.loads(artifact.read_bytes())
    except ValueError:
        document = None
    stages = sorted(document.get("stages", {})) if isinstance(document, dict) else []
    if (
        not isinstance(document, dict)
        or document.get("format") != ARTIFACT_FORMAT
        or result.get("artifact_format") != ARTIFACT_FORMAT
        or "human" not in stages
        or result.get("funnel_metrics", {}).get("human", {}).get("status") != "fitted"
    ):
        raise ValueError("Frozen trainer did not fit and export a servable human stage")
    return {"checksum": result["checksum"], "runtime": runtime, "stages": stages}


def training_rows():
    """Enough independent, two-class history that the human stage really fits and exports."""
    at = datetime(2025, 1, 1, tzinfo=UTC)
    return [
        {
            "lead_id": str(i),
            "endpoint_id": f"e{i}",
            "at": (at + timedelta(hours=6 * i)).isoformat(),
            "outcome_available_at": (at + timedelta(hours=6 * i, minutes=1)).isoformat(),
            "human": int((i % 8 < 4) != (i % 11 == 0)),
            "features": {"local_hour": 9 + i % 8, "attempt_count": i % 3, "number_health": (i % 5) / 4},
        }
        for i in range(400)
    ]


def train_child(command, directory, environment, authority_pid):
    output = directory / "training"
    output.mkdir(mode=0o700)
    process = start_owned(  # noqa: S603 - selected verified trainer, fixed training protocol
        command,
        cwd=directory,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        contents, _ = process.communicate(
            json.dumps({"rows": training_rows(), "output_dir": str(output)}).encode(), timeout=300
        )
        if process.returncode or len(contents) > 2 * 1024 * 1024:
            raise RuntimeError("Packaged training child failed")
        return check_training_reply(contents, output, authority_pid)
    finally:
        stop_owned(process)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    binary = Path(
        os.environ.get(
            "OPENDIALER_TEST_BINARY",
            "apps/desktop/src-tauri/binaries/opendialer-backend-aarch64-apple-darwin",
        )
    ).resolve(strict=True)
    trainer = trainer_for(binary, os.environ.get("OPENDIALER_TEST_TRAINER"))
    with (
        tempfile.TemporaryDirectory(prefix="opendialer-frozen-proof-") as temporary,
        tempfile.TemporaryDirectory(prefix="opendialer-frozen-log-") as logs,
    ):
        directory, log = Path(temporary), Path(logs) / "authority.log"
        log_file = log.open("ab")  # Startup diagnostics only; secrets arrive on stdin and are never logged.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        nonce = secrets.token_urlsafe(32)
        environment = isolated_environment(os.environ)
        for name in ("OPENDIALER_LOCAL_TOKEN", "OPENDIALER_SECRET_KEY"):
            environment.pop(name, None)
        secrets_line = (
            json.dumps({"local_token": nonce, "secret_key": secrets.token_urlsafe(48)}) + "\n"
        ).encode()
        command = [
            str(binary),
            "serve",
            "--secrets-stdin",
            "--simulation",
            "--mode",
            "local",
            "--port",
            str(port),
            "--data-dir",
            temporary,
        ]
        process = None
        try:
            process = start_authority(  # noqa: S603 - selected binary, isolated data and fixed arguments
                command,
                secrets_line,
                env=environment,
                cwd=directory,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            started = time.monotonic()
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
                await_ready(client, args.expected_version, process=process, log=log)
                boot_ms = (time.monotonic() - started) * 1000
                token = request(
                    client,
                    {"X-Local-Token": nonce},
                    "POST",
                    "/v1/setup",
                    json={
                        "seller_name": "Frozen Acceptance",
                        "campaign_name": "Frozen journey",
                        "email": "frozen@example.test",
                        "password": "Disposable-frozen-test-password",
                    },
                )
                headers = {"Authorization": "Bearer " + token["access_token"]}
                seed = request(client, headers, "POST", "/v1/simulation/seed")

                def pause(seconds):
                    for _ in range(seconds // 30):
                        time.sleep(30)
                        request(client, headers, "POST", "/v1/rep/heartbeat", json={"activity": "call"})

                journey = call_journey(client, headers, seed["campaign_id"], pause=pause)
                training = train_child(
                    [str(trainer)], directory, isolated_environment(os.environ), process.pid
                )
                stop_owned(process)
                process = start_authority(  # noqa: S603 - restart same verified binary/data, never reinstall
                    command,
                    secrets_line,
                    env=environment,
                    cwd=directory,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                await_ready(client, args.expected_version, process=process, log=log)
                retained = check_persisted(client, headers, journey)
                for store in directory.glob("opendialer.db*"):  # include -wal/-shm, not only the main file
                    assert b"frozen@example.test" not in store.read_bytes(), store.name
            stop_owned(process)
            process = None
            receipt = {
                "version": args.expected_version,
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "trainer_sha256": hashlib.sha256(trainer.read_bytes()).hexdigest(),
                "startup_ms": boot_ms,
                "journey": journey,
                "restart": retained,
                "training": training,
                "source_runtime_on_path": False,
                "external_calls": False,
            }
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(receipt, indent=2) + "\n")
            print(json.dumps(receipt), flush=True)
        finally:
            if process is not None:
                stop_owned(process)
            log_file.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
