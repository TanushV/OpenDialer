"""Disposable, synthetic completion measurements; no installed app or paid providers.

The source workload and native resource proof have separate receipts. Run profiles
one at a time on idle hardware. Progress JSON retains failures and interrupted work.
The load phase runs the production runtime lanes on a business clock that follows
real elapsed time, and gates each rep-visible path. Exit status 0 means every
requested profile passed (and the native receipt, when required).
"""

import argparse
import asyncio
import base64
import contextvars
import hashlib
import hmac
import inspect
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

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
# Rep-visible request boundaries under load. Start includes the simulated dial;
# cleanup and pause are retained and reported, not gated.
LOAD_TARGETS_MS = {
    "selection": 250,
    "event_ack": 100,
    "event_state": 250,
    "live_context": 100,
    "hangup": 250,
    "wrapup": 250,
}
LOAD_REPORTED = ("start", "cleanup_hangup", "pause")
RECOVERY_SECONDS = 120
LANE_SETTLE_SECONDS = 10
MAINTENANCE_LANES = {"maintenance", "call_control"}
# Writer attribution skips transaction and executor plumbing; the first frame is the caller.
SKIPPED_MODULES = {"contextlib", "threading", "app.db"}
SKIPPED_PREFIXES = ("concurrent.futures", "asyncio")
LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]{0,79}")


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


def percentile(times, q):
    """Nearest-rank percentile of already sorted values."""
    return times[max(0, math.ceil(q * len(times)) - 1)]


def summarize(rows, *, target_ms):
    if not rows or any(not math.isfinite(r["ms"]) or r["ms"] < 0 for r in rows):
        raise ValueError("Missing or invalid measurement samples")
    times = sorted(r["ms"] for r in rows)
    failures = sum(r.get("ok") is not True for r in rows)
    return {
        "samples": len(rows),
        "failures": failures,
        "p50_ms": percentile(times, 0.50),
        "p95_ms": percentile(times, 0.95),
        "p99_ms": percentile(times, 0.99),
        "max_ms": max(times),
        "sql_max": max(r.get("sql", 0) for r in rows),
        "decrypted_max": max(r.get("decrypted", 0) for r in rows),
        "target_ms": target_ms,
        "status": "passed" if not failures and percentile(times, 0.95) < target_ms else "failed",
    }


def reported(rows):
    """The same distribution without a release target."""
    return {**summarize(rows, target_ms=math.inf), "target_ms": None, "status": "reported"}


def run_for(seconds, operation, *, monotonic=time.monotonic, pause=time.sleep, cadence=10):
    """Real elapsed load; failures stay in the sequence, never shortened to a count."""
    if seconds <= 0 or cadence <= 0:
        raise ValueError("Positive duration and cadence required")
    begun, samples = monotonic(), []
    while monotonic() - begun < seconds:
        at = monotonic()
        try:
            result = operation(len(samples))
        except Exception as exc:
            result = {"ok": False, "error": type(exc).__name__}
        samples.append({**result, "ms": (monotonic() - at) * 1000})
        remaining = min(seconds - (monotonic() - begun), cadence - (monotonic() - at))
        if remaining > 0:
            pause(remaining)
    return {
        "requested_seconds": seconds,
        "elapsed_seconds": monotonic() - begun,
        "samples": samples,
        "status": "passed" if all(r.get("ok") for r in samples) else "failed",
    }


def profile_gates(result, spec):
    missing, failures = [], []
    checks = {
        "cold_selection": result.get("selection", {}).get("cold", {}).get("status"),
        "warm_selection": result.get("selection", {}).get("warm", {}).get("status"),
        "workspace": result.get("cached_workspace", {}).get("summary", {}).get("status"),
        "six_coach": result.get("six_coach", {}).get("summary", {}).get("status"),
        "writer": result.get("writer_transactions", {}).get("status"),
        "rebuild": result.get("rebuild", {}).get("status"),
        "control_paths": result.get("control_paths", {}).get("status"),
        "live_admission": result.get("live_admission", {}).get("status"),
    }
    if spec.get("reps", 1) > 1:
        checks["stalled_selection"] = result.get("stalled_selection", {}).get("warm", {}).get("status")
        checks["2vcpu_4gib_reference"] = result.get("shared_reference", {}).get("status")
    if "simulated_hours" in spec:
        checks["shift_maintenance"] = result.get("shift", {}).get("maintenance", {}).get("status")
        checks["shift_queue_recovery"] = result.get("shift", {}).get("queue_recovery", {}).get("status")
    for key, status in checks.items():
        if status is None or status == "incomplete":
            missing.append(key)
        elif status != "passed":
            failures.append(key)
    if result.get("selection", {}).get("sql_budget_met") is False:
        failures.append("selection_sql")
    if result.get("training_imported") or not result.get("integrity"):
        failures.append("runtime_integrity_or_training_import")
    if result.get("authority_startup_ms", float("inf")) >= 5000:
        failures.append("authority_startup")
    for field, output in (("load_seconds", "load"), ("simulated_hours", "shift")):
        if field in spec:
            actual = result.get(output, {})
            if not actual or actual.get("elapsed_seconds", 0) < spec[field] * (
                3600 if output == "shift" else 1
            ):
                missing.append(output)
            elif actual.get("status") != "passed":
                failures.append(output)
    return {
        "status": "failed" if failures else "incomplete" if missing else "passed",
        "missing": missing,
        "failures": failures,
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
    if receipt.get("cpu_accounting_complete") is not True:
        missing.append("complete_owned_process_cpu")
    if not receipt.get("authority_process_count"):
        missing.append("owned_authority_process")
    return {
        "status": "incomplete" if missing else "failed" if failures else "passed",
        "missing": missing,
        "failures": failures,
    }


def summarize_native_resources(samples):
    """Count the app, authority and WebView descendants, without dividing by host CPUs."""
    if len(samples) < 2 or any(not sample["processes"] for sample in samples):
        raise ValueError("Native resource samples are missing")
    elapsed = samples[-1]["elapsed_seconds"] - samples[0]["elapsed_seconds"]
    if not math.isfinite(elapsed) or elapsed <= 0:
        raise ValueError("Native resource duration is invalid")
    identities, baseline, latest, previous = set(), {}, {}, set()
    complete = True
    for index, sample in enumerate(samples):
        current = set()
        for row in sample["processes"]:
            key = (row["pid"], row["create_time"])
            if key in current or any(
                not math.isfinite(row[k]) or row[k] < 0 for k in ("rss_mib", "cpu_seconds")
            ):
                raise ValueError("Invalid or duplicate native process measurement")
            current.add(key)
            identities.add(key)
            if index == 0:
                baseline[key] = row["cpu_seconds"]
            elif row["cpu_seconds"] < latest.get(key, 0):
                complete = False
            latest[key] = row["cpu_seconds"]
        if previous - current:
            # Sampling cannot recover CPU consumed after the last observation of
            # an exited process. Retain the samples but do not certify idle CPU.
            complete = False
        previous = current
    return {
        "idle_seconds": elapsed,
        "desktop_rss_mib": max(sum(p["rss_mib"] for p in s["processes"]) for s in samples),
        "authority_rss_mib": max(
            sum(p["rss_mib"] for p in s["processes"] if p["role"] == "authority") for s in samples
        ),
        "idle_cpu_percent": 100
        * sum(max(0, value - baseline.get(key, 0)) for key, value in latest.items())
        / elapsed,
        "cpu_accounting_complete": complete,
        "observed_process_identities": len(identities),
        "authority_process_count": len(
            {
                (row["pid"], row["create_time"])
                for sample in samples
                for row in sample["processes"]
                if row["role"] == "authority"
            }
        ),
        "samples": samples,
    }


def measure_native_resources(root_pid, application, backend, *, seconds=300, interval=5):
    """Read only an exact installed app and its owned descendants for five minutes."""
    import psutil

    application, backend = Path(application).resolve(strict=True), Path(backend).resolve(strict=True)
    root = psutil.Process(root_pid)
    birth = root.create_time()
    if Path(root.exe()).resolve() != application or seconds < 300 or not 0 < interval <= 30:
        raise ValueError("Exact native process and five-minute sample are required")
    source = source_metadata()
    binary_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (application, backend)}
    owned, samples, errors = {}, [], []
    began = time.monotonic()
    while True:
        if not root.is_running() or root.create_time() != birth or Path(root.exe()).resolve() != application:
            raise RuntimeError("Native root identity changed during measurement")
        for process in [root, *root.children(recursive=True)]:
            try:
                owned[(process.pid, process.create_time())] = process
            except psutil.NoSuchProcess:
                errors.append("child_disappeared_before_identity")
        rows = []
        for (pid, created), process in owned.items():
            try:
                if not process.is_running() or process.create_time() != created:
                    errors.append("owned_pid_reused")
                    continue
                executable = Path(process.exe()).resolve()
                times = process.cpu_times()
                rows.append(
                    {
                        "pid": pid,
                        "create_time": created,
                        "executable": executable.name,
                        "role": "desktop"
                        if executable == application
                        else "authority"
                        if executable == backend
                        else "webview",
                        "rss_mib": process.memory_info().rss / 1024**2,
                        "cpu_seconds": times.user + times.system,
                    }
                )
            except psutil.NoSuchProcess:
                errors.append("owned_process_exited")
        elapsed = time.monotonic() - began
        samples.append({"elapsed_seconds": elapsed, "processes": rows})
        if elapsed >= seconds:
            break
        time.sleep(min(interval, seconds - elapsed))
    receipt = {
        **source,
        **summarize_native_resources(samples),
        "binary_sha256": binary_hashes,
        "frozen_native_processes": sorted(
            {(row["pid"], row["create_time"], row["executable"]) for s in samples for row in s["processes"]}
        ),
        "process_errors": sorted(set(errors)),
        "boundary": "Actual native app and descendants; peak aggregate RSS; CPU percent of one core; no training during idle",
    }
    if errors:
        receipt["cpu_accounting_complete"] = False
    receipt["gates"] = resource_gates(receipt)
    return receipt


