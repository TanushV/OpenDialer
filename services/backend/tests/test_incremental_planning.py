"""E09: changed work reuses the common allocator and already saved capacity."""

from datetime import timedelta

import pytest
from app.db import get, update
from app.optimization import capacity
from test_remediation_privacy import principal
from test_time_capacity import NOW, lead, number, window


def changed_plan(leads, fixed=(), **overrides):
    fn = getattr(capacity, "plan_changed", None)
    assert callable(fn), "Changed leads need bounded reuse of existing allocations"
    options = {
        "now": NOW,
        "horizon_hours": 1,
        "rep_minutes": 60,
        "handling_seconds": 120,
        "policy": {"max_attempts": 3, "local_start_hour": 9, "local_end_hour": 20},
        "staffed_windows": [window(minutes=60)],
        "inventory": [number(daily_limit=2)],
        "fixed_schedule": fixed,
    }
    options.update(overrides)
    return fn(leads=leads, **options)


def test_changed_allocation_conserves_shared_number_rep_and_endpoint():
    first = changed_plan([lead("first")])["existing_schedule"]
    assert len(first) == 1
    second = changed_plan([lead("second"), lead("third")], first)
    assert len(second["existing_schedule"]) == 1
    assert second["existing_schedule"][0]["at"] >= first[0]["end_at"]
    assert not changed_plan([lead("first")], first)["existing_schedule"]
    full = first + second["existing_schedule"]
    assert not changed_plan([lead("fourth")], full)["existing_schedule"]


def test_changed_work_respects_fixed_account_occupancy_and_work_budget():
    fixed = changed_plan([lead("first")])["existing_schedule"]
    result = changed_plan(
        [lead("second")],
        fixed,
        rep_minutes=2,
        inventory=[number(daily_limit=100, capacity=2)],
    )
    assert result["existing_schedule"] == []
    assert result["planning_limited"] is False


def test_changed_page_is_bounded_and_reports_search_limit():
    with pytest.raises(ValueError, match="100"):
        changed_plan([lead(str(i)) for i in range(101)])
    with pytest.raises(ValueError, match="10000"):
        changed_plan([], [{}] * 10001)


