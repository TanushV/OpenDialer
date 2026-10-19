"""E09: source attribution, effective time and authorization boundaries."""

from datetime import timedelta

import pytest
from app.db import get, put, update
from app.optimization.projections import read_estimate_statistics
from conftest import start
from fastapi import HTTPException
from test_optimization_workflows import finish
from test_projection_replay import project_pending, vectors
from test_remediation_privacy import principal


def test_retagged_work_source_reverses_old_contribution(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    began = clock.now
    clock.now += timedelta(seconds=30)
    finish(workspace, call)
    with service.db.transaction() as s:
        record = service._by_key(s, p, "outcomes", "attempt-work:" + call["id"])
        if not record:
            record = put(
                s,
                "outcomes",
                p["tenant_id"],
                p["seller_id"],
                {
                    "kind": "attempt_work",
                    "call_id": call["id"],
                    "rep_id": p["user_id"],
                },
                key="attempt-work:" + call["id"],
                now=clock.now,
            )
        identity = record.id
        update(
            record,
            rep_work_intervals=[
                {
                    "started_at": began.isoformat(),
                    "ended_at": (began + timedelta(seconds=60)).isoformat(),
                }
            ],
            rep_work_incomplete=False,
        )
    clock.now += timedelta(seconds=30)
    project_pending(service, p)
    assert read_estimate_statistics(service, p)["total"]["work_seconds"] == 60
    with service.db.transaction() as s:
        update(get(s, "outcomes", identity), kind="retired_attempt_work")
    project_pending(service, p)
    assert read_estimate_statistics(service, p)["total"]["work_seconds"] == 30


def test_verified_submission_before_recorded_start_counts_both_utc_days(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    midnight = clock.now.replace(hour=0, minute=0, second=0, microsecond=0)
    with service.db.transaction() as s:
        first = service.scoped(s, "leads", p)[0]
        put(
            s,
            "call_attempts",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": first.id,
                "campaign_id": workspace["campaign_id"],
                "rep_id": p["user_id"],
                "started_at": (midnight + timedelta(seconds=10)).isoformat(),
                "submitted_at": (midnight - timedelta(seconds=30)).isoformat(),
                "ended_at": (midnight + timedelta(seconds=30)).isoformat(),
                "state": "ENDED",
                "outcome": "no_answer",
                "outcome_available_at": clock.now.isoformat(),
            },
            now=clock.now,
        )
    project_pending(service, p)
    contributions = [d for _, d in vectors(service, p, "metric_contributions").values()]
    assert sum(r["values"].get("rep_work_seconds", 0) for r in contributions) == 60


def test_learning_worker_checks_owner_before_persisting_a_dataset(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        update(get(s, "users", p["user_id"]), disabled=True)
    with pytest.raises(HTTPException) as error:
        service.refresh_learning(p)
    assert error.value.status_code == 403
    with service.db.transaction(False) as s:
        assert service.scoped(s, "learning_datasets", p) == []


def test_campaign_specific_goal_cannot_pollute_another_campaign_estimate(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        first = service.required(s, "campaigns", workspace["campaign_id"], p)
        second = put(
            s,
            "campaigns",
            p["tenant_id"],
            p["seller_id"],
            {
                **first.data,
                "name": "Separate campaign",
                "optimization": {"conversion_goal": "sale_won", "window_days": 1},
            },
            now=clock.now + timedelta(microseconds=1),
        )
        campaign_id = second.id
        sample = service.scoped(s, "leads", p)[0]
        person = put(
            s,
            "leads",
            p["tenant_id"],
            p["seller_id"],
            {
                **sample.data,
                "campaign_id": second.id,
            },
            now=clock.now,
        )
        call = put(
            s,
            "call_attempts",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": person.id,
                "campaign_id": second.id,
                "rep_id": p["user_id"],
                "started_at": (clock.now - timedelta(days=2)).isoformat(),
                "submitted_at": (clock.now - timedelta(days=2)).isoformat(),
                "ended_at": (clock.now - timedelta(days=2) + timedelta(seconds=30)).isoformat(),
                "state": "ENDED",
                "outcome": "sale_won",
                "outcome_available_at": clock.now.isoformat(),
                "contact_role": "right_party",
                "outcome_source": "human",
            },
            now=clock.now,
        )
        feature = put(
            s,
            "feature_snapshots",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": person.id,
                "as_of": (clock.now - timedelta(days=2)).isoformat(),
                "features": {"segment": "separate", "local_hour": 9, "local_weekday": 0, "attempt_count": 0},
            },
            now=clock.now,
        )
        update(call, feature_snapshot_id=feature.id)
    project_pending(service, p)
    assert read_estimate_statistics(service, p)["total"].get("human_known", 0) == 0
    selected = read_estimate_statistics(service, {**p, "campaign_id": campaign_id})["total"]
    assert selected["human_known"] == 1 and selected["conversion_successes"] == 1
