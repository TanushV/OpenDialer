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
