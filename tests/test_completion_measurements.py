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


@pytest.mark.parametrize("failed", [False, True])
def test_normal_load_runs_real_maintenance_and_retains_its_failure(monkeypatch, failed):
    from contextlib import nullcontext
    from types import SimpleNamespace

    m = measurement()
    ticks = []

    def maintenance():
        ticks.append(True)
        if failed:
            raise TimeoutError("synthetic maintenance failure")
        return {"status": "passed", "errors": [], "reconciled": 1}

    service = SimpleNamespace(
        db=SimpleNamespace(transaction=lambda: nullcontext(None)),
        scoped=lambda *args: [],
        run_jobs=lambda **kwargs: {"processed": 0, "errors": []},
        maintenance_tick=maintenance,
    )
    client = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(service=service)),
        portal=SimpleNamespace(call=lambda fn: fn()),
    )
    monkeypatch.setattr(m, "call_cycle", lambda *args: {"ok": True})
    monkeypatch.setattr(m, "queue_state", lambda *args: {"due": 0, "running": 0, "retry": 0, "dead": 0})
    monkeypatch.setattr(m, "run_for", lambda seconds, cycle: cycle(0))
    result = m.run_load(client, {}, "campaign", None, 1, lambda _: None, p={}, reps=1)
    assert ticks == [True], "Normal load must include maintenance contention, not only drain queued jobs"
    assert result["ok"] is not failed
    if failed:
        assert result["background_error"] == "TimeoutError"


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
