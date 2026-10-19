"""E09/C036: consistent snapshots survive churn and irrelevant field changes."""

from app.db import update
from conftest import completed_report
from test_remediation_privacy import principal


def test_interrupted_inactive_rebuild_replays_concurrent_dirty_units(workspace, app, clock, carrier):
    import importlib.util

    from app.db import put
    from app.main import create_app
    from fastapi.testclient import TestClient

    service, p = app.state.service, principal(workspace)
    before = completed_report(workspace).json()
    module = "app.optimization.projections"
    assert importlib.util.find_spec(module), "Incremental projection coordinator contract is absent"
    from app.optimization.projections import rebuild_page

    with service.db.transaction() as s:
        sample = service.scoped(s, "leads", p)[0]
        lead_id = sample.id
        for i in range(501):
            put(
                s,
                "leads",
                p["tenant_id"],
                p["seller_id"],
                {**sample.data, "name": f"Replay {i}"},
                key=f"replay:{i}",
                now=clock.now,
            )
    first = rebuild_page(service, p, start=True)
    assert first["processed"] <= 500 and first["cursor"] and not first["published"]
    current = workspace["client"].get("/v1/optimization", headers=workspace["headers"]).json()
    assert current["metrics"] == before["metrics"]
    assert current["projection"]["definition_version"] == before["projection"]["definition_version"]
    with service.db.transaction() as s:
        put(
            s,
            "conversion_events",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead_id,
                "kind": "meeting_held",
                "status": "confirmed",
                "occurred_at": clock.now.isoformat(),
                "label_available_at": clock.now.isoformat(),
            },
            key="during-rebuild",
            now=clock.now,
        )
    restarted = create_app(service.settings, clock=clock, carrier=carrier)
    with TestClient(restarted) as client:
        for _ in range(20):
            page = rebuild_page(restarted.state.service, p)
            assert page["processed"] <= 500
            if page["published"]:
                break
        assert page["published"], "Resume must converge from the saved cursor and dirty watermark"
        final = client.get("/v1/optimization", headers=workspace["headers"]).json()
        assert final["metrics"]["imported_leads"] == 504
        assert final["metrics"]["unique_conversions"] == 1
        assert final["projection"]["source_watermark"] >= first["source_watermark"]


