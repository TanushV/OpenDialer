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
    reports = [j for j in jobs if j.payload["kind"] == "report_refresh"]
    sources = [j for j in jobs if j.payload["kind"] == "metric_refresh"]
    assert len(reports) == 1
    assert len(sources) == len({j.payload["source_id"] for j in sources}) == 3
    assert all(j.payload["source_kind"] == "lead" for j in sources)
    assert len(jobs) == len(reports) + len(sources)
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


def test_changed_comparison_definition_invalidates_saved_report(workspace, app):
    from conftest import completed_report

    service, p = app.state.service, principal(workspace)
    before = completed_report(workspace).json()
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p)
        update(campaign, controlled_comparisons=[{"id": "new-definition", "status": "stopped"}])
    after = read(workspace)
    assert after["projection"]["status"] == "stale"
    assert after["projection"]["current_revision"] > before["projection"]["current_revision"]


@pytest.mark.asyncio
async def test_source_change_marks_previous_report_stale_until_worker_publishes(workspace, app, clock):
    from conftest import completed_report

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
    current = completed_report(workspace).json()
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


def test_savepoint_rollback_does_not_suppress_later_invalidation(workspace, app):
    service, p = app.state.service, principal(workspace)
    service.refresh_optimization(p)
    with service.db.transaction() as s:
        point = s.begin_nested()
        update(service.scoped(s, "leads", p)[0], status="needs_review")
        s.flush()
        point.rollback()
        update(service.scoped(s, "leads", p)[0], status="needs_review")
    assert read(workspace)["projection"]["status"] == "stale"


def test_repeated_changes_coalesce_one_pending_calculation(workspace, app):
    service, p = app.state.service, principal(workspace)
    identities = set()
    for index in range(6):
        with service.db.transaction() as s:
            update(service.scoped(s, "leads", p)[0], name=f"Synthetic correction {index}")
        identities.add(read(workspace)["projection"]["job_id"])
    assert len(identities) == 1


@pytest.mark.asyncio
async def test_superseded_worker_retries_instead_of_claiming_completion(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    first = read(workspace)
    original = service.optimization_report

    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        with service.db.transaction() as s:
            lead = service.scoped(s, "leads", p)[0]
            update(lead, status="eligible" if lead.data["status"] == "needs_review" else "needs_review")
        return result

    with monkeypatch.context() as m:
        m.setattr(service, "optimization_report", changing)
        await service.run_jobs(queues=["ml"])
    job = service.jobs.get(first["projection"]["job_id"])
    assert job["status"] == "queued"
    assert job["error"] is None and job["attempts"] == 0, (
        "A concurrent source correction is fresh work, not a failed attempt with ten-second backoff"
    )
    await service.run_jobs(queues=["ml"])
    assert read(workspace)["projection"]["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"disabled": True}, {"role": "rep"}])
async def test_revoked_admin_cannot_run_queued_full_refresh(workspace, app, monkeypatch, change):
    service, p = app.state.service, principal(workspace)
    job = service.request_optimization_refresh(p)
    with service.db.transaction() as s:
        update(get(s, "users", p["user_id"]), **change)
    performed = []
    monkeypatch.setattr(service, "refresh_optimization", lambda *args: performed.append(True))
    await service.run_jobs(queues=["ml"])
    assert not performed
    assert service.jobs.get(job["job_id"])["status"] != "completed"


def test_role_change_during_calculation_cannot_publish_old_scope(workspace, app, monkeypatch):
    from fastapi import HTTPException

    service, p = app.state.service, principal(workspace)
    original = service.optimization_report

    def revoked(*args, **kwargs):
        result = original(*args, **kwargs)
        with service.db.transaction() as s:
            update(get(s, "users", p["user_id"]), disabled=True)
        return result

    monkeypatch.setattr(service, "optimization_report", revoked)
    with pytest.raises(HTTPException) as exc:
        service.rebuild_optimization_report(p)
    assert exc.value.status_code == 403
    with service.db.transaction(False) as s:
        assert service.scoped(s, "report_projections", p) == []


def test_stale_report_retains_its_calculation_definition(workspace, app):
    service, p = app.state.service, principal(workspace)
    service.refresh_optimization(p)
    before = read(workspace)
    response = workspace["client"].put(
        "/v1/admin/optimization", headers=workspace["headers"], json={"rep_minutes": 1}
    )
    assert response.status_code == 200
    after = read(workspace)
    assert after["settings"]["rep_minutes"] == 1
    assert after["projection"]["calculated_settings"] == before["settings"]
    assert after["projection"]["status"] == "stale"


def test_concurrent_report_views_share_the_same_job(workspace, app):
    from concurrent.futures import ThreadPoolExecutor

    service, p = app.state.service, principal(workspace)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: service.cached_optimization_report(p), range(6)))
    assert len({r["projection"]["job_id"] for r in results}) == 1


