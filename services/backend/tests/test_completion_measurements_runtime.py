"""Measurement harness uses actual indexed encrypted records and application paths."""

import hashlib
import hmac

from app.db import MODELS
from sqlalchemy import func, select
from test_control_completion import principal

from scripts import measure_completion as measure


def test_measurement_fixture_rebuilds_real_sources_and_counts_actual_requests(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    counts = measure.populate(service, p, workspace["campaign_id"], 8, 12, 20261018)
    assert counts == {"leads": 8, "call_attempts": 12}
    with service.db.transaction(False) as s:
        assert s.scalar(select(func.count()).select_from(MODELS["phone_endpoints"])) == 8
        endpoint = s.scalar(
            select(MODELS["phone_endpoints"]).where(
                MODELS["phone_endpoints"].id
                == str(measure.uuid5(measure.NAMESPACE_URL, "opendialer-measure:20261018:phone_endpoints:0"))
            )
        )
        key = hmac.new(service.db.identity_key, endpoint.data["phone"].encode(), hashlib.sha256).hexdigest()
        assert service._by_key(s, p, "phone_endpoints", key)
    rebuilt = measure.rebuild(service, p, timeout=20)
    assert rebuilt["published"]
    service.rebuild_home_metrics(p)
    service.rebuild_optimization_report(p)
    monkeypatch.setattr(measure, "WARMUPS", 1)
    monkeypatch.setattr(measure, "SAMPLES", 3)
    instrumentation = measure.Measurements(service)
    with instrumentation.instrument():
        result = measure.run_requests(
            service, p, workspace["campaign_id"], 1, instrumentation, lambda _: None
        )
    assert len(result["warmups"]) == 1 and len(result["samples"]) == 3
    assert all(r["ok"] and r["sql"] > 0 and r["decrypted"] > 0 for r in result["samples"])
    assert instrumentation.writes


def test_failed_transaction_is_not_recorded_as_success(workspace, app):
    import pytest

    measured = measure.Measurements(app.state.service)
    with measured.instrument(), pytest.raises(RuntimeError, match="synthetic rollback"):
        with app.state.service.db.transaction():
            raise RuntimeError("synthetic rollback")
    assert len(measured.writes) == 1
    assert measured.writes[0]["ok"] is False


def test_historical_profile_imports_the_preserved_public_fixture(workspace, app):
    result = measure.populate_historical(workspace["client"], workspace["headers"], workspace["campaign_id"])
    assert result == {"leads": 503, "call_attempts": 0}
    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        records = service.scoped(s, "leads", p)
        imported = [r for r in records if r.data.get("name", "").startswith("Load ")]
        assert len(imported) == 500
        assert all(r.data["timezone"] == "America/Los_Angeles" for r in imported)
        assert all(r.data["company"] == "Load fixture" for r in imported)


def test_measured_call_cycle_uses_real_controls_and_retains_all_boundaries(workspace, app):
    service = app.state.service
    measured = measure.Measurements(service)
    with measured.instrument():
        result = measure.call_cycle(
            workspace["client"], workspace["headers"], workspace["campaign_id"], measured, "contract"
        )
    assert result["ok"], result
    assert set(result["paths"]) >= {"event_ack", "event_state", "live_context", "wrapup", "hangup"}
    assert all(value["ok"] and value["sql"] > 0 for value in result["paths"].values())
    with service.db.transaction(False) as s:
        calls = service.scoped(s, "call_attempts", principal(workspace))
        assert len(calls) == 1 and calls[0].data["state"] == "FINALIZED"
        assert calls[0].data["outcome"] == "right_party"


def test_control_measurement_profile_counts_real_paths_and_load(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    declared = measure.workload_capacity(service, p, 1)
    assert declared["external_calls"] is False
    monkeypatch.setattr(measure, "WARMUPS", 0)
    monkeypatch.setattr(measure, "SAMPLES", 1)
    measured = measure.Measurements(service)
    with measured.instrument():
        result = measure.run_control_paths(
            workspace["client"], workspace["headers"], workspace["campaign_id"], measured, lambda _: None
        )
    assert all(v["samples"] == 1 and v["failures"] == 0 for v in result["boundaries"].values()), result
    assert result["samples"][0]["ok"]
    with service.db.transaction(False) as s:
        assert len(service.scoped(s, "call_attempts", p)) == 1


def test_shift_cycle_preserves_early_task_after_raw_expiry_and_recovers_one_dial(
    workspace, app, clock, carrier
):
    measured = measure.Measurements(app.state.service)
    carrier.timeout_after_accept = True
    with measured.instrument():
        result = measure.call_cycle(
            workspace["client"],
            workspace["headers"],
            workspace["campaign_id"],
            measured,
            "shift-contract",
            simulated_seconds=720,
            callback=True,
        )
    assert result["ok"], result
    assert result["raw_expired"] is True
    assert len(carrier.dials) == 1
    assert result["task_id"]
    assert (clock.now - measure.Clock.now).total_seconds() == 720


def test_shift_harness_restarts_authority_and_verifies_retained_task(workspace, app, clock):
    result = measure.run_shift(
        app.state.service.settings, clock, workspace["headers"], workspace["campaign_id"], 0.2, lambda _: None
    )
    assert result["status"] == "passed", result
    assert result["elapsed_seconds"] == 720
    assert result["restarts"][0]["task_count"] == 1
    assert result["restarts"][0]["claims"] == 0


def test_stall_is_injected_once_per_measured_request_and_selection_reuses_reps(workspace, app, monkeypatch):
    from sqlalchemy import text

    pauses = []
    monkeypatch.setattr(measure.time, "sleep", pauses.append)
    measured = measure.Measurements(app.state.service, stall_ms=100)
    with measured.instrument():

        def twice():
            with app.state.service.db.transaction(False) as s:
                s.execute(text("SELECT 1"))
                s.execute(text("SELECT 1"))

        row, _ = measured.sample(twice)
    assert row["ok"] and row["injected_stall_ms"] == 100 and pauses == [0.1]
    monkeypatch.setattr(measure, "WARMUPS", 0)
    monkeypatch.setattr(measure, "SAMPLES", 1)
    for _ in range(2):
        measure.run_requests(
            app.state.service, principal(workspace), workspace["campaign_id"], 2, measured, lambda _: None
        )
    with app.state.service.db.transaction(False) as s:
        assert len(app.state.service.scoped(s, "users", principal(workspace))) == 2


def test_cleanup_failure_is_retained_instead_of_losing_measured_call_receipt(workspace, app, monkeypatch):
    client, previous = workspace["client"], workspace["client"].request
    hangups = []

    def fail_cleanup(method, path, **kwargs):
        if path.endswith("/hangup"):
            hangups.append(path)
            if len(hangups) == 2:
                raise TimeoutError("synthetic cleanup receipt loss")
        return previous(method, path, **kwargs)

    monkeypatch.setattr(client, "request", fail_cleanup)
    measured = measure.Measurements(app.state.service)
    with measured.instrument():
        result = measure.call_cycle(
            client, workspace["headers"], workspace["campaign_id"], measured, "cleanup"
        )
    assert result["ok"] is False
    assert result["paths"]["wrapup"]["ok"]
    assert result["paths"]["cleanup_hangup"]["error"] == "TimeoutError"
    assert result["paths"]["pause"]["ok"]
