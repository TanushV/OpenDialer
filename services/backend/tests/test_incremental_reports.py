"""E09: reporting must consume contribution state, including secondary summaries."""

from datetime import timedelta

from app.db import get, put, update
from conftest import completed_report, start
from test_optimization_workflows import finish
from test_remediation_privacy import principal


def test_report_publication_reads_bucket_metrics_not_source_history(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    finish(workspace, call)
    with service.db.transaction() as session:
        put(
            session,
            "conversion_events",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": call["lead_id"],
                "call_id": call["id"],
                "kind": "meeting_held",
                "status": "confirmed",
                "occurred_at": clock.now.isoformat(),
                "label_available_at": clock.now.isoformat(),
            },
            now=clock.now,
        )
    expected = completed_report(workspace).json()
    assert expected["dataset"]["rows"] == expected["dataset"]["positive_conversions"] == 1
    assert expected["dataset"]["pending_rows"] == 0
    original = service.scoped
    historical = {
        "leads",
        "call_attempts",
        "feature_snapshots",
        "conversion_events",
        "coaching_feedback",
        "coach_events",
        "experiment_assignments",
        "compliance_decisions",
        "policy_decisions",
        "scheduling_plans",
        "learning_datasets",
    }

    def bounded(session, kind, who):
        assert kind not in historical, f"Report publication cannot scan all {kind}"
        return original(session, kind, who)

    monkeypatch.setattr(service, "scoped", bounded)

    def no_dataset(*args, **kwargs):
        raise AssertionError("Operational publication must not build a training dataset")

    monkeypatch.setattr(service, "operational_dataset", no_dataset)
    actual = service.optimization_report(p)
    for key in ("metrics", "dataset", "trial", "coaching", "drift", "comparisons"):
        assert actual[key] == expected[key]


def test_ordinary_refresh_never_creates_an_immutable_training_dataset(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    called = []

    def forbidden(*args, **kwargs):
        called.append(True)
        raise AssertionError("Routine maintenance cannot save a historical training dataset")

    monkeypatch.setattr(service, "save_operational_dataset", forbidden)
    service.refresh_optimization(p)
    assert not called


def test_incremental_dataset_corrects_attribution_and_pending_window(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    finish(workspace, call)
    report = completed_report(workspace).json()
    assert report["dataset"]["rows"] == report["dataset"]["pending_rows"] == 1
    with service.db.transaction() as session:
        event = put(
            session,
            "conversion_events",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": call["lead_id"],
                "call_id": call["id"],
                "kind": "meeting_held",
                "occurred_at": clock.now.isoformat(),
                "label_available_at": clock.now.isoformat(),
                "status": "confirmed",
            },
            now=clock.now,
        )
        event_id = event.id
    report = completed_report(workspace).json()["dataset"]
    assert report["rows"] == report["positive_conversions"] == 1
    assert report["pending_rows"] == 0
    with service.db.transaction() as session:
        update(
            get(session, "conversion_events", event_id), retracted=True, retracted_at=clock.now.isoformat()
        )
    report = completed_report(workspace).json()["dataset"]
    assert report["positive_conversions"] == 0 and report["pending_rows"] == 1
    clock.now += timedelta(days=15)
    login = workspace["client"].post(
        "/v1/auth/session",
        json={
            "email": "operator@example.test",
            "password": "a-test-only-password-123",
        },
    )
    login.raise_for_status()
    headers = {"Authorization": "Bearer " + login.json()["access_token"]}
    report = completed_report(workspace, headers=headers).json()["dataset"]
    assert report["matured_rows"] == 1 and report["pending_rows"] == 0
    assert report["positive_conversions"] == 0
