"""E09: reporting must consume contribution state, including secondary summaries."""

from datetime import timedelta

import pytest
from app.db import get, put, update
from conftest import completed_report, start
from test_optimization_workflows import finish
from test_remediation_privacy import principal


def test_observed_call_cost_uses_only_complete_attributed_receipts(workspace, app, clock):
    from app.optimization import projections
    from test_projection_replay import project_pending

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    finish(workspace, call)

    def measured():
        project_pending(service, p)
        return projections.read_metrics(service, p)

    pending = measured()
    assert pending.get("observed_cost_usd") is None
    assert pending.get("observed_cost_calls") == 0
    assert pending.get("unknown_cost_calls") == 1
    with service.db.transaction() as s:
        hold = service._by_key(s, p, "usage_ledger", "call:" + call["id"])
        hold_id = hold.id
        update(hold, status="settled", cost_usd=0, reserved_usd=0, billing_verified=True)
        voice = put(
            s,
            "usage_ledger",
            p["tenant_id"],
            p["seller_id"],
            {
                "call_id": call["id"],
                "kind": "voice",
                "status": "settled",
                "cost_usd": 0.20,
                "credited_usd": 0.05,
                "billing_verified": True,
            },
            now=clock.now,
        )
        voice_id = voice.id
        # A workspace charge without this call's identity must not inflate its observed cost.
        put(
            s,
            "usage_ledger",
            p["tenant_id"],
            p["seller_id"],
            {"kind": "research", "status": "settled", "cost_usd": 9, "billing_verified": True},
            now=clock.now,
        )
    complete = measured()
    assert complete["observed_cost_usd"] == pytest.approx(0.15)
    assert complete["observed_cost_calls"] == 1 and complete["unknown_cost_calls"] == 0
    with service.db.transaction() as s:
        uncertainty = put(
            s,
            "usage_ledger",
            p["tenant_id"],
            p["seller_id"],
            {
                "object_id": call["id"],
                "kind": "live_coach",
                "status": "uncertain",
                "cost_usd": None,
                "reserved_usd": 0.03,
            },
            now=clock.now,
        )
        uncertain_id = uncertainty.id
    assert measured()["observed_cost_usd"] is None
    with service.db.transaction() as s:
        update(get(s, "usage_ledger", uncertain_id), status="released", reserved_usd=0)
        update(get(s, "usage_ledger", voice_id), credited_usd=0.10)
    assert measured()["observed_cost_usd"] == pytest.approx(0.10)
    # Losing the checked complete-set receipt makes cost unknown, never verified zero.
    with service.db.transaction() as s:
        s.delete(get(s, "usage_ledger", hold_id))
    assert measured()["unknown_cost_calls"] == 1
    with service.db.transaction() as s:
        # Remove training eligibility; usage remains real workspace spend.
        update(get(s, "call_attempts", call["id"]), feature_snapshot_id=None)
    result = measured()
    assert result["observed_cost_calls"] == result["unknown_cost_calls"] == 0
    assert result["gross_cost_usd"] >= 9.2


def test_call_cost_contributions_rollback_and_replay(workspace, app, clock, monkeypatch):
    from app.optimization import projections
    from test_projection_replay import project_pending, vectors

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    finish(workspace, call)
    with service.db.transaction() as s:
        row = service._by_key(s, p, "usage_ledger", "call:" + call["id"])
        usage_id = row.id
        update(row, status="settled", cost_usd=0.25, reserved_usd=0, billing_verified=True)
    project_pending(service, p)
    assert projections.read_metrics(service, p).get("observed_cost_usd") == 0.25
    before = vectors(service, p, "metric_buckets")
    with service.db.transaction() as s:
        update(get(s, "usage_ledger", usage_id), credited_usd=0.05)
    original = projections.update

    def crash(row, **values):
        if row.__tablename__ == "metric_contributions":
            raise RuntimeError("Synthetic interruption after bucket replacement")
        return original(row, **values)

    monkeypatch.setattr(projections, "update", crash)
    payload = {"source_kind": "usage", "source_id": usage_id}
    with pytest.raises(RuntimeError, match="Synthetic interruption"):
        projections.refresh_unit(service, p, payload)
    assert vectors(service, p, "metric_buckets") == before
    monkeypatch.setattr(projections, "update", original)
    project_pending(service, p)
    assert projections.read_metrics(service, p)["observed_cost_usd"] == 0.20
    again = vectors(service, p, "metric_buckets")
    projections.refresh_unit(service, p, payload)
    assert vectors(service, p, "metric_buckets") == again