def test_rebuild_churn_and_atomic_publish(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    before = completed_report(workspace).json()
    # Preparation bookkeeping is not a metric input. Continuous refresh of this
    # field must not keep a coherent report permanently stale.
    for i in range(10):
        with service.db.transaction() as s:
            lead = service.scoped(s, "leads", p)[0]
            update(lead, preparation={"status": "pending", "progress": i})
        current = workspace["client"].get("/v1/optimization", headers=workspace["headers"]).json()
        assert current["projection"]["status"] == "ready"
        assert current["metrics"] == before["metrics"]
    assert current["projection"].get("definition_version")
    assert current["projection"].get("source_watermark") is not None


def test_rolled_back_tombstone_cannot_remove_a_live_contribution(workspace, app):
    service, p = app.state.service, principal(workspace)
    before = completed_report(workspace).json()
    with service.db.transaction() as s:
        row = service.scoped(s, "leads", p)[0]
        s.delete(row)
        s.flush()
        s.rollback()
    after = completed_report(workspace).json()
    assert after["metrics"] == before["metrics"]
    assert after["projection"]["source_revision"] == before["projection"]["source_revision"]


def dirty_jobs(service, p):
    from app.db import Job
    from sqlalchemy import select

    with service.db.transaction(False) as session:
        return {
            job.key: {
                "id": job.id,
                "payload": dict(job.payload),
                "status": job.status,
                "attempts": job.attempts,
                "lease_owner": job.lease_owner,
            }
            for job in session.scalars(
                select(Job).where(
                    Job.tenant_id == p["tenant_id"],
                    Job.seller_id == p["seller_id"],
                    Job.key.startswith("projection:"),
                )
            )
        }


def test_dirty_units_ignore_preparation_and_keep_one_monotonic_source_job(workspace, app):
    from app.db import get

    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as session:
        lead = service.scoped(session, "leads", p)[0]
        lead_id = lead.id
    key = f"projection:{p['seller_id']}:lead:{lead_id}"
    initial = dirty_jobs(service, p)
    assert key in initial, "The source mutation must durably enqueue its own unit"
    with service.db.transaction() as session:
        update(get(session, "leads", lead_id), preparation={"status": "pending", "progress": 9})
    assert dirty_jobs(service, p)[key] == initial[key]
    with service.db.transaction() as session:
        update(get(session, "leads", lead_id), status="needs_review")
    changed = dirty_jobs(service, p)[key]
    assert changed["id"] == initial[key]["id"]
    assert changed["payload"]["desired_generation"] == initial[key]["payload"]["desired_generation"] + 1
    assert changed["attempts"] == 0
    assert changed["payload"]["source_id"] == lead_id and changed["payload"]["source_kind"] == "lead"
    assert not {"phone", "name", "text", "transcript", "notes"} & changed["payload"].keys()
    with service.db.transaction() as session:
        update(get(session, "leads", lead_id), status="invalid")
        session.flush()
        session.rollback()
    assert dirty_jobs(service, p)[key] == changed


def test_deleted_source_keeps_a_scoped_tombstone_job_and_running_lease(workspace, app):
    from app.db import Job, get

    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as session:
        lead = service.scoped(session, "leads", p)[0]
        identity = lead.id
    key = f"projection:{p['seller_id']}:lead:{identity}"
    before = dirty_jobs(service, p)
    assert key in before
    with service.db.transaction() as session:
        job = session.get(Job, before[key]["id"])
        job.status = "running"
        job.lease_owner = "active-projection-worker"
        job.payload = {**job.payload, "claimed_generation": job.payload["desired_generation"]}
    with service.db.transaction() as session:
        session.delete(get(session, "leads", identity))
    after = dirty_jobs(service, p)[key]
    assert after["status"] == "running" and after["lease_owner"] == "active-projection-worker"
    assert after["payload"]["desired_generation"] > after["payload"]["claimed_generation"]
    assert after["payload"]["deleted"] is True
    assert after["payload"]["source_id"] == identity


def test_inplace_outcome_change_dirties_call_and_related_lead(workspace, app, clock):
    from app.db import get, put

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as session:
        lead = service.scoped(session, "leads", p)[0]
        row = put(
            session,
            "call_attempts",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead.id,
                "rep_id": p["user_id"],
                "campaign_id": workspace["campaign_id"],
                "started_at": clock.now.isoformat(),
                "submitted_at": clock.now.isoformat(),
                "state": "LIVE",
            },
            now=clock.now,
        )
        identity, lead_id = row.id, lead.id
    before = dirty_jobs(service, p)
    call_key = f"projection:{p['seller_id']}:call:{identity}"
    lead_key = f"projection:{p['seller_id']}:lead:{lead_id}"
    assert call_key in before and lead_key in before
    with service.db.transaction() as session:
        row = get(session, "call_attempts", identity)
        row.data["outcome"] = "right_party"
        row.data["outcome_available_at"] = clock.now.isoformat()
    after = dirty_jobs(service, p)
    for key in (call_key, lead_key):
        assert after[key]["payload"]["desired_generation"] > before[key]["payload"]["desired_generation"]
    assert any(j["payload"]["source_kind"] == "rep-day" for j in after.values())


def project_pending(service, p):
    from app.optimization.projections import refresh_unit

    for job in dirty_jobs(service, p).values():
        assert refresh_unit(service, p, job["payload"])["status"] in {"ready", "unchanged"}


def vectors(service, p, table):
    with service.db.transaction(False) as session:
        return {r.external_key: (r.version, dict(r.data)) for r in service.scoped(session, table, p)}


