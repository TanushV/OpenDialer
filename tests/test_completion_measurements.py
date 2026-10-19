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
        return {"reconciled": 1}

    service = SimpleNamespace(
        db=SimpleNamespace(transaction=lambda: nullcontext(None)),
        scoped=lambda *args: [],
        run_jobs=lambda **kwargs: 0,
        maintenance_tick=maintenance,
    )
    client = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(service=service)),
        portal=SimpleNamespace(call=lambda fn: fn()),
    )
    monkeypatch.setattr(m, "call_cycle", lambda *args: {"ok": True})
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
            },
        },
        "database": {
            "Id": "b" * 64,
            "Image": "sha256:" + "d" * 64,
            "Config": {"Labels": {"opendialer.task": "completion-E13"}},
            "HostConfig": {"NanoCpus": 500000000, "Memory": 1024**3, "MemorySwap": 1024**3},
        },
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
        data, "max 100000" if changed == "runtime_mismatch" else "150000 100000", str(3 * 1024**3)
    )
    assert result["status"] == ("passed" if changed == "none" else "failed")
    if changed == "none":
        assert result["cpu_limit"] == 2 and result["memory_limit_mib"] == 4096