def test_comparison_report_after_goal_change_preserves_original_trial_definition(workspace, app, clock):
    """The UI's save-goal / refresh / start-comparison sequence stays publishable."""
    from uuid import uuid4

    service, p = app.state.service, principal(workspace)
    original = completed_report(workspace).json()
    client, headers = workspace["client"], workspace["headers"]
    changed = client.put(
        "/v1/admin/optimization",
        headers=headers,
        json={"conversion_goal": "sale_won", "window_days": 30},
    )
    assert changed.status_code == 200, changed.text
    service.refresh_optimization(p)
    current = completed_report(workspace).json()
    assert current["trial"]["epoch"] != original["trial"]["epoch"]
    clock.now += timedelta(seconds=1)
    started = client.post(
        "/v1/admin/optimization/comparisons",
        headers=headers,
        json={
            "family": "coaching",
            "request_id": str(uuid4()),
            "review_reference": "Synthetic protocol review",
            "duration_days": 14,
        },
    )
    assert started.status_code == 200, started.text
    # A direct calculation exposes the exception instead of masking it as a queued retry.
    report = service.optimization_report(p)
    assert report["comparisons"][0]["id"] == started.json()["id"]
    report = completed_report(workspace).json()
    assert report["comparisons"][0]["id"] == started.json()["id"]


def test_report_uses_saved_forecast_with_its_own_freshness(workspace, app, clock, monkeypatch):
    from test_projection_replay import project_pending

    service, p = app.state.service, principal(workspace)
    project_pending(service, p)

    def no_planning(*args, **kwargs):
        raise AssertionError("Report publication must not calculate the horizon")

    monkeypatch.setattr(service, "forecast", no_planning)
    pending = service.optimization_report(p)["forecast"]
    assert pending["projection"]["status"] == "pending"
    assert pending["capacity_shortfall"] is None and pending["regions"] == []
    with service.db.transaction() as s:
        campaign = service.scoped(s, "campaigns", p)[0]
        put(
            s,
            "report_projections",
            p["tenant_id"],
            p["seller_id"],
            {
                "campaign_id": campaign.id,
                "source_revision": service.report_revision(s, p),
                "as_of": clock.now.isoformat(),
                "expires_at": (clock.now + timedelta(seconds=60)).isoformat(),
                "report": {
                    "regions": [],
                    "capacity_shortfall": 3,
                    "staffing_shortfall": 2,
                    "number_shortfall": 1,
                    "input_fingerprint": "synthetic-saved-forecast",
                },
            },
            key=f"planning:{campaign.id}:configured",
            now=clock.now,
        )
    saved = service.optimization_report(p)["forecast"]
    assert saved["projection"]["status"] == "ready" and saved["capacity_shortfall"] == 3
    clock.now += timedelta(seconds=61)
    expired = service.optimization_report(p)["forecast"]
    assert expired["projection"]["status"] == "stale"
    assert expired["input_fingerprint"] == saved["input_fingerprint"]


def test_report_decisions_follow_scoped_reservation_identities(workspace, app, clock):
    from app.db import uid
    from test_projection_replay import project_pending

    service, p = app.state.service, principal(workspace)
    own, other = [], []
    with service.db.transaction() as s:
        for i in range(36):
            owner = p["user_id"] if i in (0, 20) else uid()
            decision = put(
                s,
                "policy_decisions",
                p["tenant_id"],
                p["seller_id"],
                {"rep_id": owner, "lead_id": "synthetic", "action": "control"},
                now=clock.now + timedelta(microseconds=i),
            )
            put(
                s,
                "reservations",
                p["tenant_id"],
                p["seller_id"],
                {"rep_id": owner, "policy_decision_id": decision.id, "status": "expired"},
                now=clock.now + timedelta(microseconds=i),
            )
            (own if owner == p["user_id"] else other).append(decision.id)
    project_pending(service, p)
    admin = service.optimization_report(p)
    assert len(admin["decisions"]) == 30
    assert admin["detail_coverage"]["decisions_limited"] is True
    rep = service.optimization_report({**p, "role": "rep"})
    assert [d["id"] for d in rep["decisions"]] == own
    assert not rep["detail_coverage"]["decisions_limited"]