def test_source_replacement_duplicate_delete_and_atomic_rollback(workspace, app, clock, monkeypatch):
    from app.db import get, put
    from app.optimization import projections

    service, p = app.state.service, principal(workspace)
    project_pending(service, p)
    initial = vectors(service, p, "metric_contributions")
    assert sum(data["values"].get("original_cohort", 0) for _, data in initial.values()) == 3
    project_pending(service, p)
    assert vectors(service, p, "metric_contributions") == initial
    with service.db.transaction() as session:
        lead = service.scoped(session, "leads", p)[0]
        identity = lead.id
        put(
            session,
            "conversion_events",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": identity,
                "kind": "meeting_held",
                "status": "confirmed",
                "occurred_at": clock.now.isoformat(),
                "label_available_at": clock.now.isoformat(),
            },
            now=clock.now,
        )
    project_pending(service, p)
    changed = vectors(service, p, "metric_contributions")
    assert sum(d["values"].get("unique_conversions", 0) for _, d in changed.values()) == 1
    buckets = vectors(service, p, "metric_buckets")
    with service.db.transaction() as session:
        session.delete(get(session, "leads", identity))
    original = projections.delta_values

    def fail_after_delta(*args):
        original(*args)
        raise RuntimeError("synthetic failed bucket calculation")

    monkeypatch.setattr(projections, "delta_values", fail_after_delta)
    import pytest

    with pytest.raises(RuntimeError):
        project_pending(service, p)
    assert vectors(service, p, "metric_buckets") == buckets
    assert vectors(service, p, "metric_contributions") == changed
    monkeypatch.setattr(projections, "delta_values", original)
    project_pending(service, p)
    remaining = vectors(service, p, "metric_contributions")
    assert sum(d["values"].get("original_cohort", 0) for _, d in remaining.values()) == 2
    assert sum(d["values"].get("unique_conversions", 0) for _, d in remaining.values()) == 0


def test_dirty_generation_fences_change_during_calculation(workspace, app, monkeypatch):
    from app.db import get
    from app.optimization import projections

    service, p = app.state.service, principal(workspace)
    project_pending(service, p)
    with service.db.transaction() as session:
        lead = service.scoped(session, "leads", p)[0]
        identity = lead.id
        update(lead, status="needs_review")
    original = projections.lead_values

    def change_during_read(lead, *args, **kwargs):
        result = original(lead, *args, **kwargs)
        if lead["id"] == identity:
            with service.db.transaction() as session:
                update(get(session, "leads", identity), status="eligible")
        return result

    monkeypatch.setattr(projections, "lead_values", change_during_read)
    payload = dirty_jobs(service, p)[f"projection:{p['seller_id']}:lead:{identity}"]["payload"]
    before = vectors(service, p, "metric_contributions")
    assert projections.refresh_unit(service, p, payload)["status"] == "superseded"
    assert vectors(service, p, "metric_contributions") == before
    monkeypatch.setattr(projections, "lead_values", original)
    project_pending(service, p)
    assert (
        sum(
            d["values"].get("eligible_now", 0)
            for _, d in vectors(service, p, "metric_contributions").values()
        )
        == 3
    )


def test_due_maturity_and_cross_midnight_work_are_replaceable(workspace, app, clock):
    from datetime import timedelta

    from app.db import put
    from app.optimization.projections import refresh_due

    service, p = app.state.service, principal(workspace)
    midnight = clock.now.replace(hour=0, minute=0, second=0)
    with service.db.transaction() as session:
        lead = service.scoped(session, "leads", p)[0]
        put(
            session,
            "call_attempts",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead.id,
                "campaign_id": workspace["campaign_id"],
                "rep_id": p["user_id"],
                "started_at": (clock.now - timedelta(days=13)).isoformat(),
                "submitted_at": (clock.now - timedelta(days=13)).isoformat(),
                "ended_at": (clock.now - timedelta(days=13) + timedelta(minutes=1)).isoformat(),
                "state": "ENDED",
                "outcome": "no_answer",
                "outcome_available_at": clock.now.isoformat(),
            },
            now=clock.now,
        )
        for _ in range(2):
            put(
                session,
                "work_intervals",
                p["tenant_id"],
                p["seller_id"],
                {
                    "rep_id": p["user_id"],
                    "started_at": (midnight - timedelta(minutes=1)).isoformat(),
                    "ended_at": (midnight + timedelta(minutes=2)).isoformat(),
                },
                now=clock.now,
            )
    project_pending(service, p)
    values = [d["values"] for _, d in vectors(service, p, "metric_contributions").values()]
    assert sum(v.get("rep_work_seconds", 0) for v in values) == 240

    assert sum(v.get("pending_outcome_windows", 0) for v in values) == 1
    clock.now += timedelta(days=2)
    assert refresh_due(service, p, limit=100)["processed"] > 0
    values = [d["values"] for _, d in vectors(service, p, "metric_contributions").values()]
    assert sum(v.get("pending_outcome_windows", 0) for v in values) == 0
    assert sum(v.get("rep_work_seconds", 0) for v in values) == 240


