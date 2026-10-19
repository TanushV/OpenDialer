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