def test_report_uses_only_current_versioned_plans(workspace, app, clock):
    # Plans are one bounded forecast summary; stale lead versions, holds and expiry are filtered.
    from test_projection_replay import project_pending

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        leads = service.scoped(s, "leads", p)
        rows = [
            {
                "lead_id": lead.id,
                "lead_version": lead.version - (i == 1),
                "at": clock.now.isoformat(),
                "reason": "fixture",
                "local_hour": 9,
            }
            for i, lead in enumerate(leads[:3])
        ]
        update(leads[2], outreach_hold={"kind": "human_booked"})
        rows[2]["lead_version"] = leads[2].version
        put(
            s,
            "report_projections",
            p["tenant_id"],
            p["seller_id"],
            {
                "report": {
                    "next_attempts": rows,
                    "next_attempts_expire_at": (clock.now + timedelta(minutes=10)).isoformat(),
                },
                "campaign_id": workspace["campaign_id"],
            },
            key=f"planning:{workspace['campaign_id']}:configured",
            now=clock.now,
        )
        expected_id = leads[0].id
    project_pending(service, p)
    report = service.optimization_report(p)
    assert [v["lead_id"] for v in report["future_slots"]] == [expected_id]
    assert len(report["plans"]) == 3
    assert report["detail_coverage"]["plans_inspected"] == 3
    clock.now += timedelta(minutes=11)
    assert service.optimization_report(p)["future_slots"] == []


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
    known = (clock.now - timedelta(days=2)).isoformat()
    events = [{"lead_id": "b", "kind": "meeting_held", "occurred_at": known, "recorded_at": known}]
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


def test_health_counts_keep_residual_uncertainty_and_missing_labels(clock):
    from app.optimization import numbering
    from app.optimization.api import HealthPolicy

    policy = HealthPolicy().model_dump()
    at = (clock.now - timedelta(days=1)).isoformat()
    rows = [
        {
            "started_at": at,
            "outcome": "no_answer",
            "outcome_source": "rep",
            "outcome_available_at": at,
            "expected_human": 0.8,
            "prediction_at": at,
            "feature_schema_version": 2,
            "connected_seconds": 0,
        }
        for _ in range(40)
    ]
    rows += [{"started_at": at, "outcome": "unclassified", "expected_human": 0.8}]
    counts = {}
    for row in rows:
        for key, value in numbering.health_values(row, now=clock.now, policy=policy).items():
            counts[key] = counts.get(key, 0) + value
    result = numbering.health_from_counts(counts, now=clock.now, policy=policy, comparison_strata=1)
    assert result["samples"] == 41 and result["human_classified_samples"] == 40
    assert result["human_unknown_samples"] == result["excluded_comparison_samples"] == 1
    assert result["comparable_samples"] == 40
    assert result["expected_human_answers"] == pytest.approx(32)
    assert result["case_mix_residual_mean"] == pytest.approx(-0.8)
    assert result["case_mix_interval"] == pytest.approx([-0.8, -0.8], abs=1e-7)
    assert result["short_connected_calls"] == result["connected_duration_samples"] == 40
    assert result["delivery_failures"] == 0
    assessed = numbering.assess_number_health(result, {"as_of": clock.now.isoformat()}, None, policy)
    assert assessed["status"] == "investigating"
    assert "case_mix_pickup_drop" in assessed["reason_codes"]
    assert assessed["spam_label_confirmed"] is False


