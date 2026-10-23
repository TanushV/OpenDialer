"""Local speed bench: hot calling paths on a populated store (simulated carrier, no paid calls).

    .venv/bin/python scripts/bench.py [--leads 10000] [--attempts 100000] [--samples 40] [--out FILE]

Builds a fresh store in a temporary directory, imports leads over HTTP, inserts call history
directly (fixture loading only), then times each hot path through the HTTP API.
"""

import argparse
import base64
import json
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "services" / "backend")]

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event, func, insert, select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.providers.simulated import SimulatedCarrier  # noqa: E402

TARGETS_MS = {"next_lead": 100, "webhook_receipt": 30, "event_to_state": 100, "wrap_up": 100, "workspace": 150}


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC).replace(hour=18, minute=0, second=0, microsecond=0)

    def __call__(self):
        return self.now


def settings(directory):
    return Settings(
        database_url=f"sqlite:///{directory}/bench.db",
        mode="local",
        local_token="bench-local-token",
        simulation=True,
        data_dir=directory,
        worker_enabled=False,
        bootstrap_token="bench-bootstrap-token",
        secret_key="bench-secret-" + "b" * 48,
    )


def history(service, p, campaign, attempts, now):
    """Insert finalized attempts against the imported leads, in pages."""
    from app.db import MODELS
    from app.operational_schema import sync_columns

    model = MODELS["call_attempts"]
    with service.db.transaction(False) as s:
        leads = list(s.scalars(select(MODELS["leads"].id).where(MODELS["leads"].seller_id == p["seller_id"])))
        number = s.scalar(select(MODELS["number_inventory"].id))
    for offset in range(0, attempts, 1000):
        page = []
        for i in range(offset, min(offset + 1000, attempts)):
            at = now - timedelta(days=30 + i % 60, minutes=i % 600)
            row = model(
                id=str(uuid4()),
                tenant_id=p["tenant_id"],
                seller_id=p["seller_id"],
                external_key=f"bench:{i}",
                version=1,
                created_at=at,
                data={
                    "lead_id": leads[i % len(leads)],
                    "campaign_id": campaign,
                    "rep_id": p["user_id"],
                    "number_id": number,
                    "state": "FINALIZED",
                    "started_at": at.isoformat(),
                    "submitted_at": at.isoformat(),
                    "ended_at": (at + timedelta(seconds=60)).isoformat(),
                    "outcome": "no_answer" if i % 5 else "right_party",
                    "outcome_source": "rep",
                    "outcome_available_at": (at + timedelta(seconds=60)).isoformat(),
                    "transcript_version": 0,
                },
            )
            sync_columns(row, getattr(service.db, "identity_key", None))
            page.append({k: v for k, v in vars(row).items() if not k.startswith("_")})
        with service.db.transaction() as s:
            s.execute(insert(model), page)
    with service.db.transaction(False) as s:
        return s.scalar(select(func.count()).select_from(model))