def test_inplace_work_correction_dirties_old_and_new_days(workspace, app, clock):
    from datetime import timedelta

    from app.db import get, put

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as session:
        interval = put(
            session,
            "work_intervals",
            p["tenant_id"],
            p["seller_id"],
            {
                "rep_id": p["user_id"],
                "started_at": (clock.now - timedelta(hours=1)).isoformat(),
                "ended_at": clock.now.isoformat(),
            },
            now=clock.now,
        )
        identity = interval.id
    old_key = f"projection:{p['seller_id']}:rep-day:{p['user_id']}:{clock.now.date()}"
    before = dirty_jobs(service, p)[old_key]["payload"]["desired_generation"]
    with service.db.transaction() as session:
        interval = get(session, "work_intervals", identity)
        interval.data["started_at"] = (clock.now - timedelta(days=2, hours=1)).isoformat()
        interval.data["ended_at"] = (clock.now - timedelta(days=2)).isoformat()
    assert dirty_jobs(service, p)[old_key]["payload"]["desired_generation"] > before


def test_building_conflict_cannot_partially_commit_active_namespace(workspace, app, monkeypatch):
    from app.db import get
    from app.optimization import projections

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as session:
        campaign = get(session, "campaigns", workspace["campaign_id"])
        update(campaign, metric_projection={"active_namespace": "initial", "building_namespace": "building"})
    project_pending(service, p)
    with service.db.transaction() as session:
        lead = service.scoped(session, "leads", p)[0]
        identity = lead.id
        update(lead, status="needs_review")
    before = vectors(service, p, "metric_contributions")
    original = projections.lead_values

    def conflict(lead, *args, **kwargs):
        result = original(lead, *args, **kwargs)
        with service.db.transaction() as session:
            row = next(
                r
                for r in service.scoped(session, "metric_contributions", p)
                if r.data["namespace"] == "building" and r.data["source_id"] == identity
            )
            update(row, conflict_test=True)
        return result

    monkeypatch.setattr(projections, "lead_values", conflict)
    payload = dirty_jobs(service, p)[f"projection:{p['seller_id']}:lead:{identity}"]["payload"]
    assert projections.refresh_unit(service, p, payload)["status"] == "superseded"
    after = vectors(service, p, "metric_contributions")
    assert {k: v for k, v in after.items() if v[1]["namespace"] == "initial"} == {
        k: v for k, v in before.items() if v[1]["namespace"] == "initial"
    }


def test_changed_namespace_and_expired_worker_cannot_publish(workspace, app, clock, monkeypatch):
    from datetime import timedelta

    from app.db import Job, get
    from app.optimization import projections

    service, p = app.state.service, principal(workspace)
    project_pending(service, p)
    with service.db.transaction() as session:
        lead = service.scoped(session, "leads", p)[0]
        identity = lead.id
        update(lead, status="needs_review")
    pending = dirty_jobs(service, p)[f"projection:{p['seller_id']}:lead:{identity}"]
    with service.db.transaction() as session:
        job = session.get(Job, pending["id"])
        job.status, job.lease_owner = "running", "old-worker"
        job.lease_expires_at = clock.now - timedelta(seconds=1)
    payload = {**pending["payload"], "job_id": pending["id"], "lease_owner": "old-worker"}
    before = vectors(service, p, "metric_contributions")
    assert projections.refresh_unit(service, p, payload)["status"] == "lost_lease"
    assert vectors(service, p, "metric_contributions") == before
    original = projections.lead_values

    def switch(lead, *args, **kwargs):
        result = original(lead, *args, **kwargs)
        with service.db.transaction() as session:
            update(
                get(session, "campaigns", workspace["campaign_id"]),
                metric_projection={"active_namespace": "replacement"},
            )
        return result

    monkeypatch.setattr(projections, "lead_values", switch)
    assert projections.refresh_unit(service, p, pending["payload"])["status"] == "superseded"
    assert vectors(service, p, "metric_contributions") == before


