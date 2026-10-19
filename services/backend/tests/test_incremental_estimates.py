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
