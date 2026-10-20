"""Prove a packaged sidecar in a disposable directory with no developer runtime on PATH."""

import argparse
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx


def isolated_environment(source):
    allowed = {"SystemRoot", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
    return {
        **{k: v for k, v in source.items() if k in allowed},
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


def await_ready(client, expected_version, *, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
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
    raise TimeoutError("Frozen authority did not become ready")


def stop_owned(process, *, platform_name=os.name, system_root=None):
    if process.poll() is not None:
        return
    if platform_name == "nt":
        tool = Path(system_root or os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
        subprocess.run(  # noqa: S603 - absolute OS utility; exact owned PID, never a process-name kill
            [str(tool), "/PID", str(process.pid), "/T", "/F"], check=True, capture_output=True, timeout=20
        )
    else:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
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


def check_training_reply(contents, output_dir, authority_pid):
    result = json.loads(contents)
    artifact = output_dir / "challenger.joblib"
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
    return {"checksum": result["checksum"], "runtime": runtime}


def train_child(binary, directory, environment, authority_pid):
    output = directory / "training"
    output.mkdir(mode=0o700)
    at = datetime(2025, 1, 1, tzinfo=UTC)
    rows = [
        {
            "lead_id": str(i),
            "endpoint_id": f"e{i}",
            "at": (at + timedelta(days=i)).isoformat(),
            "outcome_available_at": (at + timedelta(days=i, minutes=1)).isoformat(),
            "human": int(i % 3 != 0),
            "features": {"local_hour": 9 + i % 8},
        }
        for i in range(120)
    ]
    process = subprocess.Popen(  # noqa: S603 - selected verified binary, fixed training entrypoint
        [str(binary), "--train-child"],
        cwd=directory,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        contents, _ = process.communicate(
            json.dumps({"rows": rows, "output_dir": str(output)}).encode(), timeout=300
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
    with tempfile.TemporaryDirectory(prefix="opendialer-frozen-proof-") as temporary:
        directory = Path(temporary)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        nonce = secrets.token_urlsafe(32)
        environment = isolated_environment(os.environ)
        environment.update(OPENDIALER_LOCAL_TOKEN=nonce, OPENDIALER_SECRET_KEY=secrets.token_urlsafe(48))
        command = [
            str(binary),
            "serve",
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
            process = subprocess.Popen(  # noqa: S603 - selected binary, isolated data and fixed arguments
                command,
                env=environment,
                cwd=directory,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            started = time.monotonic()
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
                await_ready(client, args.expected_version)
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
                training = train_child(binary, directory, isolated_environment(os.environ), process.pid)
                stop_owned(process)
                process = subprocess.Popen(  # noqa: S603 - restart same verified binary/data, never reinstall
                    command,
                    env=environment,
                    cwd=directory,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                await_ready(client, args.expected_version)
                retained = check_persisted(client, headers, journey)
                assert b"frozen@example.test" not in (directory / "opendialer.db").read_bytes()
            receipt = {
                "version": args.expected_version,
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
