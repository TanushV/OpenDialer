"""E09: reporting must consume contribution state, including secondary summaries."""

from datetime import timedelta

import pytest

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


def test_drift_sufficient_statistics_preserve_paired_strata_and_missingness():
    from app.optimization import learning

    def period(human, hours):
        counts = {}
        for hour in hours:
            vector = learning.drift_values(
                {
                    "features": {"local_hour": hour},
                    "human": human,
                    "p_human": 0.8,
                    "right_party": None,
                    "conversion": None,
                }
            )
            for key, value in vector.items():
                counts[key] = counts.get(key, 0) + value
        return counts

    key = ("model-v2", "campaign", "meeting_held", 2, "segment", 0)
    ref = {key: period(1, [10] * 20), None: {"rows": 3}}
    recent = {key: period(0, [None] * 20), ("unpaired",): {"rows": 7}}
    report = learning.drift_from_counts(ref, recent)
    assert report["reference_rows"] == 23 and report["recent_rows"] == 27
    assert report["excluded_reference"] == 3 and report["excluded_recent"] == 7
    assert report["stage_support"]["human"]["paired_support"] == 20
    assert report["reference_brier"] == pytest.approx(0.04)
    assert report["recent_brier"] == pytest.approx(0.64)
    assert report["drift_detected"] and report["missingness_shift"]
    assert report["features"]["local_hour"]["reference_mean"] == 10
    assert report["features"]["local_hour"]["recent_missing"] == 20
    assert report["stage_support"]["right_party"]["paired_support"] == 0


def test_drift_projection_corrects_and_expires_without_source_reads(workspace, app, clock):
    from app.optimization import projections
    from sqlalchemy import event
    from test_projection_replay import project_pending

    service, p = app.state.service, principal(workspace)
    calls = []
    with service.db.transaction() as session:
        lead = service.scoped(session, "leads", p)[0]
        for days, model, outcome in (
            (8, "model-v2", "right_party"),
            (1, "model-v2", "no_answer"),
            (1, None, "no_answer"),
        ):
            at = (clock.now - timedelta(days=days)).isoformat()
            snapshot = put(
                session,
                "feature_snapshots",
                p["tenant_id"],
                p["seller_id"],
                {
                    "lead_id": lead.id,
                    "as_of": at,
                    "features": {
                        "feature_schema_version": 2,
                        "segment": "s",
                        "attempt_count": 0,
                        "local_hour": 10,
                    },
                    "predictions": {"p_human": 0.8, "model_version": model},
                },
                now=clock.now,
            )
            call = put(
                session,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {
                    "lead_id": lead.id,
                    "campaign_id": workspace["campaign_id"],
                    "rep_id": p["user_id"],
                    "started_at": at,
                    "submitted_at": at,
                    "ended_at": at,
                    "state": "FINALIZED",
                    "outcome": outcome,
                    "outcome_available_at": at,
                    "outcome_source": "rep_reported",
                    "feature_snapshot_id": snapshot.id,
                },
                now=clock.now,
            )
            calls.append(call.id)
    project_pending(service, p)

    def read():
        def reject(connection, cursor, statement, parameters, context, many):
            assert not any(
                "FROM " + name in statement
                for name in ("call_attempts", "feature_snapshots", "conversion_events")
            )

        event.listen(service.db.engine, "before_cursor_execute", reject)
        try:
            return projections.read_drift_metrics(service, p)
        finally:
            event.remove(service.db.engine, "before_cursor_execute", reject)

    report = read()
    assert report["reference_rows"] == 1 and report["recent_rows"] == 2
    assert report["excluded_recent"] == 1
    assert report["stage_support"]["human"]["recent"] == 1
    limited = projections.read_drift_metrics(service, p, limit=1)
    assert limited["complete"] is False and limited["drift_detected"] is None
    assert limited["reference_rows"] is None and limited["recent_rows"] is None
    with service.db.transaction() as session:
        update(get(session, "call_attempts", calls[1]), outcome="temporary_failure")
    project_pending(service, p)
    assert read()["stage_support"]["human"]["recent"] == 0
    clock.now += timedelta(days=8)
    projections.refresh_due(service, p)
    project_pending(service, p)
    assert read()["reference_rows"] == 3 and read()["recent_rows"] == 0
    clock.now += timedelta(days=28)
    projections.refresh_due(service, p)
    project_pending(service, p)
    report = read()
    assert report["reference_rows"] == report["recent_rows"] == 0


