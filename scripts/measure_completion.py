"""Disposable, synthetic completion measurements; no installed app or paid providers.

The source workload and native resource proof have separate receipts. Run profiles
one at a time on idle hardware. Progress JSON retains failures and interrupted work.
"""

import argparse
import asyncio
import contextvars
import hashlib
import hmac
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
WARMUPS, SAMPLES, COACH_RUNS = 20, 200, 30
PROFILES = {
    "historical": {"leads": 503, "attempts": 0, "reps": 1},
    "local": {"leads": 10_000, "attempts": 100_000, "reps": 1, "load_seconds": 1800},
    "shared": {"leads": 10_000, "attempts": 100_000, "reps": 8, "load_seconds": 1800},
    "saturation": {"leads": 10_000, "attempts": 100_000, "reps": 20, "load_seconds": 300},
    "scaling": {"leads": 100_000, "attempts": 1_000_000, "reps": 1},
    "shift": {"leads": 503, "attempts": 0, "reps": 1, "simulated_hours": 8},
}
CURRENT = contextvars.ContextVar("measurement", default=None)


def validate_output(path):
    path = path.expanduser().resolve()
    text = str(path).lower()
    if any(s in text for s in ("application support", "appdata", "/applications/", "com.opendialer")):
        raise ValueError("Refusing installed application output or data path")
    if path == ROOT or path.is_relative_to(ROOT / "services") or path.is_relative_to(ROOT / "apps"):
        raise ValueError("Refusing source directory for disposable measurement output")
    return path


def profile_database(name, directory):
    if name in {"shared", "saturation"}:
        from scripts.disposable_database import validate_database_url

        value = os.getenv("OPENDIALER_PERFORMANCE_DATABASE_URL")
        if not value:
            raise ValueError("OPENDIALER_PERFORMANCE_DATABASE_URL is required for shared profiles")
        return validate_database_url(value, purpose="performance")
    return "sqlite:///" + str(directory / "measurement.db")


def summarize(rows, *, target_ms):
    if not rows or any(not math.isfinite(r["ms"]) or r["ms"] < 0 for r in rows):
        raise ValueError("Missing or invalid measurement samples")
    times = sorted(r["ms"] for r in rows)
    failures = sum(r.get("ok") is not True for r in rows)

    def percentile(q):
        return times[max(0, math.ceil(q * len(times)) - 1)]

    return {
        "samples": len(rows),
        "failures": failures,
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": max(times),
        "sql_max": max(r.get("sql", 0) for r in rows),
        "decrypted_max": max(r.get("decrypted", 0) for r in rows),
        "target_ms": target_ms,
        "status": "passed" if not failures and percentile(0.95) < target_ms else "failed",
    }


def resource_gates(receipt):
    missing = [
        name
        for name in (
            "frozen_native_processes",
            "binary_sha256",
            "source_hash",
            "idle_seconds",
            "desktop_rss_mib",
        )
        if not receipt.get(name)
    ]
    failures = []
    for field, ceiling in (("authority_rss_mib", 200), ("desktop_rss_mib", 500), ("idle_cpu_percent", 2)):
        value = receipt.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            missing.append(field)
        elif value >= ceiling:
            failures.append(field)
    if receipt.get("idle_seconds", 0) < 300:
        missing.append("five_minute_idle_sample")
    return {
        "status": "incomplete" if missing else "failed" if failures else "passed",
        "missing": missing,
        "failures": failures,
    }


class Clock:
    now = datetime(2026, 10, 14, 18, tzinfo=UTC)

    def __call__(self):
        return self.now


class Measurements:
    def __init__(self, service):
        self.service, self.writes, self.lock = service, [], threading.Lock()

    @contextmanager
    def instrument(self):
        from sqlalchemy import event

        decrypt, transaction = self.service.db.cipher.decrypt, self.service.db.transaction

        def query(*args):
            row = CURRENT.get()
            if row is not None:
                row["sql"] += 1

        def counted_decrypt(*args, **kwargs):
            row = CURRENT.get()
            if row is not None:
                row["decrypted"] += 1
            return decrypt(*args, **kwargs)

        @contextmanager
        def timed(write=True, *, session=None):
            begun = time.perf_counter()
            ok = False
            try:
                with transaction(write, session=session) as s:
                    yield s
                ok = True
            finally:
                if write and session is None:
                    with self.lock:
                        self.writes.append({"ms": (time.perf_counter() - begun) * 1000, "ok": ok})

        event.listen(self.service.db.engine, "before_cursor_execute", query)
        self.service.db.cipher.decrypt, self.service.db.transaction = counted_decrypt, timed
        try:
            yield self
        finally:
            self.service.db.cipher.decrypt, self.service.db.transaction = decrypt, transaction
            event.remove(self.service.db.engine, "before_cursor_execute", query)

    def sample(self, function):
        row = {"sql": 0, "decrypted": 0, "ok": False}
        token, begun = CURRENT.set(row), time.perf_counter()
        result = None
        try:
            result = function()
            row["ok"] = True
        except Exception as exc:
            row["error"] = type(exc).__name__
        finally:
            row["ms"] = (time.perf_counter() - begun) * 1000
            CURRENT.reset(token)
        return row, result


