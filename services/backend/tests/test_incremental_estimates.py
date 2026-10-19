"""E09: sparse estimates consume corrected sufficient statistics, not historical rows."""

from datetime import timedelta

import pytest
from app.db import get, put, update
from app.optimization import projections
from app.scheduling import models
from conftest import start
from test_optimization_workflows import finish
from test_projection_replay import project_pending
from test_remediation_privacy import principal


def example_rows():
    return [
        {
            "segment": "a",
            "local_hour": 23,
            "local_weekday": 0,
            "attempt_count": 0,
            "human": True,
            "right_party": True,
            "conversion": 1,
            "window_status": "resolved",
            "rep_work_seconds": 60,
            "observed_cost_usd": 0.2,
            "cost_status": "settled",
        },
        {
            "segment": "a",
            "local_hour": 1,
            "local_weekday": 0,
            "attempt_count": 0,
            "human": None,
            "right_party": None,
            "conversion": None,
        },
        {
            "segment": "b",
            "local_hour": 13,
            "local_weekday": 0,
            "attempt_count": 0,
            "human": False,
            "right_party": None,
            "conversion": None,
        },
    ]


def test_count_reducer_preserves_stage_support_and_excludes_cell_from_prior():
    count = getattr(models, "estimate_counts", None)
    predict = getattr(models, "prediction_from_counts", None)
    assert callable(count) and callable(predict), "Sparse math needs one sufficient-statistic reducer"
    rows = example_rows()
    result = predict(count(rows), count(rows[:2]), level="segment_time_attempt")
    assert result["p_human"] == pytest.approx((1 + 10 * 2 / 11) / 11)
    assert result["p_right_party_given_human"] == pytest.approx((1 + 10 / 3) / 11)
    assert result["p_conversion_given_right_party"] == pytest.approx(3.5 / 11)
    assert result["stage_support"]["human"]["cell_unknown"] == 1
    assert result["stage_support"]["right_party"]["conditioning_excluded"] == 1
    assert result["expected_rep_seconds"] == pytest.approx(110)
    assert result["expected_attempt_cost_usd"] == pytest.approx(0.2)
    assert result["cost_observations"] == result["cost_unknown_observations"] == 1
    empty = predict(count([]), count([]), level="pooled")
    assert empty["p_human"] == 0.2 and empty["prior_only"]
    assert empty["expected_attempt_cost_usd"] is None


def test_saved_statistic_cells_keep_midnight_and_attempt_conditioning():
    count = getattr(models, "estimate_counts", None)
    assert callable(count), "A saved profile must not retain raw training rows"
    rows = example_rows()
    policy = models.EstimatePolicy()
    config = {
        "conversion_goal": "meeting_held",
        "window_days": 14,
        "handling_seconds": 120,
        "estimate_policy": policy.model_dump(),
    }
    profile = {
        "goal": "meeting_held",
        "window_days": 14,
        "work_assumption_seconds": 120,
        "estimate_policy_version": policy.version,
        "as_of": "2026-10-19T00:00:00+00:00",
        "statistics": {
            "total": count(rows),
            "cells": [
                {
                    "features": {
                        k: row[k] for k in ("segment", "local_hour", "local_weekday", "attempt_count")
                    },
                    "counts": count([row]),
                }
                for row in rows
            ],
            "cells_complete": True,
        },
    }
    result = models.saved_prediction(
        profile, {"segment": "a", "local_hour": 0, "local_weekday": 2, "attempt_count": 0}, config
    )
    assert result["p_human"] == pytest.approx((1 + 10 * 2 / 11) / 11)
    assert result["observations"] == 1
    # Do not call an inspected subset a complete local cell.
    profile["statistics"]["cells_complete"] = False
    fallback = models.saved_prediction(
        profile, {"segment": "a", "local_hour": 0, "local_weekday": 2, "attempt_count": 0}, config
    )
    assert fallback["p_human"] == pytest.approx(3 / 12)
    assert fallback["estimate_level"] == "pooled"
    assert fallback["cell_detail_limited"] is True


def test_incremental_estimates_replace_wrapup_work_and_provisional_outcomes(workspace, app, clock):
    reader = getattr(projections, "read_estimate_statistics", None)
    assert callable(reader), "Normal learning refresh must read projected counts"
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    began = clock.now
    clock.now += timedelta(seconds=30)
    finish(workspace, call)
    project_pending(service, p)
    before = reader(service, p)
    assert before["total"]["human_known"] == 1
    assert before["total"]["work_seconds"] == 30
    clock.now += timedelta(seconds=20)
    with service.db.transaction() as s:
        key = "attempt-work:" + call["id"]
        row = service._by_key(s, p, "outcomes", key)
        value = {
            "kind": "attempt_work",
            "call_id": call["id"],
            "rep_id": p["user_id"],
            "rep_work_incomplete": False,
            "rep_work_intervals": [
                {
                    "started_at": (began + timedelta(seconds=10)).isoformat(),
                    "ended_at": (began + timedelta(seconds=50)).isoformat(),
                }
            ],
        }
        if row:
            update(row, **value)
        else:
            put(s, "outcomes", p["tenant_id"], p["seller_id"], value, key=key, now=clock.now)
    project_pending(service, p)
    assert reader(service, p)["total"]["work_seconds"] == 50
    with service.db.transaction() as s:
        update(get(s, "call_attempts", call["id"]), outcome_source="llm_provisional")
    project_pending(service, p)
    after = reader(service, p)
    assert after["total"].get("human_known", 0) == 0
    assert after["total"].get("human_unknown", 0) == 1


