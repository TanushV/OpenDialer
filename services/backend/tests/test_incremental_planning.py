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