def source_metadata():
    from scripts.inspect_architecture import LOCKS, digest, source_files

    git = shutil.which("git")
    commit = subprocess.run(  # noqa: S603 - fixed read-only repository command
        [git, "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout.strip()
    return {
        "source_commit": commit,
        "source_hash": digest(source_files()),
        "locks": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in LOCKS},
        "platform": platform.platform(),
        "python": platform.python_version(),
        "logical_cpus": os.cpu_count(),
        "external_calls": False,
    }


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def populate(service, p, campaign, leads, attempts, seed):
    """Insert synthetic encrypted history in pages; use the real operational indexer.

    This bypasses event/job creation during fixture loading only. A real paginated
    rebuild is subsequently required; no aggregate totals are fabricated here.
    """
    from app.db import MODELS, get
    from app.operational_schema import sync_columns
    from sqlalchemy import func, insert, select

    with service.db.transaction(False) as s:
        originals = list(
            s.scalars(select(MODELS["leads"]).where(MODELS["leads"].seller_id == p["seller_id"]))
        )
        sample = dict(originals[0].data)
        endpoint = dict(get(s, "phone_endpoints", sample["endpoint_id"]).data)
        number_id = s.scalar(
            select(MODELS["number_inventory"].id).where(
                MODELS["number_inventory"].seller_id == p["seller_id"]
            )
        )
    ids = [r.id for r in originals]

    def identity(kind, i):
        return str(uuid5(NAMESPACE_URL, f"opendialer-measure:{seed}:{kind}:{i}"))

    def row(kind, i, payload, at):
        obj = MODELS[kind](
            id=identity(kind, i),
            tenant_id=p["tenant_id"],
            seller_id=p["seller_id"],
            external_key=f"measure:{i}",
            data=payload,
            created_at=at,
            version=1,
        )
        sync_columns(obj, service.db.identity_key)
        if kind == "phone_endpoints":
            obj.external_key = hmac.new(
                service.db.identity_key, payload["phone"].encode(), hashlib.sha256
            ).hexdigest()
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}

    for offset in range(0, leads - len(originals), 500):
        endpoints, people = [], []
        for i in range(offset, min(offset + 500, leads - len(originals))):
            phone = f"+1415{5600000 + i:07d}"
            eid, lid = identity("phone_endpoints", i), identity("leads", i)
            endpoints.append(row("phone_endpoints", i, {**endpoint, "phone": phone}, service.clock()))
            people.append(
                row(
                    "leads",
                    i,
                    {
                        **sample,
                        "phone": phone,
                        "endpoint_id": eid,
                        "campaign_id": campaign,
                        "name": f"Synthetic {i}",
                        "script_id": None,
                    },
                    service.clock(),
                )
            )
            ids.append(lid)
        with service.db.transaction() as s:
            s.execute(insert(MODELS["phone_endpoints"]), endpoints)
            s.execute(insert(MODELS["leads"]), people)
    for offset in range(0, attempts, 500):
        page = []
        for i in range(offset, min(offset + 500, attempts)):
            at = service.clock() - timedelta(days=30 + i % 60, minutes=i % 600)
            page.append(
                row(
                    "call_attempts",
                    i,
                    {
                        "lead_id": ids[i % len(ids)],
                        "campaign_id": campaign,
                        "rep_id": p["user_id"],
                        "number_id": number_id,
                        "state": "FINALIZED",
                        "started_at": at.isoformat(),
                        "submitted_at": at.isoformat(),
                        "ended_at": (at + timedelta(seconds=60)).isoformat(),
                        "outcome": "no_answer" if i % 5 else "right_party",
                        "outcome_source": "rep",
                        "outcome_available_at": (at + timedelta(seconds=60)).isoformat(),
                        "transcript_version": 0,
                    },
                    at,
                )
            )
        with service.db.transaction() as s:
            s.execute(insert(MODELS["call_attempts"]), page)
    with service.db.transaction(False) as s:
        return {
            k: s.scalar(
                select(func.count()).select_from(MODELS[k]).where(MODELS[k].seller_id == p["seller_id"])
            )
            for k in ("leads", "call_attempts")
        }


