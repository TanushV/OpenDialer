"""E13 measurement receipts must retain failures and the declared workload."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def measurement():
    path = ROOT / "scripts/measure_completion.py"
    assert path.is_file(), "The bounded completion measurement runner is not implemented"
    spec = importlib.util.spec_from_file_location("completion_measurement", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_profile_sizes_and_sampling_cannot_claim_a_smaller_workload():
    m = measurement()
    assert m.PROFILES["historical"]["leads"] == 503
    assert m.PROFILES["local"]["attempts"] == 100_000
    assert m.PROFILES["shared"]["reps"] == 8
    assert m.PROFILES["saturation"]["reps"] == 20
    assert m.PROFILES["scaling"]["leads"] == 100_000
    assert m.PROFILES["scaling"]["attempts"] == 1_000_000
    assert m.PROFILES["shift"]["simulated_hours"] == 8
    assert m.WARMUPS == 20 and m.SAMPLES == 200 and m.COACH_RUNS == 30


def test_failed_and_timed_out_work_remains_in_latency_denominator():
    m = measurement()
    rows = [{"ms": 10, "ok": True, "sql": 2, "decrypted": 3} for _ in range(19)]
    rows += [{"ms": 8000, "ok": False, "sql": 1, "decrypted": 0}]
    result = m.summarize(rows, target_ms=250)
    assert result["samples"] == 20 and result["failures"] == 1
    assert result["p99_ms"] == 8000 and result["max_ms"] == 8000
    assert result["status"] == "failed"


@pytest.mark.parametrize("rows", [[], [{"ms": -1, "ok": True}], [{"ms": float("nan"), "ok": True}]])
def test_invalid_measurements_cannot_be_certified(rows):
    with pytest.raises(ValueError):
        measurement().summarize(rows, target_ms=250)


def test_installed_or_symlinked_output_is_refused_before_any_write(tmp_path):
    m = measurement()
    installed = Path.home() / "Library/Application Support/com.opendialer.desktop/local"
    with pytest.raises(ValueError, match="installed"):
        m.validate_output(installed)
    link = tmp_path / "elsewhere"
    link.symlink_to(installed, target_is_directory=True)
    with pytest.raises(ValueError, match="installed"):
        m.validate_output(link)


def test_shared_profile_requires_its_separate_disposable_store(monkeypatch, tmp_path):
    m = measurement()
    monkeypatch.delenv("OPENDIALER_PERFORMANCE_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="PERFORMANCE_DATABASE"):
        m.profile_database("shared", tmp_path)
    monkeypatch.setenv(
        "OPENDIALER_PERFORMANCE_DATABASE_URL",
        "postgresql+psycopg://opendialer:remediation-disposable-test@127.0.0.1:55439/opendialer_remediation_test",
    )
    with pytest.raises(ValueError, match="disposable"):
        m.profile_database("shared", tmp_path)


def test_unit_browser_and_performance_database_identities_are_distinct():
    from scripts.disposable_database import validate_database_url

    base = "postgresql+psycopg://opendialer:remediation-disposable-test@127.0.0.1:55439/"
    accepted = base + "opendialer_completion_performance"
    assert validate_database_url(accepted, purpose="performance") == accepted
    for purpose in ("unit", "browser"):
        with pytest.raises(ValueError):
            validate_database_url(accepted, purpose=purpose)


def test_native_resource_gate_requires_actual_frozen_process_receipts():
    result = measurement().resource_gates({"authority_rss_mib": 99, "idle_cpu_percent": 0.2})
    assert result["status"] == "incomplete"
    assert "frozen_native_processes" in result["missing"]


def test_native_resource_summary_counts_the_whole_owned_tree_and_one_core_cpu():
    m = measurement()
    samples = [
        {
            "elapsed_seconds": 0,
            "processes": [
                {"pid": 1, "create_time": 1, "role": "desktop", "rss_mib": 80, "cpu_seconds": 4},
                {"pid": 2, "create_time": 2, "role": "authority", "rss_mib": 120, "cpu_seconds": 3},
                {"pid": 3, "create_time": 3, "role": "webview", "rss_mib": 160, "cpu_seconds": 2},
            ],
        },
        {
            "elapsed_seconds": 300,
            "processes": [
                {"pid": 1, "create_time": 1, "role": "desktop", "rss_mib": 90, "cpu_seconds": 5},
                {"pid": 2, "create_time": 2, "role": "authority", "rss_mib": 125, "cpu_seconds": 4},
                {"pid": 3, "create_time": 3, "role": "webview", "rss_mib": 170, "cpu_seconds": 3},
            ],
        },
    ]
    result = m.summarize_native_resources(samples)
    assert result["desktop_rss_mib"] == 385
    assert result["authority_rss_mib"] == 125
    assert result["idle_cpu_percent"] == 1
    assert result["cpu_accounting_complete"] is True
    assert result["idle_seconds"] == 300


def test_native_resource_process_disappearance_or_pid_reuse_is_not_a_zero_cpu_success():
    m = measurement()
    before = {"pid": 1, "create_time": 1, "role": "desktop", "rss_mib": 10, "cpu_seconds": 4}
    after = {**before, "create_time": 2, "cpu_seconds": 0.5}
    result = m.summarize_native_resources(
        [
            {"elapsed_seconds": 0, "processes": [before]},
            {"elapsed_seconds": 300, "processes": [after]},
        ]
    )
    assert result["cpu_accounting_complete"] is False
    assert (
        m.resource_gates(
            {**result, "frozen_native_processes": [1], "binary_sha256": "a" * 64, "source_hash": "b" * 64}
        )["status"]
        == "incomplete"
    )


@pytest.mark.parametrize("samples", [[], [{"elapsed_seconds": 0, "processes": []}]])
def test_native_resource_summary_rejects_absent_process_samples(samples):
    with pytest.raises(ValueError):
        measurement().summarize_native_resources(samples)


def test_native_collector_retains_owned_children_after_reparenting(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import psutil

    m, now = measurement(), [0.0]
    files = [tmp_path / name for name in ("app.exe", "backend.exe", "webview.exe")]
    for path in files:
        path.write_bytes(b"synthetic executable identity")

    class Process:
        def __init__(self, pid, path, rss):
            self.pid, self.path, self.rss = pid, path, rss

        def create_time(self):
            return self.pid

        def is_running(self):
            return True

        def exe(self):
            return str(self.path)

        def children(self, recursive=False):
            return children if now[0] == 0 else []

        def cpu_times(self):
            return SimpleNamespace(user=5 + now[0] / 300, system=1)

        def memory_info(self):
            return SimpleNamespace(rss=self.rss * 1024**2)

    root = Process(11, files[0], 80)
    children = [Process(12, files[1], 120), Process(13, files[2], 160)]
    monkeypatch.setattr(psutil, "Process", lambda pid: root)
    monkeypatch.setattr(m.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(m.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(m, "source_metadata", lambda: {"source_hash": "synthetic", "external_calls": False})
    result = m.measure_native_resources(11, files[0], files[1])
    assert result["gates"]["status"] == "passed"
    assert result["desktop_rss_mib"] == 360 and result["authority_rss_mib"] == 120
    assert result["idle_cpu_percent"] == 1 and result["idle_seconds"] == 300
    assert all(len(s["processes"]) == 3 for s in result["samples"])
    with pytest.raises(ValueError, match="Exact native"):
        m.measure_native_resources(11, files[2], files[1])


def test_duration_receipt_keeps_errors_and_does_not_shortcut_elapsed_time():
    m = measurement()
    now = [0.0]
    attempts = []

    def pause(seconds):
        now[0] += seconds

    def operation(index):
        attempts.append(index)
        now[0] += 0.1
        if index == 1:
            raise TimeoutError("synthetic timeout")
        return {"ok": True}

    result = m.run_for(3, operation, monotonic=lambda: now[0], pause=pause, cadence=1)
    assert result["elapsed_seconds"] >= 3 and result["requested_seconds"] == 3
    assert attempts == [0, 1, 2]
    assert result["samples"][1]["ok"] is False
    assert result["status"] == "failed"


def test_cold_sql_and_missing_request_paths_are_release_gates():
    m = measurement()
    candidate = {
        "rebuild": {"status": "passed"},
        "training_imported": False,
        "authority_startup_ms": 10,
        "integrity": True,
        "selection": {"cold": {"status": "failed"}, "warm": {"status": "passed"}, "sql_budget_met": False},
        "cached_workspace": {"summary": {"status": "passed"}},
        "six_coach": {"summary": {"status": "passed"}},
        "writer_transactions": {"status": "passed"},
    }
    result = m.profile_gates(candidate, {})
    assert result["status"] == "failed"
    assert {"cold_selection", "selection_sql"} <= set(result["failures"])
    assert "control_paths" in result["missing"]


@pytest.mark.asyncio
async def test_admission_measurement_counts_rejection_and_noisy_owner_without_hiding_latency():
    from types import SimpleNamespace

    from app.coaching.engine import CoachEngine

    m = measurement()
    service = SimpleNamespace(coach=CoachEngine())
    result = await m.admission_fixture(service, 20, runs=1, delay_seconds=0.12)
    assert result["status"] == "passed", result
    assert result["opportunities"] == 20
    assert result["accepted"] == 8 and result["rejected"] == 12
    assert len(result["samples"]) == 20
    assert result["queue"]["samples"] == 8
    assert result["noisy_owner"]["accepted"] <= 2
    assert result["noisy_owner"]["other_owners_accepted"] > 0
    assert not service.coach.active and not service.coach._pending


def test_source_receipt_identifies_uncommitted_product_and_harness_bytes():
    result = measurement().source_metadata()
    assert isinstance(result["dirty_files"], list)
    assert len(result["harness_sha256"]) == 64
    assert len(result["test_tree_digest"]) == 64
    assert result["logical_cpus"] > 0


def test_shared_admission_stalls_and_reference_limits_are_required_gates():
    m = measurement()
    candidate = {
        "rebuild": {"status": "passed"},
        "training_imported": False,
        "authority_startup_ms": 10,
        "integrity": True,
        "selection": {"cold": {"status": "passed"}, "warm": {"status": "passed"}},
        "cached_workspace": {"summary": {"status": "passed"}},
        "six_coach": {"summary": {"status": "passed"}},
        "writer_transactions": {"status": "passed"},
        "control_paths": {"status": "passed"},
    }
    result = m.profile_gates(candidate, {"reps": 8})
    assert result["status"] == "incomplete"
    assert {"live_admission", "stalled_selection", "2vcpu_4gib_reference"} <= set(result["missing"])
    candidate.update(
        live_admission={"status": "failed"},
        stalled_selection={"warm": {"status": "failed"}},
        shared_reference={"status": "passed"},
    )
    result = m.profile_gates(candidate, {"reps": 8})
    assert {"live_admission", "stalled_selection"} <= set(result["failures"])


def test_shift_duration_without_maintenance_and_queue_recovery_is_not_acceptance():
    m = measurement()
    result = m.profile_gates(
        {"shift": {"status": "passed", "elapsed_seconds": 28800}},
        {"simulated_hours": 8},
    )
    assert "shift_maintenance" in result["missing"]
    assert "shift_queue_recovery" in result["missing"]


MEASURED_PATHS = (
    "selection",
    "start",
    "event_ack",
    "event_state",
    "live_context",
    "hangup",
    "wrapup",
    "cleanup_hangup",
    "pause",
)
GATED_PATHS = {
    "selection": 250,
    "event_ack": 100,
    "event_state": 250,
    "live_context": 100,
    "hangup": 250,
    "wrapup": 250,
}


def completed_cycle(**slow):
    return {
        "ok": True,
        "elapsed_ms": 5.0,
        "paths": {
            name: {"ok": True, "ms": slow.get(name, 1.0), "sql": 1, "decrypted": 0} for name in MEASURED_PATHS
        },
    }


class Lanes:
    """Async stand-ins for the service methods the production runtime lanes call."""

    def __init__(self, settings, *, failing_queue=None, raise_maintenance=False):
        self.settings, self.calls, self.enabled = settings, [], []
        self.failing_queue, self.raise_maintenance = failing_queue, raise_maintenance

    async def run_jobs(self, tenant_id=None, queues=None):
        lane = tuple(queues or ())
        self.calls.append(lane)
        self.enabled.append(self.settings.worker_enabled)
        errors = []
        if lane == (self.failing_queue,) and self.calls.count(lane) == 1:
            errors.append({"kind": "job", "job_id": "job-1", "queue": lane[0], "error_type": "TimeoutError"})
        return {"processed": 0, "status": "failed" if errors else "passed", "errors": errors}

    async def maintenance_tick(self):
        self.calls.append("maintenance_tick")
        self.enabled.append(self.settings.worker_enabled)
        if self.raise_maintenance and self.calls.count("maintenance_tick") == 1:
            raise RuntimeError("synthetic maintenance exception")
        return {"status": "passed", "completed": 0, "errors": []}


def lane_service(tmp_path, m, **failures):
    from types import SimpleNamespace

    from app.config import Settings
    from app.db import Database
    from app.jobs.queue import JobQueue

    settings = Settings(
        database_url="sqlite:///" + str(tmp_path / "load.db"),
        data_dir=tmp_path,
        simulation=True,
        worker_enabled=False,
        secret_key="synthetic-measurement-" + "a" * 48,
    )
    clock, db = m.Clock(), Database(settings.database_url, settings.secret_key)
    lanes = Lanes(settings, **failures)
    service = SimpleNamespace(
        settings=settings,
        clock=clock,
        db=db,
        jobs=JobQueue(db, clock),
        scoped=lambda *args: [],
        run_jobs=lanes.run_jobs,
        maintenance_tick=lanes.maintenance_tick,
    )
    return service, lanes


def run_load_with_portal(m, service, seconds=0.2):
    from types import SimpleNamespace

    from anyio.from_thread import start_blocking_portal

    with start_blocking_portal() as portal:
        client = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(service=service)), portal=portal)
        try:
            return m.run_load(client, {}, "campaign", None, seconds, lambda _: None, p={}, reps=1)
        finally:
            service.db.engine.dispose()


def expected_lanes(settings):
    return list(
        dict.fromkeys(
            "maintenance_tick" if queue in {"maintenance", "call_control"} else (queue,)
            for queue in settings.worker_queues
        )
    )


def test_load_runs_production_lanes_for_every_configured_queue_at_their_own_cadence(tmp_path, monkeypatch):
    import time

    import app.runtime as runtime

    m = measurement()
    service, lanes = lane_service(tmp_path, m)
    constructed, within_cycle = [], []

    class ProductionRuntime(runtime.AuthorityRuntime):
        def __init__(self, service, *, queues):
            constructed.append(list(queues))
            super().__init__(service, queues=queues)

    def cycle(*args):
        # One 10-second load cycle used to run maintenance and one drain once.
        # Production lanes keep running inside a single cycle.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not all(
            lanes.calls.count(lane) >= 2 for lane in expected_lanes(service.settings)
        ):
            time.sleep(0.02)
        within_cycle.append({str(lane): lanes.calls.count(lane) for lane in expected_lanes(service.settings)})
        return completed_cycle()

    monkeypatch.setattr(runtime, "AuthorityRuntime", ProductionRuntime)
    monkeypatch.setattr(m, "call_cycle", cycle)
    result = run_load_with_portal(m, service)
    assert constructed == [list(service.settings.worker_queues)], "The real runtime owns the lanes"
    assert all(count >= 2 for count in within_cycle[0].values()), within_cycle
    assert all(call == "maintenance_tick" or len(call) == 1 for call in lanes.calls), (
        "The harness must not substitute its own all-queue drain for production lanes"
    )
    assert lanes.enabled and set(lanes.enabled) == {True}, "Lanes run with the worker enabled"
    assert service.settings.worker_enabled is False, "Worker setting is restored after the load"
    assert service.run_jobs == lanes.run_jobs and service.maintenance_tick == lanes.maintenance_tick
    assert result["status"] == "passed", result["gate_failures"]
    assert result["gate_failures"] == []
    assert all(
        result["background"]["lane_calls"][lane] >= 2 for lane in ("maintenance_tick", "webhook", "ml")
    )
    assert result["queue_recovery"]["status"] == "passed"


def test_load_keeps_swallowed_lane_errors_and_maintenance_exceptions_and_fails(tmp_path, monkeypatch):
    m = measurement()
    service, lanes = lane_service(tmp_path, m, failing_queue="postcall", raise_maintenance=True)
    monkeypatch.setattr(m, "call_cycle", lambda *args: completed_cycle())
    result = run_load_with_portal(m, service)
    errors = result["background"]["errors"]
    assert any(
        e["lane"] == "postcall" and e["kind"] == "job" and e["error_type"] == "TimeoutError" for e in errors
    ), errors
    assert any(
        e["lane"] == "maintenance_tick" and e["kind"] == "raised" and e["error_type"] == "RuntimeError"
        for e in errors
    ), errors
    assert result["background"]["status"] == "failed"
    assert "background_errors" in result["gate_failures"]
    assert result["status"] == "failed" and result["ok"] is False
    assert all(sample["ok"] for sample in result["samples"]), "Cycles alone looked healthy"
    assert service.run_jobs == lanes.run_jobs and service.maintenance_tick == lanes.maintenance_tick


def test_slow_selection_under_load_fails_even_when_every_cycle_was_ok(tmp_path, monkeypatch):
    m = measurement()
    service, _ = lane_service(tmp_path, m)
    monkeypatch.setattr(m, "call_cycle", lambda *args: completed_cycle(selection=400.0, start=5000.0))
    result = run_load_with_portal(m, service)
    assert all(sample["ok"] for sample in result["samples"])
    assert result["paths"]["selection"]["status"] == "failed"
    assert result["paths"]["selection"]["target_ms"] == 250
    assert "path:selection" in result["gate_failures"]
    assert result["status"] == "failed"
    for name, target in GATED_PATHS.items():
        assert result["paths"][name]["target_ms"] == target
        if name != "selection":
            assert result["paths"][name]["status"] == "passed"
    for name in ("start", "cleanup_hangup", "pause"):
        assert result["paths"][name]["status"] == "reported"
        assert result["paths"][name]["target_ms"] is None
    assert result["paths"]["start"]["p95_ms"] == 5000.0, "Start includes the simulated dial and is reported"


def test_cycle_missing_paths_counts_failed_samples_for_those_paths():
    m = measurement()
    partial = {"ok": False, "error": "RuntimeError", "elapsed_ms": 7.0, "paths": {}}
    partial["paths"]["selection"] = completed_cycle()["paths"]["selection"]
    samples = [
        {"ok": False, "reps": [completed_cycle(), partial], "ms": 20.0},
        {"ok": False, "error": "TimeoutError", "ms": 9000.0},
    ]
    paths = m.load_paths(samples, reps=2)
    assert paths["selection"]["samples"] == 4 and paths["selection"]["failures"] == 2
    for name in GATED_PATHS:
        assert paths[name]["samples"] == 4, name
        assert paths[name]["status"] == "failed", name
        if name != "selection":
            assert paths[name]["failures"] == 3 and paths[name]["unattempted"] == 3, name
    assert paths["pause"]["status"] == "reported" and paths["pause"]["failures"] == 3


def test_measurement_clock_follows_elapsed_time_and_keeps_it():
    from datetime import timedelta

    m = measurement()
    clock, now = m.Clock(), [100.0]
    start = clock()
    with clock.following(monotonic=lambda: now[0]):
        assert clock() == start
        now[0] += 5
        assert clock() == start + timedelta(seconds=5), "Retry backoffs and leases must come due"
        clock.now += timedelta(seconds=60)
        assert clock() == start + timedelta(seconds=65)
        with pytest.raises(RuntimeError):
            with clock.following(monotonic=lambda: now[0]):
                pass
    now[0] += 100
    assert clock() == start + timedelta(seconds=65), "Leaving the load keeps the elapsed time"
    assert m.Clock.now == start, "The fixture origin remains the class default"


def writer_measurement(tmp_path, m):
    from types import SimpleNamespace

    from app.db import Database

    db = Database("sqlite:///" + str(tmp_path / "writer.db"), "synthetic-measurement-" + "b" * 48)
    return db, m.Measurements(SimpleNamespace(db=db))


def test_writer_attribution_identifies_callers_and_admission_wait(tmp_path):
    import threading
    import time

    m = measurement()
    db, measured = writer_measurement(tmp_path, m)
    holding, release = threading.Event(), threading.Event()

    def holder_transaction():
        with db.transaction():
            holding.set()
            release.wait(5)

    def contending_transaction():
        with db.transaction():
            pass

    try:
        with measured.instrument():
            measured.phase = "contention"
            first = threading.Thread(target=holder_transaction)
            first.start()
            assert holding.wait(5)
            second = threading.Thread(target=contending_transaction)
            second.start()
            time.sleep(0.2)
            release.set()
            first.join(5)
            second.join(5)
        assert db._sqlite_writer.acquire(timeout=1), "The original writer lock is restored"
        db._sqlite_writer.release()
    finally:
        db.engine.dispose()
    rows = {row["caller"].split(" < ")[0].rsplit(".", 1)[-1]: row for row in measured.writes}
    assert set(rows) == {"holder_transaction", "contending_transaction"}, measured.writes
    for row in measured.writes:
        assert not any(skip in row["caller"] for skip in ("contextlib", "app.db:", "threading")), row
        assert row["phase"] == "contention" and row["ok"] is True
    assert rows["contending_transaction"]["admission_ms"] >= 150
    assert rows["holder_transaction"]["admission_ms"] < 150
    summary = m.summarize_writes(measured.writes, target_ms=25)
    assert summary["samples"] == 2 and summary["target_ms"] == 25
    assert [a["count"] for a in summary["attribution"]] == [1, 1]
    totals = [a["total_ms"] for a in summary["attribution"]]
    assert totals == sorted(totals, reverse=True)
    assert summary["attribution"][0]["admission_p95_ms"] is not None
    assert summary["phases"]["contention"]["samples"] == 2
    assert summary["admission"]["samples"] == 2
    assert summary["admission"]["max_ms"] >= 150
    assert summary["status"] == ("passed" if summary["p95_ms"] < 25 else "failed")


def test_failed_writes_keep_their_error_type(tmp_path):
    m = measurement()
    db, measured = writer_measurement(tmp_path, m)

    def failing_write():
        with db.transaction():
            raise LookupError("synthetic rollback")

    try:
        with measured.instrument(), pytest.raises(LookupError):
            failing_write()
    finally:
        db.engine.dispose()
    (row,) = measured.writes
    assert row["ok"] is False and row["error"] == "LookupError"
    assert row["caller"].split(" < ")[0].endswith("failing_write")
    summary = m.summarize_writes(measured.writes, target_ms=25)
    assert summary["attribution"][0]["errors"] == {"LookupError": 1}
    assert summary["status"] == "failed"


def test_job_descriptions_and_queue_composition_carry_no_payload_content(tmp_path):
    import json
    from datetime import timedelta
    from types import SimpleNamespace

    from app.db import Database, Job
    from app.jobs.queue import JobQueue

    m = measurement()
    clock = m.Clock()
    db = Database("sqlite:///" + str(tmp_path / "jobs.db"), "synthetic-measurement-" + "c" * 48)
    service = SimpleNamespace(db=db, clock=clock, jobs=JobQueue(db, clock))
    try:
        failed = service.jobs.enqueue(
            "postcall",
            {"kind": "conversation_review", "call_id": "CANARY-call", "seller_id": "CANARY-seller"},
            "review:CANARY-key",
            tenant_id="tenant",
        )
        service.jobs.claim("postcall", "worker", key_prefix="review:")
        service.jobs.fail(failed["id"], "worker", "TimeoutError", retry_seconds=60)
        free_text = service.jobs.enqueue(
            "scripts", {"lead_ids": ["CANARY-lead"]}, "CANARY free key", tenant_id="tenant"
        )
        with db.transaction() as s:
            s.get(Job, free_text["id"]).error = "CANARY detail with private words"
        service.jobs.enqueue("ml", {"kind": "home_refresh"}, "home:CANARY", tenant_id="tenant")
        clock.now += timedelta(seconds=1)
        described = m.describe_jobs(service, [failed["id"], free_text["id"], failed["id"]])
        composition = m.queue_composition(service)
    finally:
        db.engine.dispose()
    assert "CANARY" not in json.dumps(described) + json.dumps(composition)
    by_queue = {row["queue"]: row for row in described}
    assert len(described) == 2
    assert by_queue["postcall"] == {
        "id": failed["id"],
        "queue": "postcall",
        "kind": "conversation_review",
        "key_prefix": "review",
        "status": "queued",
        "attempts": 1,
        "error_type": "TimeoutError",
    }
    assert by_queue["scripts"]["kind"] == "default"
    assert by_queue["scripts"]["key_prefix"] == "unclassified"
    assert by_queue["scripts"]["error_type"] == "unclassified"
    counts = {(r["queue"], r["kind"], r["status"], r["due"], r["has_error"]): r["count"] for r in composition}
    assert counts == {
        ("postcall", "conversation_review", "queued", False, True): 1,
        ("scripts", "default", "queued", True, True): 1,
        ("ml", "home_refresh", "queued", True, False): 1,
    }


def test_cold_selection_is_labelled_as_one_decision_per_rep(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    m = measurement()
    service = SimpleNamespace(
        db=SimpleNamespace(transaction=lambda *args, **kwargs: nullcontext(None)),
        scoped=lambda *args: [],
        presence=lambda *args: None,
        reserve=lambda *args: {"reservation_id": "r"},
        clock=m.Clock(),
    )
    monkeypatch.setattr(m, "WARMUPS", 0)
    monkeypatch.setattr(m, "SAMPLES", 3)
    result = m.run_requests(service, {"user_id": "u"}, "c", 1, m.Measurements(service), lambda _: None)
    assert result["cold"]["samples"] == 1 and result["cold"]["samples_per_rep"] == 1
    assert "one" in result["cold"]["sampling"]
    assert result["warm"]["samples"] == 3


def passing_native_receipt(commit):
    return {
        "source_commit": commit,
        "source_hash": "s" * 64,
        "frozen_native_processes": [[1, 1, "app.exe"]],
        "binary_sha256": {"app.exe": "a" * 64},
        "idle_seconds": 300,
        "desktop_rss_mib": 300,
        "authority_rss_mib": 120,
        "idle_cpu_percent": 0.5,
        "cpu_accounting_complete": True,
        "authority_process_count": 1,
    }


@pytest.mark.parametrize(
    ("statuses", "options", "receipt", "code"),
    [
        ({"historical": "passed"}, [], None, 0),
        ({"historical": "failed"}, [], None, 1),
        ({"historical": "incomplete"}, [], None, 1),
        ({"historical": "passed"}, ["--require-native-resources"], None, 1),
        ({"historical": "passed"}, ["--require-native-resources"], "current", 0),
        ({"historical": "passed"}, ["--require-native-resources"], "stale", 1),
        ({"historical": "passed", "local": "failed"}, [], None, 1),
    ],
)
def test_exit_code_reports_requested_profiles_and_separate_native_gate(
    tmp_path, monkeypatch, statuses, options, receipt, code
):
    import json

    m = measurement()
    monkeypatch.setattr(m, "source_metadata", lambda: {"source_commit": "c" * 40, "source_hash": "h"})
    monkeypatch.setattr(m, "run_profile", lambda name, *args: {"profile": name, "status": statuses[name]})
    argv = ["--output-dir", str(tmp_path / "out"), *options]
    argv += ["--profile", next(iter(statuses))] if len(statuses) == 1 else ["--profile", "all"]
    if len(statuses) > 1:
        monkeypatch.setattr(m, "PROFILES", {name: {} for name in statuses})
    if receipt:
        path = tmp_path / "native.json"
        path.write_text(json.dumps(passing_native_receipt("c" * 40 if receipt == "current" else "0" * 40)))
        argv += ["--native-receipt", str(path)]
    assert m.main(argv) == code
    summary = json.loads((tmp_path / "out/summary.json").read_text())
    assert summary["profiles"] == statuses
    assert summary["status"] == (
        "passed" if code == 0 else "failed" if "failed" in statuses.values() or options else "incomplete"
    )
    if not options:
        assert summary["resource_gates"]["status"] == "incomplete", "Native proof stays separate by default"


def reference_manifest():
    return {
        "api": {
            "Id": "a" * 64,
            "Image": "sha256:" + "c" * 64,
            "Config": {"Labels": {"opendialer.task": "completion-E13"}},
            "HostConfig": {
                "NanoCpus": 1500000000,
                "Memory": 3 * 1024**3,
                "MemorySwap": 3 * 1024**3,
                "NetworkMode": "container:" + "b" * 64,
                "PidMode": "container:" + "b" * 64,
            },
        },
        "database": {
            "Id": "b" * 64,
            "Image": "sha256:" + "d" * 64,
            "Config": {"Labels": {"opendialer.task": "completion-E13"}},
            "HostConfig": {"NanoCpus": 500000000, "Memory": 1024**3, "MemorySwap": 1024**3},
        },
        "runtime": {
            "hostname": "b" * 12,
            "network_namespace": "net:[123]",
            "cgroup_namespace": "cgroup:[456]",
            "pid_namespace": "pid:[789]",
            "cgroup_path": "/system.slice/docker-" + "a" * 64 + ".scope",
            "memory_swap_max": "0",
        },
        "database_runtime": {"network_namespace": "net:[123]", "pid_namespace": "pid:[789]"},
        "postgres": {
            "system_identifier": "123456789",
            "port": 55439,
            "database": "opendialer_completion_performance",
        },
    }


def observed_postgres(data):
    return {
        **data["postgres"],
        "cgroup_path": "/system.slice/docker-" + "b" * 64 + ".scope",
        "limits": {"cpu.max": "50000 100000", "memory.max": str(1024**3), "memory.swap.max": "0"},
    }


@pytest.mark.parametrize(
    "changed", ["none", "unlimited_api", "unlimited_database", "wrong_namespace", "runtime_mismatch"]
)
def test_reference_requires_inspected_caps_and_matching_actual_runtime(changed):
    m = measurement()
    data = reference_manifest()
    if changed == "unlimited_api":
        data["api"]["HostConfig"]["Memory"] = 0
    elif changed == "unlimited_database":
        data["database"]["HostConfig"]["NanoCpus"] = 0
    elif changed == "wrong_namespace":
        data["api"]["HostConfig"]["NetworkMode"] = "bridge"
    result = m.validate_reference(
        data,
        "max 100000" if changed == "runtime_mismatch" else "150000 100000",
        str(3 * 1024**3),
        runtime=data["runtime"],
        postgres=observed_postgres(data),
    )
    assert result["status"] == ("passed" if changed == "none" else "failed")
    if changed == "none":
        assert result["cpu_limit"] == 2 and result["memory_limit_mib"] == 4096


@pytest.mark.parametrize(
    "changed", ["hostname", "network_namespace", "cgroup_namespace", "system_identifier", "port", "database"]
)
def test_matching_caps_with_another_container_or_database_never_certify_reference(changed):
    m = measurement()
    data = reference_manifest()
    runtime, postgres = dict(data["runtime"]), observed_postgres(data)
    if changed in runtime:
        runtime[changed] = "different-runtime"
    else:
        postgres[changed] = 5432 if changed == "port" else "different-database"
    result = m.validate_reference(data, "150000 100000", str(3 * 1024**3), runtime=runtime, postgres=postgres)
    assert result["status"] == "failed", "Matching quotas do not bind the measured database or process"


def test_stale_reference_manifest_is_rejected_at_the_running_entrypoint(monkeypatch, tmp_path):
    import json

    m = measurement()
    data = reference_manifest()
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("OPENDIALER_REFERENCE_MANIFEST", str(path))
    original_read = Path.read_text

    def cgroups(path, *args, **kwargs):
        if str(path).startswith("/sys/fs/cgroup/") and path.name == "cpu.max":
            return "150000 100000"
        if str(path).startswith("/sys/fs/cgroup/") and path.name == "memory.max":
            return str(3 * 1024**3)
        if str(path).startswith("/sys/fs/cgroup/") and path.name == "memory.swap.max":
            return "0"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", cgroups)
    monkeypatch.setattr(
        m, "runtime_identity", lambda: {**data["runtime"], "hostname": "different"}, raising=False
    )
    monkeypatch.setattr(m, "postgres_identity", lambda: observed_postgres(data), raising=False)
    with pytest.raises(ValueError, match="reference|Reference"):
        m.shared_reference()


@pytest.mark.parametrize("changed", ["cpu", "memory", "swap", "container"])
def test_reference_reads_the_connected_postgres_process_limits_not_only_its_cluster(monkeypatch, changed):
    from contextlib import nullcontext
    from types import SimpleNamespace

    import sqlalchemy

    m = measurement()
    data = reference_manifest()
    monkeypatch.setenv(
        "OPENDIALER_PERFORMANCE_DATABASE_URL",
        "postgresql+psycopg://opendialer:remediation-disposable-test@127.0.0.1:55439/opendialer_completion_performance",
    )

    def execute(statement):
        result = ("123456789", 55439, "opendialer_completion_performance")
        if "pg_backend_pid" in str(statement):
            result += (7,)
        return SimpleNamespace(one=lambda: result)

    engine = SimpleNamespace(
        connect=lambda: nullcontext(SimpleNamespace(execute=execute)), dispose=lambda: None
    )
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *args: engine)
    original = Path.read_text
    group = "/system.slice/docker-" + ("e" if changed == "container" else "b") * 64 + ".scope"

    def actual_file(path, *args, **kwargs):
        if str(path) == "/proc/7/cgroup":
            return "0::" + group + "\n"
        if str(path).startswith("/sys/fs/cgroup/"):
            return {
                "cpu.max": "100000 100000" if changed == "cpu" else "50000 100000",
                "memory.max": str((2 if changed == "memory" else 1) * 1024**3),
                "memory.swap.max": "1024" if changed == "swap" else "0",
            }[path.name]
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", actual_file)
    result = m.validate_reference(
        data, "150000 100000", str(3 * 1024**3), runtime=data["runtime"], postgres=m.postgres_identity()
    )
    assert result["status"] == "failed", (
        "The same PostgreSQL cluster under changed effective limits cannot pass"
    )