def container_cgroup(path, container_id):
    return any(part in {container_id, "docker-" + container_id + ".scope"} for part in Path(path).parts)


def validate_reference(manifest, cpu_max, memory_max, *, runtime, postgres):
    """Bind inspected limits to this process, network and actual PostgreSQL server."""
    try:
        api, database = manifest["api"], manifest["database"]
        quota, period = map(int, cpu_max.split())
        runtime_memory = int(memory_max)
        limits = [item["HostConfig"] for item in (api, database)]
        if any(
            item["Config"]["Labels"].get("opendialer.task") != "completion-E13"
            or len(item["Id"]) != 64
            or not item["Image"].startswith("sha256:")
            for item in (api, database)
        ):
            raise ValueError("Missing owned container identity")
        if any(
            cap["NanoCpus"] <= 0 or cap["Memory"] <= 0 or cap["MemorySwap"] != cap["Memory"] for cap in limits
        ):
            raise ValueError("Unbounded CPU, memory or swap")
        cpu, memory = sum(c["NanoCpus"] for c in limits) / 1e9, sum(c["Memory"] for c in limits)
        if (
            cpu > 2
            or memory > 4 * 1024**3
            or api["Id"] == database["Id"]
            or limits[0]["NetworkMode"] != "container:" + database["Id"]
            or limits[0]["PidMode"] != "container:" + database["Id"]
            or period <= 0
            or quota / period != limits[0]["NanoCpus"] / 1e9
            or runtime_memory != limits[0]["Memory"]
            or runtime != manifest["runtime"]
            or not container_cgroup(runtime["cgroup_path"], api["Id"])
            or runtime["network_namespace"] != manifest["database_runtime"]["network_namespace"]
            or runtime["pid_namespace"] != manifest["database_runtime"]["pid_namespace"]
            or not runtime["cgroup_namespace"].startswith("cgroup:[")
            or runtime["memory_swap_max"] != "0"
            or {key: postgres[key] for key in ("system_identifier", "port", "database")}
            != manifest["postgres"]
            or postgres["port"] != 55439
            or postgres["database"] != "opendialer_completion_performance"
            or not str(postgres["system_identifier"]).isdigit()
        ):
            raise ValueError("Reference limits or executing namespace differ")
        db_quota, db_period = map(int, postgres["limits"]["cpu.max"].split())
        if (
            not container_cgroup(postgres["cgroup_path"], database["Id"])
            or db_period <= 0
            or db_quota / db_period != limits[1]["NanoCpus"] / 1e9
            or int(postgres["limits"]["memory.max"]) != limits[1]["Memory"]
            or postgres["limits"]["memory.swap.max"] != "0"
        ):
            raise ValueError("Connected database process or effective limits differ")
        return {
            "status": "passed",
            "cpu_limit": cpu,
            "memory_limit_mib": memory / 1024**2,
            "runtime_cpu_max": cpu_max.strip(),
            "runtime_memory_max": runtime_memory,
            "runtime_identity": runtime,
            "postgres_identity": postgres,
            "containers": manifest,
            "boundary": "Linux container CPU/memory limits on the recorded host; provider and native resources separate",
        }
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        return {"status": "failed", "error_type": type(exc).__name__}


def process_cgroup(pid):
    values = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    path = next(value[3:] for value in values if value.startswith("0::"))
    if not path.startswith("/") or ".." in Path(path).parts:
        raise ValueError("Invalid cgroup path")
    return path


def cgroup_limits(path):
    root = Path("/sys/fs/cgroup") / path.lstrip("/")
    return {name: (root / name).read_text().strip() for name in ("cpu.max", "memory.max", "memory.swap.max")}