def populate_historical(client, headers, campaign):
    """Use the preserved 500-row public import plus the three seed leads."""
    from app.db import MODELS
    from sqlalchemy import func, select

    rows = [
        {
            "name": f"Load {i}",
            "phone": f"+1415{5600000 + i:07d}",
            "company": "Load fixture",
            "timezone": "America/Los_Angeles",
        }
        for i in range(500)
    ]
    response = client.post("/v1/leads/import", headers=headers, json={"campaign_id": campaign, "rows": rows})
    response.raise_for_status()
    assert response.json()["imported"] == 500
    with client.app.state.service.db.transaction(False) as s:
        return {
            kind: s.scalar(select(func.count()).select_from(MODELS[kind]))
            for kind in ("leads", "call_attempts")
        }


def rebuild(service, p, *, timeout=1200):
    from app.optimization.projections import rebuild_page

    begun, pages, result = time.perf_counter(), 0, {}
    while time.perf_counter() - begun < timeout:
        result = rebuild_page(service, p, start=pages == 0)
        pages += 1
        if result.get("published"):
            break
    return {
        "seconds": time.perf_counter() - begun,
        "pages": pages,
        "published": bool(result.get("published")),
        "status": "passed" if result.get("published") else "failed",
    }


async def coach_fixture(service, p):
    """Measure the real queue/admission runner with six 700-ms provider-work fixtures."""
    previous, samples = service.coach_turn, []
    try:
        for run in range(COACH_RUNS):
            completions = []

            async def simulated_provider(principal, call_id, completions=completions):
                await asyncio.sleep(0.7)
                completions.append(time.perf_counter())

            service.coach_turn = simulated_provider
            for i in range(6):
                service.jobs.enqueue(
                    "live_coach",
                    {"call_id": f"measure-{run}-{i}", "seller_id": p["seller_id"], "user_id": p["user_id"]},
                    f"measure-coach-{run}-{i}",
                    tenant_id=p["tenant_id"],
                )
            begun = time.perf_counter()
            result = await service.run_jobs(queues=["live_coach"])
            samples.append(
                {
                    "ms": (time.perf_counter() - begun) * 1000,
                    "ok": len(completions) == 6 and result["processed"] == 6,
                }
            )
    finally:
        service.coach_turn = previous
    return {
        "summary": summarize(samples, target_ms=1500),
        "samples": samples,
        "boundary": "real worker; six independent synthetic 700-ms provider jobs, not live inference",
    }


def run_requests(service, p, campaign, reps, measurement, checkpoint):
    from app.db import put

    principals = [p]
    with service.db.transaction() as s:
        for i in range(1, reps):
            user = put(
                s,
                "users",
                p["tenant_id"],
                p["seller_id"],
                {"role": "rep", "email": f"measure-{i}@example.test", "disabled": False},
                now=service.clock(),
            )
            principals.append({**p, "user_id": user.id, "role": "rep"})
    rows, warmups, cold = [], [], []

    def selection(who):
        service.presence(
            who,
            {
                "status": "ready",
                "audio_ready": True,
                "telephony_ready": True,
                "agent_call_control_id": "synthetic-" + who["user_id"],
            },
        )
        result = measurement.sample(lambda: service.reserve(who, campaign))[0]
        service.presence(who, {"status": "paused"})
        return {**result, "rep": principals.index(who)}

    with ThreadPoolExecutor(max_workers=reps) as pool:
        cold.extend(pool.map(selection, principals))
        for i in range(WARMUPS + SAMPLES):
            batch = list(pool.map(selection, principals))
            (warmups if i < WARMUPS else rows).extend(batch)
            if i % 20 == 0:
                checkpoint({"measured_decisions": len(rows), "warmups": len(warmups)})
    return {
        "cold": summarize(cold, target_ms=1000),
        "warm": summarize(rows, target_ms=250),
        "sql_budget_met": max(r["sql"] for r in rows) <= 30 if reps == 1 else None,
        "warmups": warmups,
        "samples": rows,
        "boundary": "complete authority selection and claim; presence before/after excluded; no carrier I/O",
    }