def percentile(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def run(leads, attempts, samples):
    directory = Path(tempfile.mkdtemp(prefix="opendialer-bench-"))
    clock, carrier = Clock(), SimulatedCarrier()
    app = create_app(settings(directory), clock=clock, carrier=carrier)
    client = TestClient(app)
    client.__enter__()
    r = client.post(
        "/v1/setup",
        headers={"X-Local-Token": "bench-local-token"},
        json={
            "seller_name": "Bench Seller",
            "campaign_name": "Bench",
            "email": "bench@example.test",
            "password": "bench-only-password-123",
        },
    )
    r.raise_for_status()
    creds = r.json()
    h = {"Authorization": f"Bearer {creds['access_token']}"}
    campaign = client.post("/v1/simulation/seed", headers=h).raise_for_status().json()["campaign_id"]
    for offset in range(0, leads, 500):
        rows = [
            {
                "name": f"Bench {i}",
                "phone": f"+1415{5600000 + i:07d}",
                "company": f"Bench Co {i}",
                "timezone": "America/Los_Angeles",
                "timezone_confidence": "confirmed",
                "region": "415",
            }
            for i in range(offset, min(offset + 500, leads))
        ]
        client.post("/v1/leads/import", headers=h, json={"campaign_id": campaign, "rows": rows}).raise_for_status()
    p = {"tenant_id": creds["tenant_id"], "seller_id": creds["seller_id"], "user_id": creds["user_id"]}
    loaded = history(app.state.service, p, campaign, attempts, clock.now)
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    client.put("/v1/admin/settings", headers=h, json={"telnyx_public_key": public}).raise_for_status()

    statements = []
    event.listen(app.state.db.engine, "before_cursor_execute", lambda *a, **k: statements.append(1))
    timings = {name: [] for name in TARGETS_MS}
    sql_next = []

    def timed(name, method, path, **kwargs):
        started = time.perf_counter()
        response = client.request(method, path, **kwargs)
        timings[name].append((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            raise RuntimeError(f"{name}: {response.status_code} {response.text}")
        return response.json()

    for i in range(samples):
        client.post(
            "/v1/rep/presence",
            headers=h,
            json={"status": "ready", "audio_ready": True, "telephony_ready": True, "agent_call_control_id": "a"},
        ).raise_for_status()
        statements.clear()
        packet = timed("next_lead", "POST", "/v1/queue/next", headers=h, json={"campaign_id": campaign})
        sql_next.append(len(statements))
        call = client.post(
            "/v1/calls/start",
            headers=h,
            json={"reservation_id": packet["reservation_id"], "idempotency_key": f"bench-{i}"},
        ).raise_for_status().json()
        provider = next(reversed(carrier.calls.values()))["call_control_id"]
        body = json.dumps(
            {"data": {"id": str(uuid4()), "event_type": "call.ringing", "payload": {"call_control_id": provider}}}
        ).encode()
        stamp = str(int(clock.now.timestamp()))
        signature = base64.b64encode(private.sign(stamp.encode() + b"|" + body)).decode()
        timed(
            "webhook_receipt",
            "POST",
            "/webhooks/telnyx",
            content=body,
            headers={"telnyx-signature-ed25519": signature, "telnyx-timestamp": stamp, "content-type": "application/json"},
        )
        for kind in ("call.answered", "human.confirmed", "call.hangup"):
            timed(
                "event_to_state",
                "POST",
                "/v1/simulation/event",
                headers=h,
                json={"event_id": str(uuid4()), "event_type": kind, "call_id": call["id"], "payload": {}},
            )
        version = client.get(f"/v1/calls/{call['id']}", headers=h).raise_for_status().json()["version"]
        timed(
            "wrap_up",
            "POST",
            f"/v1/calls/{call['id']}/disposition",
            headers=h,
            json={"request_id": str(uuid4()), "expected_version": version, "outcome": "not_interested"},
        )
        timed("workspace", "GET", "/v1/workspace", headers=h)
        clock.now += timedelta(seconds=90)
    client.__exit__(None, None, None)
    app.state.db.engine.dispose()
    started = time.perf_counter()
    reopened = create_app(settings(directory), clock=clock, carrier=SimulatedCarrier())
    startup_ms = (time.perf_counter() - started) * 1000
    reopened.state.db.engine.dispose()
    paths = {
        name: {
            "p50_ms": round(statistics.median(values), 1),
            "p95_ms": round(percentile(values, 0.95), 1),
            "target_p95_ms": TARGETS_MS[name],
            "pass": percentile(values, 0.95) <= TARGETS_MS[name],
        }
        for name, values in timings.items()
    }
    paths["next_lead"]["sql_statements_max"] = max(sql_next)
    return {
        "leads": leads,
        "attempts": loaded,
        "samples": samples,
        "paths": paths,
        "startup_ms": round(startup_ms),
        "store_bytes": (directory / "bench.db").stat().st_size,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--leads", type=int, default=10000)
    parser.add_argument("--attempts", type=int, default=100000)
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    result = run(args.leads, args.attempts, args.samples)
    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