def runtime_identity():
    import socket

    path = process_cgroup("self")
    return {
        "hostname": socket.gethostname(),
        "network_namespace": os.readlink("/proc/self/ns/net"),
        "cgroup_namespace": os.readlink("/proc/self/ns/cgroup"),
        "pid_namespace": os.readlink("/proc/self/ns/pid"),
        "cgroup_path": path,
        "memory_swap_max": cgroup_limits(path)["memory.swap.max"],
    }


def postgres_identity():
    from sqlalchemy import create_engine, text

    from scripts.disposable_database import validate_database_url

    url = validate_database_url(os.environ["OPENDIALER_PERFORMANCE_DATABASE_URL"], purpose="performance")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            system, port, database, pid = connection.execute(
                text(
                    "SELECT system_identifier::text, inet_server_port(), current_database(), pg_backend_pid() FROM pg_control_system()"
                )
            ).one()
            path = process_cgroup(int(pid))
            limits = cgroup_limits(path)
        return {
            "system_identifier": system,
            "port": port,
            "database": database,
            "cgroup_path": path,
            "limits": limits,
        }
    finally:
        engine.dispose()


def shared_reference():
    source = os.getenv("OPENDIALER_REFERENCE_MANIFEST")
    if not source:
        raise ValueError("An inspected reference manifest is required before shared measurement")
    runtime = runtime_identity()
    limits = cgroup_limits(runtime["cgroup_path"])
    result = validate_reference(
        json.loads(Path(source).read_text()),
        limits["cpu.max"],
        limits["memory.max"],
        runtime=runtime,
        postgres=postgres_identity(),
    )
    if result["status"] != "passed":
        raise ValueError("Inspected reference limits do not match the executing container")
    return result


class Clock:
    """Business time for the fixture: frozen by default, real elapsed time under load."""

    now = datetime(2026, 10, 14, 18, tzinfo=UTC)

    def __init__(self):
        self._lock, self._following = threading.Lock(), None

    def __call__(self):
        with self._lock:
            if self._following is None:
                return self.now
            monotonic, origin = self._following
            return self.now + timedelta(seconds=monotonic() - origin)

    @contextmanager
    def following(self, monotonic=time.monotonic):
        """clock() == now + real elapsed; leaving keeps the elapsed time in now."""
        with self._lock:
            if self._following is not None:
                raise RuntimeError("Clock already follows elapsed time")
            self._following = (monotonic, monotonic())
        try:
            yield self
        finally:
            with self._lock:
                monotonic, origin = self._following
                self.now = self.now + timedelta(seconds=monotonic() - origin)
                self._following = None


def caller_identity(frame, limit=3):
    """Up to three module:qualname frames above the transaction plumbing."""
    names = []
    try:
        while frame is not None and len(names) < limit:
            module = frame.f_globals.get("__name__", "")
            filename = frame.f_code.co_filename.replace("\\", "/")
            if not (
                module in SKIPPED_MODULES
                or module.startswith(SKIPPED_PREFIXES)
                or filename.endswith("/app/db.py")
            ):
                names.append(f"{module}:{frame.f_code.co_qualname}")
            frame = frame.f_back
    finally:
        del frame
    return " < ".join(names) or "unknown"


class WriterAdmission:
    """Wraps the SQLite writer lock and records each thread's admission wait."""

    def __init__(self, lock):
        self.lock, self.local = lock, threading.local()

    def acquire(self, *args, **kwargs):
        begun = time.perf_counter()
        try:
            return self.lock.acquire(*args, **kwargs)
        finally:
            self.local.wait_ms = (time.perf_counter() - begun) * 1000

    def release(self):
        return self.lock.release()


class Measurements:
    def __init__(self, service, *, stall_ms=0):
        self.service, self.writes, self.lock = service, [], threading.Lock()
        self.stall_ms = stall_ms
        self.phase = None  # Set by run_profile around each measured phase.

    @contextmanager
    def instrument(self):
        from sqlalchemy import event

        db = self.service.db
        decrypt, transaction = db.cipher.decrypt, db.transaction
        writer = db._sqlite_writer if getattr(db, "sqlite", False) else None
        admission = WriterAdmission(writer) if writer is not None else None

        def query(*args):
            row = CURRENT.get()
            if row is not None:
                row["sql"] += 1
                if self.stall_ms and "injected_stall_ms" not in row:
                    row["injected_stall_ms"] = self.stall_ms
                    time.sleep(self.stall_ms / 1000)

        def counted_decrypt(*args, **kwargs):
            row = CURRENT.get()
            if row is not None:
                row["decrypted"] += 1
            return decrypt(*args, **kwargs)

        @contextmanager
        def timed(write=True, *, session=None):
            recorded = write and session is None
            caller = caller_identity(inspect.currentframe().f_back) if recorded else None
            if recorded and admission is not None:
                admission.local.wait_ms = None
            begun = time.perf_counter()
            ok, error = False, None
            try:
                with transaction(write, session=session) as s:
                    yield s
                ok = True
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                if recorded:
                    row = {
                        "ms": (time.perf_counter() - begun) * 1000,
                        "ok": ok,
                        "caller": caller,
                        "phase": self.phase,
                        "admission_ms": admission.local.wait_ms if admission is not None else None,
                    }
                    if error:
                        row["error"] = error
                    with self.lock:
                        self.writes.append(row)

        event.listen(db.engine, "before_cursor_execute", query)
        db.cipher.decrypt, db.transaction = counted_decrypt, timed
        if admission is not None:
            db._sqlite_writer = admission
        try:
            yield self
        finally:
            if admission is not None:
                db._sqlite_writer = writer
            db.cipher.decrypt, db.transaction = decrypt, transaction
            event.remove(db.engine, "before_cursor_execute", query)

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


def summarize_writes(rows, *, target_ms=25):
    """Overall writer gate plus who wrote, in which phase, and how long admission took."""
    overall = summarize(rows, target_ms=target_ms)
    callers, phases = {}, {}
    for row in rows:
        callers.setdefault(row.get("caller") or "unknown", []).append(row)
        phases.setdefault(row.get("phase") or "unassigned", []).append(row)
    attribution = []
    for caller, items in callers.items():
        times = sorted(r["ms"] for r in items)
        waits = sorted(r["admission_ms"] for r in items if r.get("admission_ms") is not None)
        attribution.append(
            {
                "caller": caller,
                "count": len(items),
                "failures": sum(r.get("ok") is not True for r in items),
                "p50_ms": percentile(times, 0.50),
                "p95_ms": percentile(times, 0.95),
                "max_ms": times[-1],
                "total_ms": sum(times),
                "admission_p95_ms": percentile(waits, 0.95) if waits else None,
                "errors": dict(sorted(Counter(r["error"] for r in items if r.get("error")).items())),
            }
        )
    attribution.sort(key=lambda a: (-a["total_ms"], a["caller"]))
    waits = [{"ms": r["admission_ms"], "ok": True} for r in rows if r.get("admission_ms") is not None]
    return {
        **overall,
        "attribution": attribution,
        "phases": {name: summarize(items, target_ms=target_ms) for name, items in sorted(phases.items())},
        "admission": reported(waits) if waits else {"samples": 0, "status": "not_applicable"},
        "boundary": "every write transaction, background lanes included; admission is the SQLite writer-lock wait",
    }


