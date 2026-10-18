"""A50/A51: report views read saved evidence; calculation belongs to bounded jobs."""

from datetime import timedelta

import pytest
from app.db import Job, get, put, update
from sqlalchemy import select
from test_context_reads import count_decrypted
from test_remediation_privacy import principal


def read(workspace):
    result = workspace["client"].get("/v1/optimization", headers=workspace["headers"])
    assert result.status_code == 200, result.text
    return result.json()


def no_calculation(*args, **kwargs):
    raise AssertionError("Report view reconstructed historical analysis")


def test_saved_report_does_not_rebuild_history_on_view(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    service.refresh_optimization(p)
    before = read(workspace)
    monkeypatch.setattr(service, "operational_dataset", no_calculation)
    monkeypatch.setattr(service, "forecast", no_calculation)
    monkeypatch.setattr(service, "inventory_evidence", no_calculation)
    decrypted = count_decrypted(app, monkeypatch)
    after = read(workspace)
    assert after["metrics"] == before["metrics"]
    assert after["as_of"] == before["as_of"]
    assert after["projection"]["status"] == "ready"
    assert len(decrypted) <= 15


@pytest.mark.asyncio
async def test_first_view_is_explicitly_pending_and_coalesces_background_work(workspace, app, monkeypatch):
    service = app.state.service
    with monkeypatch.context() as m:
        m.setattr(service, "operational_dataset", no_calculation)
        for _ in range(3):
            result = read(workspace)
            assert result["projection"]["status"] == "pending"
            assert result.get("metrics") is None
    with service.db.transaction(False) as s:
        jobs = list(s.scalars(select(Job).where(Job.queue == "ml")))
    assert len(jobs) == 1
    assert jobs[0].payload["kind"] == "report_refresh"
    await service.run_jobs(queues=["ml"])
    result = read(workspace)
    assert result["projection"]["status"] == "ready"
    assert result["metrics"]["imported_leads"] == 3


def test_refresh_http_request_only_queues_work(workspace, app, monkeypatch):
    monkeypatch.setattr(app.state.service, "refresh_optimization", no_calculation)
    response = workspace["client"].post("/v1/admin/optimization/refresh", headers=workspace["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    assert response.json()["job_id"]


@pytest.mark.asyncio
async def test_source_change_marks_previous_report_stale_until_worker_publishes(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    service.refresh_optimization(p)
    previous = read(workspace)
    with service.db.transaction() as s:
        lead = service.scoped(s, "leads", p)[0]
        put(
            s,
            "conversion_events",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead.id,
                "kind": "meeting_held",
                "occurred_at": clock.now.isoformat(),
                "observed_at": clock.now.isoformat(),
                "label_available_at": clock.now.isoformat(),
                "status": "confirmed",
                "retracted": False,
            },
            now=clock.now,
        )
    stale = read(workspace)
    assert stale["projection"]["status"] == "stale"
    assert stale["metrics"] == previous["metrics"]
    assert stale["projection"]["current_revision"] > stale["projection"]["source_revision"]
    await service.run_jobs(queues=["ml"])
    current = read(workspace)
    assert current["projection"]["status"] == "ready"
    assert current["metrics"]["unique_conversions"] == 1


def test_clock_expiry_does_not_relabel_old_report_as_current(workspace, app, clock):
    app.state.service.refresh_optimization(principal(workspace))
    previous = read(workspace)
    clock.now += timedelta(minutes=11)
    after = read(workspace)
    assert after["as_of"] == previous["as_of"]
    assert after["projection"]["status"] == "stale"


def test_rollback_and_another_seller_do_not_invalidate_saved_report(workspace, app):
    service, p = app.state.service, principal(workspace)
    service.refresh_optimization(p)
    before = read(workspace)
    with service.db.transaction() as s:
        lead = service.scoped(s, "leads", p)[0]
        update(lead, status="needs_review")
        s.flush()
        s.rollback()
    with service.db.transaction() as s:
        put(s, "leads", p["tenant_id"], "unrelated-seller", {"status": "eligible"})
    after = read(workspace)
    assert after["projection"]["status"] == "ready"
    assert after["projection"]["source_revision"] == before["projection"]["source_revision"]


def test_recreated_service_uses_the_persisted_report(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    service.refresh_optimization(p)
    newer = type(service)(service.db, service.settings, service.clock)
    monkeypatch.setattr(newer, "operational_dataset", no_calculation)
    assert newer.cached_optimization_report(p)["projection"]["status"] == "ready"


@pytest.mark.asyncio
async def test_report_failure_preserves_old_evidence_and_does_not_queue_on_every_read(
    workspace, app, clock, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    service.refresh_optimization(p)
    before = read(workspace)
    clock.now += timedelta(minutes=11)
    read(workspace)
    monkeypatch.setattr(service, "optimization_report", no_calculation)
    await service.run_jobs(queues=["ml"])
    later = [read(workspace) for _ in range(3)]
    assert all(item["metrics"] == before["metrics"] for item in later)
    assert len({item["projection"]["job_id"] for item in later}) == 1
    assert later[0]["projection"]["refresh_error"]


def test_concurrent_source_change_cannot_publish_a_falsely_fresh_report(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    service.refresh_optimization(p)
    previous = read(workspace)
    original = service.optimization_report

    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        with service.db.transaction() as s:
            lead = service.scoped(s, "leads", p)[0]
            update(lead, status="needs_review")
        return result

    monkeypatch.setattr(service, "optimization_report", changing)
    assert service.rebuild_optimization_report(p)["status"] == "superseded"
    current = read(workspace)
    assert current["projection"]["status"] == "stale"
    assert current["metrics"] == previous["metrics"]


def test_role_downgrade_does_not_reuse_privileged_report(workspace, app):
    service, p = app.state.service, principal(workspace)
    service.refresh_optimization(p)
    with service.db.transaction() as s:
        update(get(s, "users", p["user_id"]), role="rep")
    result = service.cached_optimization_report({**p, "role": "rep"})
    assert result["projection"]["status"] == "pending"
    assert result.get("decisions") is None