def test_historical_cohort_is_loaded_through_a_separate_projection(workspace, app):
    from conftest import completed_report

    service, p = app.state.service, principal(workspace)
    service.refresh_optimization(p)
    epoch = read(workspace)["trial"]["epoch"]
    changed = workspace["client"].put(
        "/v1/admin/optimization", headers=workspace["headers"], json={"conversion_goal": "sale_won"}
    )
    assert changed.status_code == 200
    assert service.rebuild_optimization_report(p, epoch)["status"] == "pending"
    past = completed_report(workspace, params={"epoch": epoch}).json()
    assert past["trial"]["epoch"] == epoch
    assert past["trial"]["goal"] == "meeting_held"
    assert past["projection"]["status"] == "ready"
    assert service.cached_optimization_report(p)["projection"]["status"] == "pending"


def test_backups_exclude_rebuildable_reports(workspace, app):
    import json

    service, p = app.state.service, principal(workspace)
    service.refresh_optimization(p)
    receipt = service.backup(p)
    path = service.settings.data_dir / "backups" / (receipt["backup_id"] + ".backup")
    payload = json.loads(service.vault.cipher.decrypt(path.read_bytes()))
    assert not payload["tables"].get("report_projections")


def test_backup_from_installed_schema_restores_without_new_cache_tables(workspace, app):
    import json

    service, p = app.state.service, principal(workspace)
    service.refresh_optimization(p)
    receipt = service.backup(p)
    path = service.settings.data_dir / "backups" / (receipt["backup_id"] + ".backup")
    payload = json.loads(service.vault.cipher.decrypt(path.read_bytes()))
    before_ids = {r["id"] for r in payload["tables"]["leads"]}
    payload["tables"].pop("report_projections", None)
    payload["tables"].pop("work_intervals", None)
    path.write_bytes(service.vault.cipher.encrypt(json.dumps(payload).encode()))
    assert service.restore(p, receipt["backup_id"])["restored"]
    with service.db.transaction(False) as s:
        assert {r.id for r in service.scoped(s, "leads", p)} == before_ids
        assert not service.scoped(s, "report_projections", p)
    assert service.cached_optimization_report(p)["projection"]["status"] == "pending"


def test_incomplete_backup_cannot_delete_required_source_tables(workspace, app):
    import json

    from fastapi import HTTPException

    service, p = app.state.service, principal(workspace)
    receipt = service.backup(p)
    path = service.settings.data_dir / "backups" / (receipt["backup_id"] + ".backup")
    payload = json.loads(service.vault.cipher.decrypt(path.read_bytes()))
    payload["tables"].pop("leads")
    path.write_bytes(service.vault.cipher.encrypt(json.dumps(payload).encode()))
    with pytest.raises(HTTPException) as exc:
        service.restore(p, receipt["backup_id"])
    assert exc.value.status_code == 422
    with service.db.transaction(False) as s:
        assert len(service.scoped(s, "leads", p)) == 3