def source_metadata():
    from scripts.completion_acceptance import TESTS
    from scripts.inspect_architecture import LOCKS, digest, source_files

    git = shutil.which("git")
    commit = subprocess.run(  # noqa: S603 - fixed read-only repository command
        [git, "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout.strip()
    dirty = subprocess.run(  # noqa: S603 - fixed read-only tracked status
        [git, "status", "--porcelain", "--untracked-files=no"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    tests = [
        p
        for root in TESTS
        for p in (ROOT / root).rglob("*")
        if p.is_file() and p.suffix in {".py", ".ts", ".tsx", ".json"} and "__pycache__" not in p.parts
    ]
    return {
        "source_commit": commit,
        "source_hash": digest(source_files()),
        "test_tree_digest": digest(tests),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "dirty_files": dirty,
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


async def admission_fixture(service, reps, *, runs=COACH_RUNS, delay_seconds=0.7):
    """Exercise real shared admission; report denied opportunities as well as accepted waits."""
    engine, rows = service.coach, []

    async def wave(owners, label):
        samples = []

        async def one(index, owner):
            began, entered = time.perf_counter(), None

            class Provider:
                async def suggest(self, request, **kwargs):
                    nonlocal entered
                    entered = time.perf_counter()
                    await asyncio.sleep(delay_seconds)
                    return {"action": "NO_ACTION"}

            reply = await engine.suggest(
                f"measure-admission-{label}-{index}",
                {"help_request_id": f"{label}-{index}"},
                Provider(),
                {"action": "NO_ACTION"},
                deadline_ms=1500,
                owner_key=owner,
            )
            end = time.perf_counter()
            accepted = entered is not None
            samples.append(
                {
                    "owner": owner,
                    "accepted": accepted,
                    "ms": (end - began) * 1000,
                    "queue_ms": ((entered if accepted else end) - began) * 1000,
                    "ok": accepted or reply.get("status") == "busy",
                    "status": reply.get("status", "completed"),
                }
            )

        await asyncio.gather(*(one(i, owner) for i, owner in enumerate(owners)))
        return samples

    for run in range(runs):
        rows.extend(await wave([f"rep-{i}" for i in range(reps)], str(run)))
    noisy = await wave(["noisy"] * 5 + [f"other-{i}" for i in range(8)], "noisy")
    accepted = [r for r in rows if r["accepted"]]
    queue = summarize([{"ms": r["queue_ms"], "ok": r["ok"]} for r in accepted], target_ms=100)
    noise_receipt = {
        "accepted": sum(r["accepted"] for r in noisy if r["owner"] == "noisy"),
        "other_owners_accepted": sum(r["accepted"] for r in noisy if r["owner"] != "noisy"),
        "samples": noisy,
    }
    passed = all(r["ok"] for r in rows + noisy) and queue["status"] == "passed"
    passed = passed and len(accepted) == min(reps, engine.limits.live_active) * runs
    passed = passed and noise_receipt["accepted"] <= engine.limits.live_per_rep
    passed = passed and noise_receipt["other_owners_accepted"] > 0
    return {
        "status": "passed" if passed else "failed",
        "opportunities": len(rows),
        "accepted": len(accepted),
        "rejected": len(rows) - len(accepted),
        "queue": queue,
        "samples": rows,
        "noisy_owner": noise_receipt,
        "latency_including_rejections": summarize(rows, target_ms=1500),
        "boundary": "real admission and cancellation; synthetic provider latency; all opportunities retained",
    }


def run_requests(service, p, campaign, reps, measurement, checkpoint):
    from app.db import put

    principals = [p]
    with service.db.transaction() as s:
        existing = {u.data.get("email"): u for u in service.scoped(s, "users", p)}
        for i in range(1, reps):
            user = existing.get(f"measure-{i}@example.test") or put(
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
        "cold": {
            **summarize(cold, target_ms=1000),
            "samples_per_rep": 1,
            "sampling": "one first decision per rep after startup; percentiles are order statistics of n=reps",
        },
        "warm": summarize(rows, target_ms=250),
        "sql_budget_met": max(r["sql"] for r in rows) <= 30 if reps == 1 else None,
        "warmups": warmups,
        "samples": rows,
        "boundary": "complete authority selection and claim; presence before/after excluded; no carrier I/O",
    }


def call_cycle(
    client, headers, campaign, measured, identity, *, simulated_seconds=0, callback=False, runtime=False
):
    """One actual HTTP control journey with simulated carrier; no paid endpoint.

    With `runtime`, the production lanes are running: as in production, the webhook lane is the
    only processor of a received event, so event-to-state waits for it instead of racing it.
    """
    service = client.app.state.service
    assert service.settings.simulation, "Measurement cannot run on a live authority"
    paths, call, begun, outcome = {}, None, time.perf_counter(), {}

    def request(method, path, **kwargs):
        response = client.request(method, path, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()

    def record(name, function):
        row, value = measured.sample(function)
        paths[name] = row
        if not row["ok"]:
            raise RuntimeError("Measured path failed: " + name)
        return value

    def settle(tenant_id, receipt_id):
        if not runtime:
            return service._process_webhook(tenant_id, receipt_id)
        from app.db import MODELS
        from sqlalchemy import select

        inbox, deadline = MODELS["webhook_inbox"], time.monotonic() + LANE_SETTLE_SECONDS
        while True:
            with service.db.transaction(False) as s:
                status = s.scalar(select(inbox.op_status).where(inbox.id == receipt_id))
            if status in {"processed", "expired_raw"}:
                return {"received": True, "status": status}
            if time.monotonic() >= deadline:
                raise TimeoutError("The webhook lane did not process the receipt")
            time.sleep(0.002)

    try:
        request(
            "POST",
            "/v1/rep/presence",
            json={
                "status": "ready",
                "audio_ready": True,
                "telephony_ready": True,
                "agent_call_control_id": "measurement-agent-"
                + hashlib.sha256(headers["Authorization"].encode()).hexdigest()[:12],
            },
        )
        selected = record(
            "selection", lambda: request("POST", "/v1/queue/next", json={"campaign_id": campaign})
        )
        call = record(
            "start",
            lambda: request(
                "POST",
                "/v1/calls/start",
                json={
                    "reservation_id": selected["reservation_id"],
                    "idempotency_key": "measurement:" + identity,
                },
            ),
        )
        if call["state"] == "RECONCILING":
            # Telnyx cannot be asked about a dial by command ID, so neither can the simulator: the
            # signed call.initiated event carries the original carrier reference, as in production.
            from app.db import get

            with service.db.transaction(False) as s:
                tenant = get(s, "call_attempts", call["id"]).tenant_id
            initiated = {
                "id": "measure-initiated:" + identity,
                "event_type": "call.initiated",
                "payload": {
                    "call_control_id": service.simulated_carrier.calls[call["id"]]["call_control_id"],
                    "direction": "outgoing",
                    "client_state": base64.b64encode(
                        json.dumps({"call_id": call["id"], "tenant_id": tenant}).encode()
                    ).decode(),
                },
            }
            digest = hashlib.sha256(json.dumps(initiated, sort_keys=True).encode()).hexdigest()
            settle(tenant, service._receive_webhook(tenant, initiated, digest)["receipt_id"])
            record("recovery", lambda: request("POST", "/v1/admin/recovery"))
            call = request("GET", f"/v1/calls/{call['id']}")
        provider_event = {
            "id": "measure-event:" + identity,
            "event_type": "call.answered",
            "payload": {
                "call_control_id": call["provider_id"],
                "occurred_at": service.clock().isoformat(),
                "client_state": base64.b64encode(
                    json.dumps({"call_id": call["id"], "kind": "prospect"}).encode()
                ).decode(),
            },
        }
        principal = request("GET", "/v1/workspace")["user"]
        # Identity comes from the stored call, not a user-supplied tenant guess.
        from app.db import get

        with service.db.transaction(False) as s:
            stored = get(s, "call_attempts", call["id"])
            p = {
                "tenant_id": stored.tenant_id,
                "seller_id": stored.seller_id,
                "user_id": principal["id"],
                "role": principal["role"],
            }
        receipt = record(
            "event_ack",
            lambda: service._receive_webhook(
                p["tenant_id"],
                provider_event,
                hashlib.sha256(json.dumps(provider_event, sort_keys=True).encode()).hexdigest(),
            ),
        )
        record("event_state", lambda: settle(p["tenant_id"], receipt["receipt_id"]))
        request(
            "POST",
            f"/v1/calls/{call['id']}/transcript",
            json={
                "segment_id": "measure-turn",
                "revision": 1,
                "side": "remote",
                "text": "Our purchasing team needs clearer ownership of approvals.",
                "is_final": True,
            },
        )

        def context():
            value = service._help_context(p, call["id"], True, "Help clarify the next step")
            assert value is not None
            return value

        record("live_context", context)
        raw_expired = None
        if simulated_seconds:
            assert simulated_seconds >= 300 and hasattr(service.clock, "now")
            service.clock.now += timedelta(seconds=simulated_seconds)
            raw_expired = not request("GET", f"/v1/calls/{call['id']}/analysis")["transcript"]
            assert raw_expired
        record("hangup", lambda: request("POST", f"/v1/calls/{call['id']}/hangup"))
        current = request("GET", f"/v1/calls/{call['id']}")
        saved = record(
            "wrapup",
            lambda: request(
                "POST",
                f"/v1/calls/{call['id']}/disposition",
                json={
                    "outcome": "callback_requested" if callback else "right_party",
                    **(
                        {
                            "callback_at": (service.clock() + timedelta(days=1)).isoformat(),
                            "callback_note": "Rep-confirmed follow-up about purchasing ownership.",
                        }
                        if callback
                        else {}
                    ),
                    "contact_role": "right_party",
                    "request_id": str(uuid4()),
                    "expected_version": current["version"],
                },
            ),
        )
        outcome.update(
            {
                "ok": True,
                "paths": paths,
                "raw_expired": raw_expired,
                "task_id": saved.get("canonical_task_id"),
                "call_id": call["id"],
                "elapsed_ms": (time.perf_counter() - begun) * 1000,
            }
        )
    except Exception as exc:
        outcome.update(
            {
                "ok": False,
                "paths": paths,
                "error": type(exc).__name__,
                "elapsed_ms": (time.perf_counter() - begun) * 1000,
            }
        )
    finally:
        if call is not None:
            paths["cleanup_hangup"] = measured.sample(
                lambda: request("POST", f"/v1/calls/{call['id']}/hangup")
            )[0]
        paths["pause"] = measured.sample(
            lambda: request("POST", "/v1/rep/presence", json={"status": "paused"})
        )[0]
        outcome["ok"] = outcome.get("ok", False) and all(r["ok"] for r in paths.values())
        outcome["elapsed_ms"] = (time.perf_counter() - begun) * 1000
    return outcome


def workload_capacity(service, p, reps):
    """Declared synthetic capacity, not a bypass of runtime admission checks."""
    from app.db import put, update

    with service.db.transaction() as s:
        numbers = service.scoped(s, "number_inventory", p)
        for number in numbers:
            update(number, daily_limit=1000)
        prototype = dict(numbers[0].data)
        for i in range(max(0, reps - len(numbers) + 1)):
            phone = f"+14155550{200 + i:03d}"
            put(
                s,
                "number_inventory",
                p["tenant_id"],
                p["seller_id"],
                {**prototype, "phone": phone, "region": "415", "daily_limit": 1000},
                key=phone,
                now=service.clock(),
            )
        for budget in service.scoped(s, "budgets", p):
            update(budget, daily_limit_usd=5000, monthly_limit_usd=5000)
    return {
        "number_limit": 1000,
        "numbers": max(len(numbers), reps + 1),
        "synthetic_budget_usd": 5000,
        "external_calls": False,
    }


def run_control_paths(client, headers, campaign, measured, checkpoint):
    attempts = []
    for i in range(WARMUPS + SAMPLES):
        attempts.append(call_cycle(client, headers, campaign, measured, f"controls-{i}"))
        if i % 20 == 0:
            checkpoint({"control_cycles": i + 1, "control_failures": sum(not r["ok"] for r in attempts)})
    rows = attempts[WARMUPS:]
    paths = {
        name: summarize(
            [r["paths"].get(name, {"ok": False, "ms": r["elapsed_ms"], "unattempted": True}) for r in rows],
            target_ms=target,
        )
        for name, target in {
            "event_ack": 100,
            "event_state": 250,
            "live_context": 100,
            "wrapup": 250,
            "hangup": 250,
        }.items()
    }
    return {
        "status": "passed"
        if all(r["ok"] for r in rows) and all(r["status"] == "passed" for r in paths.values())
        else "failed",
        "boundaries": paths,
        "warmups": attempts[:WARMUPS],
        "samples": rows,
        "boundary": "HTTP control/wrap-up; durable verified-event receipt and reducer, excluding network/signature transit",
    }


def queue_state(service):
    from app.db import Job
    from sqlalchemy import and_, func, or_, select

    with service.db.transaction(False) as s:
        states = dict(s.execute(select(Job.status, func.count()).group_by(Job.status)).all())
        due = s.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.status.in_(["queued", "retry"]),
                Job.available_at <= service.clock(),
            )
        )
        retry = s.scalar(
            select(func.count())
            .select_from(Job)
            .where(or_(Job.status == "retry", and_(Job.status == "queued", Job.error.is_not(None))))
        )
    return {
        "due": due,
        "running": states.get("running", 0),
        "dead": states.get("dead", 0),
        "retry": retry,
        "total": sum(states.values()),
    }


def maintain_and_drain(client, service, *, waves=1):
    """Keep every inner failure even when a later retry empties the queue."""
    from functools import partial

    maintenance = client.portal.call(service.maintenance_tick)
    errors = list(maintenance["errors"])
    samples = [queue_state(service)]
    drained = 0
    for _ in range(waves):
        if not (samples[-1]["due"] or samples[-1]["running"]):
            break
        receipt = client.portal.call(
            partial(
                service.run_jobs, queues=["webhook", "live_coach", "postcall", "research", "scripts", "ml"]
            )
        )
        errors.extend(receipt["errors"])
        drained += receipt["processed"]
        samples.append(queue_state(service))
    return {
        "status": "failed" if errors or samples[-1]["dead"] or samples[-1]["retry"] else "passed",
        "errors": errors,
        "maintenance": maintenance,
        "drained": drained,
        "queue_samples": samples,
    }


def aware(value):
    """SQLite returns naive UTC datetimes; PostgreSQL returns aware ones."""
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def label(value, default="unclassified"):
    """Only short identifiers leave the store; anything else could be content."""
    return value if isinstance(value, str) and LABEL.fullmatch(value) else default


def job_kind(payload):
    kind = payload.get("kind") if isinstance(payload, dict) else None
    return "default" if kind is None else label(kind)


def describe_jobs(service, ids):
    """Content-free description of jobs: no payload values, key suffixes or error text."""
    from app.db import Job
    from sqlalchemy import select

    ids, rows = list(dict.fromkeys(i for i in ids if i))[:500], []
    with service.db.transaction(False) as s:
        for job in s.scalars(select(Job).where(Job.id.in_(ids))) if ids else ():
            rows.append(
                {
                    "id": job.id,
                    "queue": job.queue,
                    "kind": job_kind(job.payload),
                    "key_prefix": label(job.key.split(":", 1)[0]) if ":" in job.key else "unclassified",
                    "status": job.status,
                    "attempts": job.attempts,
                    "error_type": label(job.error) if job.error else None,
                }
            )
    return sorted(rows, key=lambda row: (row["queue"], row["kind"], row["id"]))


def queue_composition(service):
    """Counts of unfinished work by (queue, kind, status, due, has_error)."""
    from app.db import Job
    from sqlalchemy import select

    now, counts = aware(service.clock()), Counter()
    with service.db.transaction(False) as s:
        query = select(Job.queue, Job.payload, Job.status, Job.available_at, Job.error).where(
            Job.status != "completed"
        )
        for queue, payload, status, available_at, error in s.execute(query):
            due = status in {"queued", "retry"} and available_at is not None and aware(available_at) <= now
            counts[(queue, job_kind(payload), status, due, error is not None)] += 1
    return [
        {"queue": queue, "kind": kind, "status": status, "due": due, "has_error": error, "count": count}
        for (queue, kind, status, due, error), count in sorted(counts.items())
    ]


def unresolved_job_ids(service, limit=200):
    from app.db import Job
    from sqlalchemy import and_, or_, select

    with service.db.transaction(False) as s:
        return list(
            s.scalars(
                select(Job.id)
                .where(
                    or_(
                        Job.status.in_(["dead", "retry"]),
                        and_(Job.status == "queued", Job.error.is_not(None)),
                    )
                )
                .order_by(Job.id)
                .limit(limit)
            )
        )


def queue_snapshot(service, job_ids=()):
    return {
        "state": queue_state(service),
        "composition": queue_composition(service),
        "jobs": describe_jobs(service, [*job_ids, *unresolved_job_ids(service)]),
    }


def runtime_lanes(queues):
    return list(dict.fromkeys("maintenance_tick" if q in MAINTENANCE_LANES else q for q in queues))


class BackgroundRecorder:
    """Production lanes swallow exceptions; wrapping their service calls retains every error."""

    FIELDS = ("kind", "job_id", "queue", "user_id", "error_type")

    def __init__(self, service, *, monotonic=time.monotonic):
        self.service, self.monotonic, self.lock = service, monotonic, threading.Lock()
        self.errors, self.started, self.processed, self.finished = [], Counter(), Counter(), {}
        self.begun = monotonic()

    def _error(self, lane, source, error):
        with self.lock:
            self.errors.append(
                {"lane": lane, "source": source, "at_seconds": self.monotonic() - self.begun, **error}
            )

    async def _observed(self, lane, source, call):
        with self.lock:
            index = self.started[lane]
            self.started[lane] += 1
        try:
            receipt = await call()
        except Exception as exc:
            self._error(lane, source, {"kind": "raised", "error_type": type(exc).__name__})
            raise
        else:
            if isinstance(receipt, dict):
                for error in receipt.get("errors") or ():
                    self._error(lane, source, {k: error[k] for k in self.FIELDS if k in error})
                with self.lock:
                    self.processed[lane] += receipt.get("processed") or 0
            return receipt
        finally:
            with self.lock:
                self.finished[lane] = max(self.finished.get(lane, -1), index)

    @contextmanager
    def recording(self):
        service = self.service
        owned = {name: name in vars(service) for name in ("run_jobs", "maintenance_tick")}
        run_jobs, maintenance_tick = service.run_jobs, service.maintenance_tick

        async def recorded_run_jobs(*args, **kwargs):
            queues = kwargs.get("queues", args[1] if len(args) > 1 else None)
            lane = ",".join(queues) if queues else "all"
            return await self._observed(lane, "run_jobs", lambda: run_jobs(*args, **kwargs))

        async def recorded_maintenance_tick(*args, **kwargs):
            return await self._observed(
                "maintenance_tick", "maintenance_tick", lambda: maintenance_tick(*args, **kwargs)
            )

        service.run_jobs, service.maintenance_tick = recorded_run_jobs, recorded_maintenance_tick
        try:
            yield self
        finally:
            for name, original in (("run_jobs", run_jobs), ("maintenance_tick", maintenance_tick)):
                if owned[name]:
                    setattr(service, name, original)
                else:
                    delattr(service, name)

    def mark(self):
        with self.lock:
            return dict(self.started)

    def passed_since(self, mark, lanes):
        """Every lane finished a pass that started after the mark."""
        with self.lock:
            return all(self.finished.get(lane, -1) >= mark.get(lane, 0) for lane in lanes)

    def error_count(self):
        with self.lock:
            return len(self.errors)

    def job_ids(self):
        with self.lock:
            return [error["job_id"] for error in self.errors if error.get("job_id")]

    def receipt(self):
        with self.lock:
            return {
                "status": "failed" if self.errors else "passed",
                "errors": list(self.errors),
                "lane_calls": dict(sorted(self.started.items())),
                "processed": dict(sorted(self.processed.items())),
            }


@contextmanager
def production_runtime(client, service):
    """The authority's own lanes, as production runs them, in the app's event loop."""
    from app.runtime import AuthorityRuntime

    queues, previous, runtime = list(service.settings.worker_queues), service.settings.worker_enabled, None

    async def build():
        return AuthorityRuntime(service, queues=queues)

    service.settings.worker_enabled = True
    try:
        runtime = client.portal.call(build)
        client.portal.call(runtime.start)
        yield runtime
    finally:
        try:
            if runtime is not None:
                client.portal.call(runtime.close)
        finally:
            service.settings.worker_enabled = previous


def load_paths(samples, reps):
    """Per-path distributions under load; a cycle missing a path is a failed sample of it."""
    rows = {name: [] for name in [*LOAD_TARGETS_MS, *LOAD_REPORTED]}
    for cycle in samples:
        journeys = cycle.get("reps") or [{"elapsed_ms": cycle.get("ms", 0.0)}] * reps
        for journey in journeys:
            paths = journey.get("paths") or {}
            fallback = journey.get("elapsed_ms", cycle.get("ms", 0.0))
            for name, values in rows.items():
                values.append(paths.get(name) or {"ok": False, "ms": fallback, "unattempted": True})
    return {
        name: {
            **(
                summarize(values, target_ms=LOAD_TARGETS_MS[name])
                if name in LOAD_TARGETS_MS
                else reported(values)
            ),
            "unattempted": sum(bool(v.get("unattempted")) for v in values),
        }
        for name, values in rows.items()
    }


def await_recovery(
    service,
    recorder,
    lanes,
    *,
    follows,
    seconds=RECOVERY_SECONDS,
    poll=0.25,
    monotonic=time.monotonic,
    pause=time.sleep,
):
    """After the last cycle the lanes keep running; the queue must settle on its own."""
    mark, begun, samples = recorder.mark(), monotonic(), []
    while True:
        state, elapsed = queue_state(service), monotonic() - begun
        samples.append({**state, "at_seconds": elapsed})
        passes = recorder.passed_since(mark, lanes)
        # A frozen clock never brings a retry due; waiting for it would only burn the window.
        settled = passes and not state["due"] and not state["running"] and not (state["retry"] and follows)
        if settled or elapsed >= seconds:
            break
        pause(poll)
    unresolved = [key for key in ("due", "running", "retry", "dead") if state[key]]
    return {
        "status": "passed" if settled and not unresolved else "failed",
        "settled": settled,
        "lane_pass_after_load": passes,
        "seconds": elapsed,
        "limit_seconds": seconds,
        "unresolved": unresolved,
        "samples": samples,
    }


def run_load(
    client, headers, campaign, measured, seconds, checkpoint, *, p, reps, recovery_seconds=RECOVERY_SECONDS
):
    """Rep journeys against the production runtime lanes, on a clock that follows real time."""
    service = client.app.state.service
    from app.security import issue_session

    rep_headers = [headers]
    with service.db.transaction() as s:
        for user in service.scoped(s, "users", p):
            if user.id != p["user_id"] and user.data.get("email", "").startswith("measure-"):
                token = issue_session(s, service.settings, user, service.clock())["access_token"]
                rep_headers.append({"Authorization": "Bearer " + token})
    assert len(rep_headers) == reps
    queues = list(service.settings.worker_queues)
    lanes, recorder = runtime_lanes(queues), BackgroundRecorder(service)
    follow = getattr(service.clock, "following", None)

    def cycle(index):
        with ThreadPoolExecutor(max_workers=reps) as pool:
            calls = [
                pool.submit(call_cycle, client, h, campaign, measured, f"load-{index}-{rep}", runtime=True)
                for rep, h in enumerate(rep_headers)
            ]
            results = [future.result() for future in calls]
        result = {"ok": all(r.get("ok") is True for r in results), "reps": results}
        checkpoint(
            {"load_cycles": index + 1, "latest_ok": result["ok"], "background_errors": recorder.error_count()}
        )
        return result

    with (
        recorder.recording(),
        follow() if follow else nullcontext(),
        production_runtime(client, service),
    ):
        began = service.clock()
        cycles = run_for(seconds, cycle)
        end = queue_snapshot(service, recorder.job_ids())
        recovery = await_recovery(
            service, recorder, lanes, follows=follow is not None, seconds=recovery_seconds
        )
        recovery.update(queue_snapshot(service, recorder.job_ids()))
        followed = (service.clock() - began).total_seconds()
    background = recorder.receipt()
    samples = cycles.get("samples") or []
    paths = load_paths(samples, reps) if samples else {}
    gate_failures = [] if follow else ["clock_not_following"]
    if not samples:
        gate_failures.append("no_load_cycles")
    if any(sample.get("ok") is not True for sample in samples):
        gate_failures.append("cycle_failures")
    gate_failures += [f"path:{name}" for name, row in paths.items() if row["status"] == "failed"]
    if background["errors"]:
        gate_failures.append("background_errors")
    if recovery["status"] != "passed":
        gate_failures.append("queue_recovery")
    status = "failed" if gate_failures else "passed"
    return {
        **cycles,
        "status": status,
        "ok": status == "passed",
        "gate_failures": gate_failures,
        "paths": paths,
        "runtime": {
            "queues": queues,
            "lanes": lanes,
            "worker_enabled": True,
            "clock_followed_seconds": followed,
        },
        "background": background,
        "queue_end": end,
        "queue_recovery": recovery,
        "boundary": "HTTP rep journeys with production AuthorityRuntime lanes; business clock follows real time",
    }


def run_shift(settings, clock, headers, campaign, hours, checkpoint):
    """Eight virtual hours; real persisted HTTP journeys and four authority lifetimes."""
    from app.db import MODELS, ActiveClaim
    from app.main import create_app
    from app.providers.simulated import SimulatedCarrier
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    began, wall, rows, restarts = clock.now, time.perf_counter(), [], []
    maintenance, queue_samples, errors = [], [], []
    carrier, task_ids = SimulatedCarrier(), set()
    count = int(hours * 3600 / 720)
    for page in range(0, count, 10):
        app = create_app(settings, clock=clock, carrier=carrier)
        with TestClient(app) as client:
            service = app.state.service
            measured = Measurements(service)

            def maintain(service=service, measured=measured):
                row, receipt = measured.sample(lambda: maintain_and_drain(client, service, waves=200))
                if receipt:
                    row["ok"] = row["ok"] and receipt["status"] == "passed"
                    errors.extend(receipt["errors"])
                    queue_samples.extend(receipt["queue_samples"])
                maintenance.append(row)

            with measured.instrument():
                maintain()
                for index in range(page, min(page + 10, count)):
                    carrier.timeout_after_accept = index % 7 == 0
                    before = len(carrier.dials)
                    row = call_cycle(
                        client,
                        headers,
                        campaign,
                        measured,
                        f"shift-{index}",
                        simulated_seconds=720,
                        callback=index % 4 == 0,
                    )
                    row["one_carrier_submission"] = len(carrier.dials) == before + 1
                    if row.get("task_id"):
                        task_ids.add(row["task_id"])
                    row["raw_bytes"] = service.raw.bytes_used
                    rows.append(row)
                    maintain()
                    checkpoint(
                        {"shift_calls": len(rows), "simulated_seconds": (clock.now - began).total_seconds()}
                    )
                    if not row["ok"]:
                        break
            with service.db.transaction(False) as s:
                retained = set(
                    s.scalars(
                        select(MODELS["followup_tasks"].id).where(MODELS["followup_tasks"].id.in_(task_ids))
                    )
                )
                claims = s.scalar(select(func.count()).select_from(ActiveClaim))
            restarts.append(
                {
                    "phase": page // 10,
                    "task_count": len(retained),
                    "tasks_preserved": retained == task_ids,
                    "claims": claims,
                    "integrity": service.db.integrity(),
                    "active_helper": len(service.coach.active),
                }
            )
        assert service.raw.bytes_used == 0
        if rows and not rows[-1]["ok"]:
            break
    elapsed = (clock.now - began).total_seconds()
    passed = (
        elapsed == hours * 3600
        and all(r["ok"] and r["one_carrier_submission"] and r["raw_expired"] for r in rows)
        and all(
            r["tasks_preserved"] and not r["claims"] and r["integrity"] and not r["active_helper"]
            for r in restarts
        )
        and all(row["ok"] for row in maintenance)
        and bool(queue_samples)
        and not errors
        and not any(queue_samples[-1][key] for key in ("due", "running", "retry", "dead"))
    )
    return {
        "status": "passed" if passed else "failed",
        "elapsed_seconds": elapsed,
        "wall_seconds": time.perf_counter() - wall,
        "samples": rows,
        "restarts": restarts,
        "errors": errors,
        "maintenance": {
            "ticks": len(maintenance),
            "status": "passed" if maintenance and all(row["ok"] for row in maintenance) else "failed",
            "samples": maintenance,
        },
        "queue_recovery": {
            "status": "passed"
            if queue_samples
            and not errors
            and not any(queue_samples[-1][k] for k in ("due", "running", "retry", "dead"))
            else "failed",
            "due_remaining": queue_samples[-1]["due"] if queue_samples else None,
            "running_remaining": queue_samples[-1]["running"] if queue_samples else None,
            "dead_remaining": queue_samples[-1]["dead"] if queue_samples else None,
            "retry_remaining": queue_samples[-1]["retry"] if queue_samples else None,
            "samples": queue_samples,
        },
        "boundary": "simulated clock, carrier and provider failures; real authority restarts/persistence; native process proof separate",
    }


def run_profile(name, directory, seed, checkpoint):
    from app.config import Settings
    from app.main import create_app
    from fastapi.testclient import TestClient

    from scripts.disposable_database import reset_database

    spec, clock = PROFILES[name], Clock()
    database = profile_database(name, directory)
    reference = shared_reference() if name in {"shared", "saturation"} else None
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
        session_seconds=86400,
    )
    begun = time.perf_counter()
    app = create_app(settings, clock=clock)
    from app.priority import settle_heap

    settle_heap()  # As the serving authority does after startup.
    result = {"profile": name, "declared": spec, "status": "running"}
    if reference:
        result["shared_reference"] = reference
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
                measured.phase = "selection"
                result["selection"] = run_requests(service, p, campaign, spec["reps"], measured, checkpoint)
                measured.phase = "cached_workspace"
                reports = [measured.sample(lambda: service.snapshot(p))[0] for _ in range(WARMUPS + SAMPLES)]
                result["cached_workspace"] = {
                    "summary": summarize(reports[WARMUPS:], target_ms=250),
                    "samples": reports[WARMUPS:],
                }
                measured.phase = "six_coach"
                result["six_coach"] = client.portal.call(coach_fixture, service, p)
                measured.phase = "live_admission"
                result["live_admission"] = client.portal.call(admission_fixture, service, spec["reps"])
                measured.phase = "workload_capacity"
                result["workload_capacity"] = workload_capacity(service, p, spec["reps"])
                measured.phase = "control_paths"
                result["control_paths"] = run_control_paths(client, headers, campaign, measured, checkpoint)
                if "load_seconds" in spec:
                    measured.phase = "load"
                    result["load"] = run_load(
                        client,
                        headers,
                        campaign,
                        measured,
                        spec["load_seconds"],
                        checkpoint,
                        p=p,
                        reps=spec["reps"],
                    )
                if name in {"shared", "saturation"}:
                    measured.phase = "stalled_selection"
                    measured.stall_ms = 100
                    result["stalled_selection"] = run_requests(
                        service, p, campaign, spec["reps"], measured, checkpoint
                    )
                    measured.stall_ms = 0
                measured.phase = None
            result["writer_transactions"] = summarize_writes(measured.writes, target_ms=25)
            result["training_imported"] = any(m in sys.modules for m in ("numpy", "scipy", "sklearn"))
            result["integrity"] = service.db.integrity()
        if "simulated_hours" in spec:
            result["shift"] = run_shift(
                settings, clock, headers, campaign, spec["simulated_hours"], checkpoint
            )
    finally:
        app.state.db.engine.dispose()
    if reference:
        result["shared_reference_after"] = shared_reference()
    result.update(profile_gates(result, spec))
    return result


def native_gates(path, metadata):
    """Native proof is a separate receipt; a missing one is incomplete, never a pass."""
    if path is None:
        return {**resource_gates({}), "receipt": None}
    try:
        receipt = json.loads(Path(path).read_text())
        gates = resource_gates(receipt)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {
            "status": "failed",
            "missing": [],
            "failures": ["unreadable_receipt"],
            "error_type": type(exc).__name__,
        }
    if receipt.get("source_commit") != metadata.get("source_commit"):
        gates = {**gates, "status": "failed", "failures": [*gates["failures"], "source_commit_mismatch"]}
    return {**gates, "receipt": str(path), "receipt_source_commit": receipt.get("source_commit")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=[*PROFILES, "all"], required=True)
    parser.add_argument("--seed", type=int, default=20261018)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--native-receipt", type=Path, help="JSON receipt from measure_native_resources")
    parser.add_argument(
        "--require-native-resources",
        action="store_true",
        help="Fail unless a current native resource receipt passes; by default it is reported only",
    )
    args = parser.parse_args(argv)
    try:
        output = validate_output(args.output_dir)
    except ValueError as exc:
        parser.error(str(exc))
    output.mkdir(parents=True, exist_ok=True)
    metadata = source_metadata()
    native = native_gates(args.native_receipt, metadata)
    summary = {
        **metadata,
        "seed": args.seed,
        "profiles": {},
        "resource_gates": native,
        "native_resources_required": args.require_native_resources,
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
    statuses = list(summary["profiles"].values())
    native_ok = native["status"] == "passed" or not args.require_native_resources
    if "failed" in statuses or not native_ok:
        summary["status"] = "failed"
    elif statuses and all(status == "passed" for status in statuses):
        summary["status"] = "passed"
    else:
        summary["status"] = "incomplete"
    save(output / "summary.json", summary)
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "services/backend"))
    raise SystemExit(main())