def test_number_health_projection_corrects_and_expires_without_call_history(workspace, app, clock):
    from app.optimization import projections
    from sqlalchemy import event
    from test_projection_replay import project_pending

    service, p = app.state.service, principal(workspace)
    began = clock.now - timedelta(days=89)
    at = began.isoformat()
    with service.db.transaction() as s:
        lead = service.scoped(s, "leads", p)[0]
        number = service.scoped(s, "number_inventory", p)[0]
        number_id = number.id
        feature = put(
            s,
            "feature_snapshots",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead.id,
                "as_of": at,
                "feature_schema_version": 2,
                "features": {"segment": "example", "local_hour": 10, "source": "import", "attempt_count": 1},
                "predictions": {"p_human": 0.2},
            },
            now=began,
        )
        call = put(
            s,
            "call_attempts",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead.id,
                "campaign_id": workspace["campaign_id"],
                "rep_id": p["user_id"],
                "number_id": number_id,
                "started_at": at,
                "submitted_at": at,
                "ended_at": at,
                "state": "ended",
                "outcome": "right_party",
                "outcome_source": "rep",
                "outcome_available_at": at,
                "feature_snapshot_id": feature.id,
                "connected_seconds": 5,
            },
            now=began,
        )
        call_id = call.id
        callback = put(
            s,
            "followup_tasks",
            p["tenant_id"],
            p["seller_id"],
            {
                "inbound": True,
                "number_id": number_id,
                "lead_id": lead.id,
                "status": "open",
                "occurred_at": at,
            },
            now=began,
        )
        callback_id = callback.id
    project_pending(service, p)

    def read():
        def reject(connection, cursor, statement, parameters, context, many):
            assert not any(
                "FROM " + t in statement for t in ("call_attempts", "feature_snapshots", "followup_tasks")
            )

        event.listen(service.db.engine, "before_cursor_execute", reject)
        try:
            return projections.read_number_health_metrics(service, p).get(number_id)
        finally:
            event.remove(service.db.engine, "before_cursor_execute", reject)

    result = read()
    assert result["samples"] == result["human_answers"] == result["comparable_samples"] == 1
    assert result["short_connected_calls"] == result["callbacks"] == result["comparison_strata"] == 1
    assert result["expected_human_answers"] == 0.2
    with service.db.transaction() as s:
        update(
            get(s, "call_attempts", call_id),
            outcome="no_answer",
            connected_seconds=20,
            outcome_available_at=clock.now.isoformat(),
        )
        s.delete(get(s, "followup_tasks", callback_id))
    project_pending(service, p)
    result = read()
    assert result["samples"] == 1
    assert result["human_answers"] == result["short_connected_calls"] == result["callbacks"] == 0
    clock.now += timedelta(days=2)
    projections.refresh_due(service, p)
    project_pending(service, p)
    assert not read() or read()["samples"] == 0


