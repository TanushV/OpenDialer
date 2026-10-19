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


def test_call_projection_keeps_training_counts_without_retaining_training_rows(workspace, app):
    from test_projection_replay import project_pending, vectors

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    finish(workspace, call)
    project_pending(service, p)
    contribution = next(
        value
        for _, value in vectors(service, p, "metric_contributions").values()
        if value["source_kind"] == "call" and value["source_id"] == call["id"]
    )
    values = contribution["values"]
    assert values.get("dataset_rows") == 1
    assert values.get("dataset_pending_rows") == 1
    assert values.get("dataset_positive_conversions") == 0
    assert values.get("dataset_missing_snapshot_calls") == 0
    assert all(type(v) in (int, float) for v in values.values())
    assert not {"training_rows", "transcript", "observations", "features"} & contribution.keys()


def test_help_totals_correct_timing_and_quality_without_history(workspace, app, clock):
    from app.optimization import projections
    from test_projection_replay import project_pending

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    with service.db.transaction() as session:
        event = put(
            session,
            "coach_events",
            p["tenant_id"],
            p["seller_id"],
            {
                "call_id": call["id"],
                "rep_id": p["user_id"],
                "action": "SHOW_CARD",
                "latency": {"inference_ms": 700, "state_ms": 20, "retrieval_ms": 5},
            },
            now=clock.now,
        )
        feedback = put(
            session,
            "coaching_feedback",
            p["tenant_id"],
            p["seller_id"],
            {
                "call_id": call["id"],
                "coach_event_id": event.id,
                "rep_id": p["user_id"],
                "render_ms": 12,
                "event_to_render_ms": 80,
            },
            key=event.id + ":" + p["user_id"],
            now=clock.now,
        )
        feedback_id = feedback.id
    project_pending(service, p)
    actual = projections.read_coaching_metrics(service, p)
    assert actual["reviewed_cards"] == 0 and actual["useful_fraction"] is None
    assert actual["p95_render_ms"] == 12 and actual["p95_event_to_render_ms"] == 80
    assert 700 <= actual["p95_inference_ms"] <= 770
    assert actual["latency_support"]["inference_ms"] == 1
    assert actual["latency_quantization"]["upper_bounds"] is True
    with service.db.transaction() as session:
        update(get(session, "coaching_feedback", feedback_id), useful=False, render_ms=25)
    project_pending(service, p)
    actual = projections.read_coaching_metrics(service, p)
    assert actual["p95_render_ms"] == 25 and actual["latency_support"]["render_ms"] == 1
    assert actual["reviewed_cards"] == 1 and actual["useful_fraction"] == 0
    with service.db.transaction() as session:
        session.delete(get(session, "coaching_feedback", feedback_id))
    project_pending(service, p)
    actual = projections.read_coaching_metrics(service, p)
    assert actual["p95_render_ms"] is None and actual["reviewed_cards"] == 0


def test_help_quantiles_use_bounded_counts_include_unknown_and_overflow(clock):
    from app.optimization import projections

    total = {}
    for i in range(10000):
        event = {"action": "NO_ACTION", "latency": {"inference_ms": i + 0.5}}
        for key, value in projections.help_values(event, None, now=clock.now).items():
            total[key] = total.get(key, 0) + value
    report = projections.coaching_from_counts(total)
    assert len(total) < 250, "Latency distribution must not grow one key per unique duration"
    assert report["latency_support"]["inference_ms"] == 10000
    assert 9500 <= report["p95_inference_ms"] <= 10450
    assert report["p95_stt_ms"] is None and report["latency_support"]["stt_ms"] == 0
    overflow = projections.help_values({"latency": {"inference_ms": 1e9}}, None, now=clock.now)
    assert projections.coaching_from_counts(overflow)["p95_inference_ms"] is None


def test_dataset_summary_does_not_decrypt_saved_training_payloads(workspace, app):
    from app.optimization import projections
    from sqlalchemy import event

    service, p = app.state.service, principal(workspace)
    saved = service.save_operational_dataset(p)

    def reject_training_payload(connection, cursor, statement, parameters, context, many):
        assert "FROM learning_datasets" not in statement, "Summary metadata cannot reload training payloads"

    event.listen(service.db.engine, "before_cursor_execute", reject_training_payload)
    try:
        report = projections.read_dataset_metrics(service, p)
    finally:
        event.remove(service.db.engine, "before_cursor_execute", reject_training_payload)
    assert report["snapshots"][-1] == {
        "id": saved["id"],
        "rows": len(saved["rows"]),
        "as_of": saved["as_of"],
        "goal": saved["goal"],
    }
