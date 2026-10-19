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