def test_retry_statistics_follow_one_corrected_lead_without_history(workspace, app, clock, monkeypatch):
    reader = getattr(projections, "read_estimate_statistics", None)
    assert callable(reader), "Retry counts must be reversible lead contributions"
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    clock.now += timedelta(seconds=30)
    finish(workspace, call, outcome="no_answer")
    project_pending(service, p)
    stats = reader(service, p)["retry_observations"]
    assert sum(r["needs_retry_count"] for r in stats) == 1
    with service.db.transaction() as s:
        update(get(s, "leads", call["lead_id"]), outreach_hold={"kind": "human_booked"})
    project_pending(service, p)

    def no_history(*args, **kwargs):
        raise AssertionError("Reading estimates must not load operational history")

    monkeypatch.setattr(service, "operational_dataset", no_history)
    monkeypatch.setattr(service, "scoped", no_history)
    corrected = reader(service, p)["retry_observations"]
    assert sum(r["needs_retry_count"] for r in corrected) == 0
    assert sum(r["classified_count"] for r in corrected) == 1


def test_complete_call_cost_is_pooled_and_reverses_a_changed_receipt_kind(workspace, app):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    finish(workspace, call)
    with service.db.transaction() as s:
        hold = service._by_key(s, p, "usage_ledger", "call:" + call["id"])
        hold_id = hold.id
        update(hold, status="settled", cost_usd=0.25, reserved_usd=0, billing_verified=True)
    project_pending(service, p)
    statistics = projections.read_estimate_statistics(service, p)
    assert statistics.get("costs") == {
        "cost_usd": 0.25,
        "cost_observations": 1,
        "cost_unknown_observations": 0,
        "level": "pooled_complete_calls",
    }
    with service.db.transaction() as s:
        update(get(s, "usage_ledger", hold_id), kind="voice")
    project_pending(service, p)
    assert projections.read_estimate_statistics(service, p)["costs"]["cost_observations"] == 0


def test_saved_cell_estimate_discloses_pooled_cost_support():
    policy = models.EstimatePolicy()
    config = {"conversion_goal": "meeting_held", "handling_seconds": 120}
    counts = models.estimate_counts(example_rows())
    profile = {
        "goal": "meeting_held",
        "window_days": 14,
        "work_assumption_seconds": 120,
        "estimate_policy_version": policy.version,
        "statistics": {
            "total": counts,
            "cells": [],
            "cells_complete": True,
            "costs": {
                "cost_usd": 0.9,
                "cost_observations": 3,
                "cost_unknown_observations": 1,
                "level": "pooled_complete_calls",
            },
        },
    }
    estimate = models.saved_prediction(profile, {"segment": "new"}, config)
    assert estimate["expected_attempt_cost_usd"] == pytest.approx(0.3)
    assert estimate["cost_observations"] == 3 and estimate["cost_unknown_observations"] == 1
    assert estimate.get("cost_support_level") == "pooled_complete_calls"


def test_refresh_uses_saved_estimates_without_history_or_inline_forecast(workspace, app, clock, monkeypatch):
    from conftest import completed_report

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    clock.now += timedelta(seconds=30)
    finish(workspace, call)
    completed_report(workspace)
    original = service.scoped

    def no_history(session, table, who):
        assert table not in {
            "call_attempts",
            "feature_snapshots",
            "conversion_events",
            "leads",
            "experiment_assignments",
            "learning_datasets",
            "notes",
            "conversation_states",
            "outcomes",
            "usage_ledger",
        }, f"Routine refresh cannot scan {table}"
        return original(session, table, who)

    def no_forecast(*args, **kwargs):
        raise AssertionError("A maintenance tick must queue separate planning, not compute a whole horizon")

    monkeypatch.setattr(service, "scoped", no_history)
    monkeypatch.setattr(service, "forecast_input", no_forecast)
    service.refresh_optimization(p)
    with service.db.transaction(False) as s:
        profile = service.scoped(s, "campaigns", p)[0].data["selection_profile"]
        config = service.optimization_config(s, p)
    assert profile["statistics"]["total"]["human_known"] == 1
    assert models.saved_prediction(profile, {}, config)["expected_rep_seconds"] == pytest.approx(105)


def test_projected_treatment_rewards_keep_global_and_local_support(workspace, app, clock):
    from app.optimization.learning import distribution_from_counts

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    finish(workspace, call)
    clock.now += timedelta(days=15)
    project_pending(service, p)
    stats = projections.read_estimate_statistics(service, p)
    rewards = stats.get("treatment_counts")
    assert rewards is not None, "Finite treatments must retain corrected mature reward counts"
    with service.db.transaction(False) as s:
        action = get(s, "call_attempts", call["id"]).data.get("treatment_variant_id", "approved-default")
        feature = get(s, "feature_snapshots", call["feature_snapshot_id"]).data["features"]
    segment = feature.get("segment", "unknown")
    result = distribution_from_counts([action], rewards[segment])
    assert result["observations"][action] == 1
    assert result["estimates"][action] < 0.2