def test_one_batch_coalesces_many_dirty_ids_without_per_unit_reads(workspace, app):
    from app.optimization.projections import enqueue_units
    from sqlalchemy import event

    service, p = app.state.service, principal(workspace)
    reads = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "jobs" in statement:
            reads.append(statement)

    event.listen(service.db.engine, "before_cursor_execute", record)
    try:
        units = {(p["tenant_id"], p["seller_id"], "lead", f"bulk-{i}"): False for i in range(100)}
        for _ in range(2):
            with service.db.transaction() as session:
                enqueue_units(session, units)
        assert len(reads) <= 2, "One indexed job lookup per100 changed source IDs"
    finally:
        event.remove(service.db.engine, "before_cursor_execute", record)


def test_suppression_fanout_is_content_free_scoped_and_resumable(workspace, app, clock):
    from app.db import put

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as session:
        sample = service.scoped(session, "leads", p)[0]
        phone, endpoint = sample.data["phone"], sample.data["endpoint_id"]
        session.info["defer_flush"] = True
        for i in range(204):
            put(
                session,
                "leads",
                p["tenant_id"],
                p["seller_id"],
                dict(sample.data),
                key=f"fanout-{i}",
                now=clock.now,
            )
        put(session, "leads", p["tenant_id"], "other-seller", dict(sample.data), now=clock.now)
    before = dirty_jobs(service, p)
    with service.db.transaction() as session:
        put(
            session,
            "suppressions",
            p["tenant_id"],
            p["seller_id"],
            {"phone": phone, "active": True},
            key=phone,
            now=clock.now,
        )
    pending = [j for j in dirty_jobs(service, p).values() if j["payload"]["source_kind"] == "endpoint-leads"]
    assert len(pending) == 1, (
        "One denial schedules bounded dependent work, not all lead writes in the denial transaction"
    )
    payload = pending[0]["payload"]
    assert payload["source_id"] == endpoint and phone not in str(payload)
    from app.optimization.projections import expand_dependents

    pages = []
    for _ in range(4):
        result = expand_dependents(service, p, payload)
        pages.append(result["processed"])
        assert result["processed"] <= 100
        if result["status"] == "ready":
            break
    assert sum(pages) == 205 and result["status"] == "ready"
    after = dirty_jobs(service, p)
    changed = [
        j
        for k, j in after.items()
        if j["payload"]["source_kind"] == "lead"
        and j["payload"]["desired_generation"] > before[k]["payload"]["desired_generation"]
    ]
    assert len(changed) == 205


def test_late_conversion_dirties_related_attempt_units(workspace, app, clock):
    from app.db import put

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as session:
        lead = service.scoped(session, "leads", p)[0]
        put(
            session,
            "call_attempts",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead.id,
                "submitted_at": clock.now.isoformat(),
                "started_at": clock.now.isoformat(),
                "state": "ENDED",
                "outcome": "no_answer",
            },
            now=clock.now,
        )
    before = dirty_jobs(service, p)
    with service.db.transaction() as session:
        put(
            session,
            "conversion_events",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead.id,
                "kind": "meeting_held",
                "status": "confirmed",
                "occurred_at": clock.now.isoformat(),
            },
            now=clock.now,
        )
    pending = [j for j in dirty_jobs(service, p).values() if j["payload"]["source_kind"] == "lead-calls"]
    assert len(pending) == 1
    from app.optimization.projections import expand_dependents

    assert expand_dependents(service, p, pending[0]["payload"])["processed"] == 1
    changed = dirty_jobs(service, p)
    assert all(
        j["payload"]["desired_generation"] > before[k]["payload"]["desired_generation"]
        for k, j in changed.items()
        if j["payload"]["source_kind"] == "call"
    )