def test_trial_count_reducer_preserves_matured_and_original_denominators(clock):
    from app.optimization import learning

    assigned = (clock.now - timedelta(days=15)).isoformat()
    pending = (clock.now - timedelta(days=1)).isoformat()
    records = [
        {"lead_id": "a", "arm": "control", "assigned_at": assigned, "covered": True, "policy_unknown": 0},
        {"lead_id": "b", "arm": "candidate", "assigned_at": assigned, "covered": True, "policy_unknown": 0},
        {"lead_id": "c", "arm": "candidate", "assigned_at": pending, "covered": False, "policy_unknown": 1},
    ]
    events = [
        {"lead_id": "b", "kind": "meeting_held", "occurred_at": (clock.now - timedelta(days=2)).isoformat()}
    ]
    counts = {}
    for assignment in records:
        for key, value in learning.trial_values(
            assignment, events, now=clock.now, goal="meeting_held", window_days=14
        ).items():
            counts[key] = counts.get(key, 0) + value
    report = learning.trial_from_counts(counts, goal="meeting_held", window_days=14)
    assert report["arms"]["candidate"]["leads"] == 2
    assert report["arms"]["candidate"]["matured_leads"] == 1
    assert report["arms"]["candidate"]["rate"] == 1
    assert report["arms"]["control"]["rate"] == 0
    assert report["pending_leads"] == report["policy_unknown"] == 1
    assert not report["sufficient_evidence"]


def test_trial_projection_reads_original_cohort_and_later_correction(workspace, app, clock):
    from app.optimization import projections
    from sqlalchemy import event
    from test_projection_replay import project_pending

    service, p = app.state.service, principal(workspace)
    initial = completed_report(workspace).json()
    call = start(workspace)
    finish(workspace, call)
    with service.db.transaction() as s:
        conversion = put(
            s,
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
        identity = conversion.id
    project_pending(service, p)

    def read():
        def reject(connection, cursor, statement, parameters, context, many):
            assert not any(
                "FROM " + table in statement
                for table in (
                    "leads",
                    "call_attempts",
                    "experiment_assignments",
                    "conversion_events",
                    "feature_snapshots",
                    "compliance_decisions",
                )
            )

        event.listen(service.db.engine, "before_cursor_execute", reject)
        try:
            return projections.read_trial_metrics(service, p, epoch=initial["trial"]["epoch"])
        finally:
            event.remove(service.db.engine, "before_cursor_execute", reject)

    actual = read()
    assert sum(a["leads"] for a in actual["arms"].values()) == 3
    assert sum(a["conversions"] for a in actual["arms"].values()) == 1
    assert actual["pending_leads"] == 3
    clock.now += timedelta(days=15)
    projections.refresh_due(service, p)
    project_pending(service, p)
    actual = read()
    assert actual["pending_leads"] == 0
    assert sum(a["matured_conversions"] for a in actual["arms"].values()) == 1
    with service.db.transaction() as s:
        update(get(s, "conversion_events", identity), retracted=True, retracted_at=clock.now.isoformat())
    project_pending(service, p)
    assert sum(a["conversions"] for a in read()["arms"].values()) == 0


def test_trial_study_metadata_change_refreshes_enrolled_leads(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    previous = completed_report(workspace).json()
    assert previous["trial"]["trial_model_id"] is None
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p)
        update(
            campaign,
            optimization_trial={
                "model_id": "reviewed-test-model",
                "starts_at": clock.now.isoformat(),
                "ends_at": (clock.now + timedelta(days=1)).isoformat(),
                "max_leads": 100,
            },
        )
    current = completed_report(workspace).json()
    assert current["trial"]["trial_model_id"] == "reviewed-test-model"
    assert current["trial"]["epoch"] == previous["trial"]["epoch"]
    assert sum(a["leads"] for a in current["trial"]["arms"].values()) == 3
