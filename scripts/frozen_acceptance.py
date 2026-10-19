"""Exercise a packaged sidecar from a fresh directory with no source/Python runtime dependency."""

import argparse
import os
import secrets
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--expected-version", required=True)
args = parser.parse_args()

binary = Path(
    os.environ.get(
        "OPENDIALER_TEST_BINARY", "apps/desktop/src-tauri/binaries/opendialer-backend-aarch64-apple-darwin"
    )
).resolve()
with tempfile.TemporaryDirectory(prefix="opendialer-frozen-proof-") as directory:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    nonce = secrets.token_urlsafe(32)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENDIALER_") and k != "PYTHONPATH"}
    env.update(
        OPENDIALER_LOCAL_TOKEN=nonce,
        OPENDIALER_SECRET_KEY=secrets.token_urlsafe(48),
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
    )
    proc = subprocess.Popen(  # noqa: S603 - explicitly selected local packaged executable and fixed safe arguments
        [
            str(binary),
            "serve",
            "--simulation",
            "--mode",
            "local",
            "--port",
            str(port),
            "--data-dir",
            directory,
        ],
        env=env,
        cwd=directory,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as c:
            for _attempt in range(160):
                try:
                    health = c.get("/health")
                    health.raise_for_status()
                    break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Frozen backend failed to boot")
            assert health.json()["version"] == args.expected_version
            session = c.post(
                "/v1/setup",
                headers={"X-Local-Token": nonce},
                json={
                    "seller_name": "Frozen Acceptance",
                    "campaign_name": "Frozen smoke",
                    "email": "frozen@example.test",
                    "password": "Disposable-frozen-test-password",
                },
            )
            session.raise_for_status()
            token = session.json()
            h = {"Authorization": "Bearer " + token["access_token"]}
            seed = c.post("/v1/simulation/seed", headers=h)
            seed.raise_for_status()
            c.post(
                "/v1/rep/presence",
                headers=h,
                json={
                    "status": "ready",
                    "audio_ready": True,
                    "telephony_ready": True,
                    "agent_call_control_id": "agent-frozen",
                },
            ).raise_for_status()
            reservation = c.post(
                "/v1/queue/next", headers=h, json={"campaign_id": seed.json()["campaign_id"]}
            )
            reservation.raise_for_status()
            call = c.post(
                "/v1/calls/start",
                headers=h,
                json={
                    "reservation_id": reservation.json()["reservation_id"],
                    "idempotency_key": "frozen-smoke",
                },
            )
            call.raise_for_status()
            cid = call.json()["id"]
            turn = c.post(
                f"/v1/calls/{cid}/transcript",
                headers=h,
                json={
                    "segment_id": "frozen-turn",
                    "revision": 1,
                    "side": "remote",
                    "text": "We cannot afford another subscription.",
                    "is_final": True,
                },
            )
            turn.raise_for_status()
            detailed = c.get(f"/v1/calls/{cid}", headers=h)
            detailed.raise_for_status()
            card = detailed.json()["coach"]
            assert card["playbook_card_ids"] == ["budget"]
            c.post(
                f"/v1/calls/{cid}/coach/feedback",
                headers=h,
                json={"coach_event_id": card["id"], "useful": True, "render_ms": 12},
            ).raise_for_status()
            c.post(f"/v1/calls/{cid}/hangup", headers=h).raise_for_status()
            c.post(
                f"/v1/calls/{cid}/disposition", headers=h, json={"outcome": "qualified_conversation"}
            ).raise_for_status()
            lead_id = call.json()["lead_id"]
            event = c.post(
                f"/v1/leads/{lead_id}/conversions",
                headers=h,
                json={
                    "event_id": "frozen-meeting",
                    "call_id": cid,
                    "kind": "meeting_held",
                    "occurred_at": "2026-10-14T18:00:00+00:00",
                    "source_reference": "Synthetic packaged-runtime evidence",
                },
            )
            event.raise_for_status()
            c.post(
                f"/v1/leads/{lead_id}/conversions/{event.json()['id']}/retract",
                headers=h,
                json={"reason": "Synthetic correction"},
            ).raise_for_status()
            c.post("/v1/admin/optimization/refresh", headers=h).raise_for_status()
            report = c.get("/v1/optimization", headers=h)
            report.raise_for_status()
            assert report.json()["dataset"]["rows"] == 1 and report.json()["coaching"]["reviewed_cards"] == 1
            assert not report.json()["trial"]["sufficient_evidence"]
            c.post(
                "/v1/admin/optimization/comparisons",
                headers=h,
                json={"family": "coaching", "review_reference": "Synthetic frozen acceptance"},
            ).raise_for_status()
            c.put(
                "/v1/admin/optimization", headers=h, json={"conversion_goal": "sale_won"}
            ).raise_for_status()
            training = c.post("/v1/admin/optimization/train", headers=h, json={})
            training.raise_for_status()
            assert training.json()["status"] == "insufficient_data"
            check = c.get("/v1/admin/preflight", headers=h).json()
            assert check["integrity_ok"]
            assert b"frozen@example.test" not in (Path(directory) / "opendialer.db").read_bytes()
            print(
                f"PASS: frozen {args.expected_version} boot/setup/encryption/calling/grounded coaching/feedback/conversion correction/learning/comparisons/preflight; no source checkout or Python installation used by the packaged runtime."
            )
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
