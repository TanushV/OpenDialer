"""E09/W5b: changed work refreshes one bounded whole-list summary; no per-lead plan rows.

The former changed-page allocator (plan_changed) and its per-lead scheduling_plans
publication were removed: every forecast already reallocates the whole list, and the
queue never consumed those rows. These tests keep the surviving obligations.
"""

from datetime import timedelta

import pytest
from app.db import get, update
from test_remediation_privacy import principal


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
async def test_dirty_lead_legacy_jobs_complete_without_whole_list_work_or_plan_rows(
    workspace, app, monkeypatch
):
    from app.db import Job
    from sqlalchemy import select
    from test_projection_replay import project_pending

    service, p = app.state.service, principal(workspace)
    project_pending(service, p)
    with service.db.transaction() as s:
        chosen = service.scoped(s, "leads", p)[0]
        update(chosen, outreach_hold={"kind": "human_booked"})
    service.prepare_metric_read(p)

    def no_whole_list(*args, **kwargs):
        raise AssertionError("Dirty work cannot recalculate a whole-list proposal")

    monkeypatch.setattr("app.optimization.workflows.capacity_forecast", no_whole_list)
    await service.run_jobs(p["tenant_id"], ["ml"])
    with service.db.transaction(False) as s:
        assert service.scoped(s, "scheduling_plans", p) == []
        pending = [
            j
            for j in s.scalars(select(Job).where(Job.queue == "ml"))
            if j.payload.get("kind") == "retry_plan_refresh" and j.status not in {"completed"}
        ]
        assert not [j for j in pending if j.status == "dead"]


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
        assert service.scoped(s, "scheduling_plans", p) == []
    assert service.optimization_report(p)["plans"]


def test_changed_leads_queue_report_units_but_no_retired_plan_pages(workspace, app, clock):
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
        units = {
            j.payload["source_id"]
            for j in s.scalars(select(Job).where(Job.key.startswith(f"projection:{p['seller_id']}:lead:")))
        }
    # Per-lead plans are retired (W5b): changed leads refresh their report units only.
    assert jobs == []
    assert wanted <= units


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
    service.build_forecast(p, workspace["campaign_id"])
    return service, p


def test_full_reallocation_replaces_an_unchanged_leads_old_slot(workspace, app, clock):
    service, p = constrained_workspace(workspace, app)
    first = service.optimization_report(p)["plans"]
    assert len(first) == 1
    with service.db.transaction(False) as s:
        promoted = next(r.id for r in service.scoped(s, "leads", p) if r.id != first[0]["lead_id"])
    with service.db.transaction() as s:
        update(get(s, "leads", promoted), callback_due_at=clock.now.isoformat())
    service.build_forecast(p, workspace["campaign_id"])
    selected = {row["lead_id"] for row in service.optimization_report(p)["plans"]}
    assert selected == {promoted}, "The old winner must relinquish its capacity after reallocation"