@pytest.mark.parametrize("family", ["coaching", "number_allocation", "strategy"])
def test_comparison_buckets_keep_strata_feedback_seed_and_corrected_speech(workspace, app, clock, family):
    from app.optimization import projections
    from sqlalchemy import event
    from test_projection_replay import project_pending

    service, p = app.state.service, principal(workspace)
    began = clock.now - timedelta(days=30)
    at = (began + timedelta(minutes=10)).isoformat()
    with service.db.transaction() as s:
        lead = service.scoped(s, "leads", p)[0]
        study = {
            "id": "source-study",
            "family": "coaching" if family == "strategy" else family,
            "population": [lead.id, "uncalled"],
            "goal": "meeting_held",
            "window_days": 14,
            "starts_at": began.isoformat(),
            "ends_at": (began + timedelta(days=7)).isoformat(),
        }
        if family == "strategy":
            study.update(
                strategy_ids=["control", "approach"], strategy_policy="two_arm", candidate_probability=0.5
            )
        studies = [study]
        if family == "strategy":
            studies.append(
                {
                    **study,
                    "id": "adaptive-study",
                    "strategy_policy": "adaptive",
                    "evidence_comparison_id": study["id"],
                }
            )
        update(get(s, "campaigns", workspace["campaign_id"]), controlled_comparisons=studies)
        assignment = {
            "comparison_id": study["id"],
            "family": study["family"],
            "lead_id": lead.id,
            "assigned_at": began.isoformat(),
            "arm": "candidate",
            "rep_id": p["user_id"],
            "cluster": "west",
            "goal": "meeting_held",
            "outcome_window_days": 14,
            "strategy_id": "approach",
            "propensity": 0.5,
        }
        put(s, "experiment_assignments", p["tenant_id"], p["seller_id"], assignment, now=began)
        if family == "strategy":
            # The same lead's older approved reward wins over a newer enrollment.
            put(
                s,
                "experiment_assignments",
                p["tenant_id"],
                p["seller_id"],
                {
                    **assignment,
                    "comparison_id": "adaptive-study",
                    "assigned_at": (clock.now - timedelta(days=2)).isoformat(),
                },
                now=clock.now,
            )
        call = put(
            s,
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
                "state": "ended",
                "outcome": "meeting_held",
                "outcome_source": "rep",
                "outcome_available_at": at,
            },
            now=began,
        )
        call_id = call.id
        card = put(
            s,
            "coach_events",
            p["tenant_id"],
            p["seller_id"],
            {
                "call_id": call.id,
                "rep_id": p["user_id"],
                "action": "SHOW_CARD",
                "strategy_exposure": {
                    "comparison_id": study["id"],
                    "strategy_id": "approach",
                    "displayed_at": at,
                    "used_by_rep": None,
                },
            },
            now=began,
        )
        feedback = put(
            s,
            "coaching_feedback",
            p["tenant_id"],
            p["seller_id"],
            {
                "call_id": call.id,
                "coach_event_id": card.id,
                "rep_id": p["user_id"],
                "useful": True,
                "grounded": False,
            },
            key=card.id + ":" + p["user_id"],
            now=began,
        )
        feedback_id = feedback.id
        lineage = {
            "id": "speech-match",
            "version": 1,
            "call_id": call.id,
            "kind": "conversation_lineage",
            "observation_kind": "approach",
            "observed_guidance_id": card.id,
            "strategy_id": "approach",
            "state": "confirmed",
            "review_status": "reviewed",
            "epistemic_status": "model_reviewed",
            "review_receipt_id": "review",
            "available_at": at,
        }
        put(s, "outcomes", p["tenant_id"], p["seller_id"], lineage, now=began)
    project_pending(service, p)

    def read(who=None):
        def reject(connection, cursor, statement, parameters, context, many):
            assert not any(
                "FROM " + table in statement
                for table in (
                    "leads",
                    "call_attempts",
                    "experiment_assignments",
                    "conversion_events",
                    "coaching_feedback",
                    "coach_events",
                    "outcomes",
                    "compliance_decisions",
                )
            ), "Comparison reads must use aggregates, not historical sources"

        event.listen(service.db.engine, "before_cursor_execute", reject)
        try:
            return projections.read_comparison_metrics(service, who or p)
        finally:
            event.remove(service.db.engine, "before_cursor_execute", reject)

    reports = read()
    actual = reports[0]
    assert actual["assigned_leads"] == actual["unassigned_leads"] == 1
    assert actual["arms"]["candidate"]["matured_conversions"] == 1
    quality = actual["coaching_quality"]["candidate"]
    assert quality["reviewed_cards"] == 1
    assert quality["useful_fraction"] == 1 and quality["grounded_fraction"] == 0
    if family == "number_allocation":
        assert actual["cluster_count"] == 1
        assert actual["cluster_summary"]["candidate"]["mean_cluster_conversion"] == 1
    else:
        assert actual["rep_strata"][0]["id"] == p["user_id"]
    if family == "strategy":
        assert actual["strategy_exposure"]["observed_in_speech"] == 1
        assert actual["strategy_exposure"]["reported_used"] == 0
        assert actual["strategy_exposure"]["use_unknown"] == 1
        assert reports[1]["strategy_policy_snapshot"] == actual["strategy_policy_snapshot"]
        assert actual["strategy_policy_snapshot"]["observations"]["approach"] == 1
    assert not actual["sufficient_evidence"]
    private = read({**p, "role": "rep", "user_id": "another-rep"})[0]
    assert private["coaching_quality"]["candidate"]["reviewed_cards"] == 0
    if family == "strategy":
        assert private["strategy_exposure"]["generated"] == 0

    with service.db.transaction() as s:
        update(
            get(s, "call_attempts", call_id),
            outcome="not_interested",
            outcome_available_at=clock.now.isoformat(),
        )
        update(get(s, "coaching_feedback", feedback_id), useful=False)
        put(
            s,
            "outcomes",
            p["tenant_id"],
            p["seller_id"],
            {
                **{k: v for k, v in lineage.items() if k != "observed_guidance_id"},
                "version": 2,
                "state": "retracted",
                "invalidated_at": clock.now.isoformat(),
            },
            now=clock.now,
        )
    project_pending(service, p)
    corrected = read()
    assert corrected[0]["arms"]["candidate"]["matured_conversions"] == 0
    assert corrected[0]["coaching_quality"]["candidate"]["useful_fraction"] == 0
    if family == "strategy":
        assert corrected[0]["strategy_exposure"]["observed_in_speech"] == 0
        assert corrected[1]["strategy_policy_snapshot"] == corrected[0]["strategy_policy_snapshot"]
        assert (
            corrected[0]["strategy_policy_snapshot"]["estimates"]["approach"]
            < actual["strategy_policy_snapshot"]["estimates"]["approach"]
        )
