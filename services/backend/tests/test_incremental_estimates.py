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


def test_explicit_snapshot_bounds_attempts_before_loading_related_content(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    finish(workspace, call)
    identities = []
    with service.db.transaction() as s:
        original = dict(get(s, "call_attempts", call["id"]).data)
        for i in range(9):
            row = put(
                s,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {
                    **original,
                    "started_at": (clock.now + timedelta(seconds=i + 1)).isoformat(),
                    "submitted_at": (clock.now + timedelta(seconds=i + 1)).isoformat(),
                },
                now=clock.now,
            )
            identities.append(row.id)
    clock.now += timedelta(minutes=1)

    def no_history(*args, **kwargs):
        raise AssertionError("Even explicit training must bound records before decrypting history")

    monkeypatch.setattr(service, "scoped", no_history)
    with service.db.transaction(False) as s:
        actual = service.operational_dataset(s, p, limit=2)
    assert {r["call_id"] for r in actual} == set(identities[-2:])


def test_explicit_learning_snapshot_is_outside_writer_and_reuses_one_dataset(workspace, app, monkeypatch):
    from contextlib import contextmanager

    service, p = app.state.service, principal(workspace)
    learn = getattr(service, "refresh_learning", None)
    assert callable(learn), "Dataset/association work needs its separate bounded job"
    transaction, original = service.db.transaction, service.operational_dataset
    writes, snapshots = [], []

    @contextmanager
    def traced(write=True):
        writes.append(write)
        try:
            with transaction(write) as s:
                yield s
        finally:
            writes.pop()

    def observed(*args, **kwargs):
        assert not any(writes), "Training snapshot construction held an operational writer"
        snapshots.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(service.db, "transaction", traced)
    monkeypatch.setattr(service, "operational_dataset", observed)
    result = learn(p)
    assert len(snapshots) == 1
    assert result["training"]["status"] == "insufficient_data"
    with service.db.transaction(False) as s:
        campaign = service.scoped(s, "campaigns", p)[0]
        assert campaign.data["conversation_report"]["patterns"] == []


def test_attributed_usage_index_backfills_without_rewriting_the_receipt(workspace, app, clock):
    from app.db import MODELS
    from sqlalchemy import inspect
    from sqlalchemy import update as sql_update

    service, p = app.state.service, principal(workspace)
    model = MODELS["usage_ledger"]
    assert hasattr(model, "op_call_id"), "Explicit learning needs an indexed attributed receipt lookup"
    with service.db.transaction() as s:
        record = put(
            s,
            "usage_ledger",
            p["tenant_id"],
            p["seller_id"],
            {
                "kind": "voice",
                "object_id": "synthetic-call",
                "status": "settled",
                "cost_usd": 0.05,
            },
            now=clock.now,
        )
        identity, original, version = record.id, dict(record.data), record.version
    with service.db.transaction() as s:
        s.execute(sql_update(model).where(model.id == identity).values(op_index_version=3, op_call_id=None))
    service.db.backfill_operational_columns()
    with service.db.transaction(False) as s:
        row = get(s, "usage_ledger", identity)
        assert row.op_call_id == "synthetic-call" and row.op_index_version == 4
        assert row.data == original and row.version == version
    indexes = {
        row["name"]: row["column_names"] for row in inspect(service.db.engine).get_indexes("usage_ledger")
    }
    assert indexes["ix_usage_ledger_call"] == ["tenant_id", "seller_id", "op_call_id"]


def test_unchanged_ticks_and_changed_counts_keep_one_pending_learning_job(workspace, app, clock):
    from app.db import Job
    from conftest import completed_report
    from sqlalchemy import select

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    finish(workspace, call)
    completed_report(workspace)
    service.refresh_optimization(p)

    def learning_jobs():
        with service.db.transaction(False) as s:
            return [
                (j.id, j.payload.get("desired_generation"))
                for j in s.scalars(
                    select(Job).where(
                        Job.tenant_id == p["tenant_id"],
                        Job.key.startswith("operational-train:snapshot:", autoescape=True),
                    )
                )
            ]

    first = learning_jobs()
    assert len(first) == 1
    clock.now += timedelta(seconds=30)
    service.refresh_optimization(p)
    assert learning_jobs() == first, "Passing time alone must not create another historical learning job"
    with service.db.transaction() as s:
        update(get(s, "call_attempts", call["id"]), outcome="gatekeeper")
    project_pending(service, p)
    service.refresh_optimization(p)
    updated = learning_jobs()
    assert len(updated) == 1 and updated[0][0] == first[0][0]
    assert updated[0][1] > first[0][1]


def test_published_future_slots_keep_conditional_estimates(workspace, app, clock):
    from zoneinfo import ZoneInfo

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        lead = service.scoped(s, "leads", p)[0]
        local = clock.now.astimezone(ZoneInfo(lead.data.get("timezone") or "UTC"))
        row = {
            "segment": lead.data.get("segment", "unknown"),
            "local_hour": local.hour,
            "local_weekday": local.weekday(),
            "attempt_count": 0,
            "outcome": "right_party",
            "rep_work_seconds": 60,
        }
        statistics = {
            "total": models.estimate_counts([row]),
            "cells": [
                {
                    "features": {
                        k: row[k] for k in ("segment", "local_hour", "local_weekday", "attempt_count")
                    },
                    "counts": models.estimate_counts([row]),
                }
            ],
            "cells_complete": True,
            "retry_observations": [],
            "treatment_counts": {},
        }
        config = service.optimization_config(s, p)
        profile = service.selection_profile(s, p, statistics, config)
        campaign = get(s, "campaigns", workspace["campaign_id"])
        update(campaign, selection_profile=profile)
    assert service.build_forecast(p, workspace["campaign_id"])["status"] == "ready"
    predictions = [r["estimate"] for r in service.optimization_report(p)["plans"]]
    assert predictions, "The fixture must exercise actual permitted future slots"
    assert any(
        v["observations"] == 1 and v["estimate_level"] == "segment_time_attempt" for v in predictions
    ), "The saved summary silently lost its conditional estimates"


def test_changed_source_during_dataset_read_publishes_the_as_of_snapshot(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    finish(workspace, call)
    original = service.operational_dataset

    def changed(*args, **kwargs):
        data = original(*args, **kwargs)
        with service.db.transaction() as s:
            update(get(s, "call_attempts", call["id"]), outcome="gatekeeper")
        return data

    monkeypatch.setattr(service, "operational_dataset", changed)
    # The change belongs to the next dataset; this one is immutable and as of its read.
    dataset = service.save_operational_dataset(p)
    row = next(r for r in dataset["rows"] if r["call_id"] == call["id"])
    assert row["outcome"] == "right_party"
    with service.db.transaction(False) as s:
        assert dataset["source_revision"] < service.report_revision(s, p)
        assert [d.id for d in service.scoped(s, "learning_datasets", p)] == [dataset["id"]]
    monkeypatch.setattr(service, "operational_dataset", original)
    assert service.save_operational_dataset(p)["id"] != dataset["id"]
    assert service.proposal(p, workspace["campaign_id"], capacity=1000)["projection"]