def run_profile(name, directory, seed, checkpoint):
    from app.config import Settings
    from app.main import create_app
    from fastapi.testclient import TestClient

    from scripts.disposable_database import reset_database

    spec, clock = PROFILES[name], Clock()
    database = profile_database(name, directory)
    if name in {"shared", "saturation"}:
        reset_database(database, purpose="performance")
    settings = Settings(
        database_url=database,
        data_dir=directory,
        simulation=True,
        worker_enabled=False,
        secret_key="synthetic-measurement-" + "a" * 48,
        local_token="synthetic-measurement-token",  # noqa: S106 - isolated fixture identity
        mode="local",
    )
    begun = time.perf_counter()
    app = create_app(settings, clock=clock)
    result = {"profile": name, "declared": spec, "status": "running"}
    try:
        with TestClient(app) as client:
            result["authority_startup_ms"] = (time.perf_counter() - begun) * 1000
            response = client.post(
                "/v1/setup",
                headers={"X-Local-Token": settings.local_token},
                json={
                    "seller_name": "Synthetic measurement",
                    "campaign_name": "Synthetic measurement",
                    "email": "measure@example.test",
                    "password": "Synthetic-measurement-only-password",
                },
            )
            response.raise_for_status()
            setup = response.json()
            headers = {"Authorization": "Bearer " + setup["access_token"]}
            seeded = client.post("/v1/simulation/seed", headers=headers)
            seeded.raise_for_status()
            campaign = seeded.json()["campaign_id"]
            p = {k: setup[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}
            service = app.state.service
            result["actual_dataset"] = (
                populate_historical(client, headers, campaign)
                if name == "historical"
                else populate(service, p, campaign, spec["leads"], spec["attempts"], seed)
            )
            checkpoint({"loaded": result["actual_dataset"]})
            result["rebuild"] = rebuild(service, p)
            checkpoint({"rebuild": result["rebuild"]})
            service.rebuild_home_metrics(p)
            service.rebuild_optimization_report(p)
            measured = Measurements(service)
            with measured.instrument():
                result["selection"] = run_requests(service, p, campaign, spec["reps"], measured, checkpoint)
                reports = [measured.sample(lambda: service.snapshot(p))[0] for _ in range(WARMUPS + SAMPLES)]
                result["cached_workspace"] = {
                    "summary": summarize(reports[WARMUPS:], target_ms=250),
                    "samples": reports[WARMUPS:],
                }
                result["six_coach"] = client.portal.call(coach_fixture, service, p)
            result["writer_transactions"] = summarize(measured.writes, target_ms=25)
            result["training_imported"] = any(m in sys.modules for m in ("numpy", "scipy", "sklearn"))
            result["integrity"] = service.db.integrity()
    finally:
        app.state.db.engine.dispose()
    # Long-load, shift and frozen native evidence are explicit independent gates.
    # A hot-path receipt cannot assert those durations from a shorter run.
    result["missing"] = [k for k in ("load_seconds", "simulated_hours") if k in spec]
    if name in {"shared", "saturation"}:
        result["missing"].append("2vcpu_4gib_reference")
    checks = [
        result["selection"]["warm"]["status"],
        result["cached_workspace"]["summary"]["status"],
        result["six_coach"]["summary"]["status"],
        result["writer_transactions"]["status"],
        result["rebuild"]["status"],
    ]
    result["status"] = (
        "failed"
        if "failed" in checks or result["training_imported"]
        else "incomplete"
        if result["missing"]
        else "passed"
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=[*PROFILES, "all"], required=True)
    parser.add_argument("--seed", type=int, default=20261018)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        output = validate_output(args.output_dir)
    except ValueError as exc:
        parser.error(str(exc))
    output.mkdir(parents=True, exist_ok=True)
    metadata = source_metadata()
    summary = {
        **metadata,
        "seed": args.seed,
        "profiles": {},
        "resource_gates": resource_gates({}),
        "status": "running",
    }
    for name in PROFILES if args.profile == "all" else [args.profile]:

        def checkpoint(progress, name=name):
            save(
                output / f"{name}-progress.json",
                {**metadata, "profile": name, "at": datetime.now(UTC).isoformat(), **progress},
            )

        with tempfile.TemporaryDirectory(prefix="opendialer-measure-", dir=output) as temporary:
            try:
                profile = run_profile(name, Path(temporary), args.seed, checkpoint)
            except Exception as exc:
                profile = {"profile": name, "status": "failed", "error_type": type(exc).__name__}
            save(output / f"{name}.json", {**metadata, **profile})
            summary["profiles"][name] = profile["status"]
            save(output / "summary.json", summary)
            print(json.dumps({"profile": name, "status": profile["status"]}), flush=True)
    summary["status"] = "failed" if "failed" in summary["profiles"].values() else "incomplete"
    save(output / "summary.json", summary)
    return 1


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "services/backend"))
    raise SystemExit(main())