def test_changed_lead_refresh_preserves_other_allocations_without_full_forecast(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        rows = service.scoped(s, "leads", p)
        identities = [r.id for r in rows]
        for r in rows:
            update(r, timezone="UTC")
    service.refresh_schedule_plans(p, workspace["campaign_id"])
    with service.db.transaction(False) as s:
        others = {
            r.id: (r.version, dict(r.data))
            for r in service.scoped(s, "scheduling_plans", p)
            if r.data["lead_id"] != identities[0]
        }
    with service.db.transaction() as s:
        update(get(s, "leads", identities[0]), outreach_hold={"kind": "human_booked"})

    def no_forecast(*args, **kwargs):
        raise AssertionError("One changed lead cannot compute a whole-list proposal")

    monkeypatch.setattr("app.optimization.workflows.capacity_forecast", no_forecast)
    result = service.refresh_schedule_plans(p, workspace["campaign_id"], lead_ids=[identities[0]])
    assert result["saved"] == 1
    with service.db.transaction(False) as s:
        changed = service._by_key(s, p, "scheduling_plans", "current:" + identities[0])
        assert changed.data["status"] == "unavailable" and changed.data["selected"] is None
        for identity, (version, value) in others.items():
            row = get(s, "scheduling_plans", identity)
            assert row.version == version and row.data == value


def test_unchanged_refresh_does_not_queue_another_whole_list_plan(workspace, app, clock):
    from test_projection_replay import project_pending

    service, p = app.state.service, principal(workspace)
    project_pending(service, p)
    first = service.refresh_optimization(p)
    assert all(j["kind"] != "retry_plan_refresh" for j in first["jobs"])
    clock.now += timedelta(seconds=60)
    second = service.refresh_optimization(p)
    assert second["jobs"] == first["jobs"]


@pytest.mark.asyncio
async def test_dirty_lead_worker_updates_only_that_plan(workspace, app, monkeypatch):
    from test_projection_replay import project_pending

    service, p = app.state.service, principal(workspace)
    project_pending(service, p)
    service.refresh_schedule_plans(p, workspace["campaign_id"])
    project_pending(service, p)
    with service.db.transaction() as s:
        chosen = service.scoped(s, "leads", p)[0]
        identity = chosen.id
        update(chosen, outreach_hold={"kind": "human_booked"})
    # Report/readiness catch-up can complete metric jobs before the normal
    # runner sees them; the separate planning obligation must survive that path.
    service.prepare_metric_read(p)

    def no_whole_list(*args, **kwargs):
        raise AssertionError("Dirty work cannot recalculate a whole-list proposal")

    monkeypatch.setattr("app.optimization.workflows.capacity_forecast", no_whole_list)
    await service.run_jobs(p["tenant_id"], ["ml"])
    with service.db.transaction(False) as s:
        row = service._by_key(s, p, "scheduling_plans", "current:" + identity)
        assert row.data["status"] == "unavailable"
        assert row.data["source_lead_version"] == get(s, "leads", identity).version


def test_explicit_forecast_publishes_schedule_from_one_calculation(workspace, app, monkeypatch):
    import app.optimization.workflows as workflows

    service, p = app.state.service, principal(workspace)
    original, calls = workflows.capacity_forecast, []

    def once(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(workflows, "capacity_forecast", once)
    result = service.build_forecast(p, workspace["campaign_id"])
    assert result["status"] == "ready" and len(calls) == 1
    with service.db.transaction(False) as s:
        assert len(service.scoped(s, "scheduling_plans", p)) == 3


def test_changed_planning_coalesces_bounded_pages_before_reports(workspace, app, clock):
    from app.db import Job, put
    from sqlalchemy import select
    from test_projection_replay import project_pending

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        sample = service.scoped(s, "leads", p)[0]
        for i in range(101):
            put(
                s,
                "leads",
                p["tenant_id"],
                p["seller_id"],
                {**sample.data, "name": f"Bounded page {i}"},
                now=clock.now,
            )
        wanted = {r.id for r in service.scoped(s, "leads", p)}
    project_pending(service, p)
    with service.db.transaction(False) as s:
        jobs = [
            j
            for j in s.scalars(select(Job).where(Job.queue == "ml", Job.status == "queued"))
            if j.payload.get("kind") == "retry_plan_refresh"
        ]
        assert len(jobs) == 2, "104 changed leads need two pages, not 104 calculations"
        assert all(1 <= len(j.payload["lead_ids"]) <= 100 and j.priority > 10 for j in jobs)
        assert {i for j in jobs for i in j.payload["lead_ids"]} == wanted


@pytest.mark.asyncio
async def test_planning_lease_expiring_during_calculation_cannot_publish(workspace, app, clock, monkeypatch):
    from test_projection_replay import project_pending

    service, p = app.state.service, principal(workspace)
    project_pending(service, p)
    original = capacity.plan_changed

    def expired(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.now += timedelta(seconds=61)
        return result

    monkeypatch.setattr(capacity, "plan_changed", expired)
    await service.run_jobs(p["tenant_id"], ["ml"])
    with service.db.transaction(False) as s:
        assert service.scoped(s, "scheduling_plans", p) == []


def constrained_workspace(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        for i, row in enumerate(service.scoped(s, "number_inventory", p)):
            update(
                row,
                lifecycle="healthy" if i == 0 else "retired_callback_only",
                daily_limit=1,
                region="general",
            )
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p)
        update(
            campaign,
            optimization={**campaign.data.get("optimization", {}), "horizon_hours": 1, "rep_minutes": 60},
        )
        for row in service.scoped(s, "leads", p):
            update(row, timezone="UTC")
    service.refresh_schedule_plans(p, workspace["campaign_id"])
    return service, p


def test_full_reallocation_replaces_an_unchanged_leads_old_slot(workspace, app, clock):
    service, p = constrained_workspace(workspace, app)
    with service.db.transaction() as s:
        plans = service.scoped(s, "scheduling_plans", p)
        assert sum(bool(row.data.get("selected")) for row in plans) == 1
        promoted = next(row.data["lead_id"] for row in plans if not row.data.get("selected"))
        update(get(s, "leads", promoted), callback_due_at=clock.now.isoformat())
    service.refresh_schedule_plans(p, workspace["campaign_id"])
    with service.db.transaction(False) as s:
        selected = {
            row.data["lead_id"]
            for row in service.scoped(s, "scheduling_plans", p)
            if row.data.get("selected")
        }
        assert selected == {promoted}, "The old winner must relinquish its capacity after reallocation"


def test_changed_priority_work_requests_rebalance_only_when_saved_work_blocks_it():
    fixed = changed_plan([lead("cold")], inventory=[number(daily_limit=1)])["existing_schedule"]
    callback = lead("promised", callback_due_at=NOW.isoformat())
    result = changed_plan([callback], fixed, inventory=[number(daily_limit=1)])
    assert result["existing_schedule"] == []
    assert result.get("rebalance_required") is True
    assert not changed_plan([lead("cold2")], fixed).get("rebalance_required")
    assert not changed_plan([callback], (), inventory=[number(daily_limit=1)]).get("rebalance_required")


@pytest.mark.asyncio
async def test_blocked_callback_queues_one_internal_rebalance_and_replaces_cold_plan(workspace, app, clock):
    from app.db import Job
    from sqlalchemy import select
    from test_projection_replay import project_pending

    service, p = constrained_workspace(workspace, app)
    project_pending(service, p)
    with service.db.transaction() as s:
        plans = service.scoped(s, "scheduling_plans", p)
        promoted = next(row.data["lead_id"] for row in plans if not row.data.get("selected"))
        update(get(s, "leads", promoted), callback_due_at=clock.now.isoformat())
    internal = {**p, "user_id": None}
    result = service.refresh_schedule_plans(
        internal, workspace["campaign_id"], lead_ids=[promoted], internal=True
    )
    assert result.get("rebalance_job_id"), "Blocked priority work must retain a background obligation"
    again = service.refresh_schedule_plans(
        internal, workspace["campaign_id"], lead_ids=[promoted], internal=True
    )
    assert again["rebalance_job_id"] == result["rebalance_job_id"]
    for _ in range(8):
        await service.run_jobs(p["tenant_id"], ["ml"])
    with service.db.transaction(False) as s:
        jobs = list(s.scalars(select(Job).where(Job.id == result["rebalance_job_id"])))
        assert len(jobs) == 1 and jobs[0].status == "completed"
        selected = {
            row.data["lead_id"]
            for row in service.scoped(s, "scheduling_plans", p)
            if row.data.get("selected")
        }
        assert selected == {promoted}
