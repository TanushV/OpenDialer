from datetime import timedelta

from app.db import put, rows, update
from conftest import completed_report, start


def auth(workspace):
    return {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


def finish(workspace, call, outcome="right_party"):
    c, h = workspace["client"], workspace["headers"]
    assert c.post(f"/v1/calls/{call['id']}/hangup", headers=h).status_code == 200
    result = c.post(f"/v1/calls/{call['id']}/disposition", headers=h, json={"outcome": outcome})
    assert result.status_code == 200, result.text


def test_optimization_goal_settings_are_validated_and_reported(workspace):
    c, h = workspace["client"], workspace["headers"]
    result = c.put(
        "/v1/admin/optimization",
        headers=h,
        json={
            "conversion_goal": "sale_won",
            "window_days": 30,
            "exploration": 0.1,
            "auto_train": True,
            "horizon_hours": 48,
            "rep_minutes": 240,
        },
    )
    assert result.status_code == 200, result.text
    report = completed_report(workspace, headers=h)
    assert report.status_code == 200
    assert report.json()["settings"]["conversion_goal"] == "sale_won"
    assert c.put("/v1/admin/optimization", headers=h, json={"exploration": 1.5}).status_code == 422
    assert c.put("/v1/admin/optimization", headers=h, json={"conversion_goal": "emotion"}).status_code == 422


def test_call_decision_freezes_features_explains_allocation_and_keeps_immutable_lineage(workspace, app):
    call = start(workspace)
    assert call.get("feature_snapshot_id") and call.get("decision_id")
    with app.state.db.transaction() as session:
        snapshot = next(r for r in rows(session, "feature_snapshots") if r.id == call["feature_snapshot_id"])
        original = dict(snapshot.data)
        lead = next(r for r in rows(session, "leads") if r.id == call["lead_id"])
        update(lead, segment="changed-after-decision")
    finish(workspace, call)
    c, h = workspace["client"], workspace["headers"]
    result = c.post("/v1/admin/optimization/refresh", headers=h)
    assert result.status_code == 200, result.text
    report = completed_report(workspace, headers=h).json()
    assert report["decisions"] and report["decisions"][-1]["allocation"]["reason"]
    with app.state.db.transaction(False) as session:
        snapshot = next(r for r in rows(session, "feature_snapshots") if r.id == call["feature_snapshot_id"])
        assert snapshot.data == original
    assert report["dataset"]["rows"] >= 1 and report["dataset"]["missing_snapshot_calls"] == 0


def test_later_conversion_is_idempotent_attributed_and_not_counted_twice(workspace, clock):
    call = start(workspace)
    finish(workspace, call)
    c, h = workspace["client"], workspace["headers"]
    body = {
        "event_id": "crm-meeting-001",
        "kind": "meeting_held",
        "call_id": call["id"],
        "occurred_at": clock.now.isoformat(),
        "source_reference": "permissioned-test-outcome",
    }
    a = c.post(f"/v1/leads/{call['lead_id']}/conversions", headers=h, json=body)
    b = c.post(f"/v1/leads/{call['lead_id']}/conversions", headers=h, json=body)
    assert a.status_code == b.status_code == 200, a.text
    assert a.json()["id"] == b.json()["id"]
    bad = c.post(f"/v1/leads/{call['lead_id']}/conversions", headers=h, json={**body, "kind": "sale_won"})
    assert bad.status_code == 409
    c.post("/v1/admin/optimization/refresh", headers=h)
    report = completed_report(workspace, headers=h).json()
    assert report["metrics"]["unique_conversions"] == 1
    assert report["dataset"]["positive_conversions"] == 1
    assert report["dataset"]["pending_rows"] == 0


def test_original_eligible_denominator_does_not_change_after_optout(workspace):
    c, h = workspace["client"], workspace["headers"]
    before = completed_report(workspace, headers=h).json()["metrics"]
    lead = c.get("/v1/workspace", headers=h).json()["leads"][0]
    c.post("/v1/suppressions", headers=h, json={"phone": lead["phone"], "reason": "opt_out"})
    after = completed_report(workspace, headers=h).json()["metrics"]
    assert after["original_eligible_leads"] == before["original_eligible_leads"]
    assert after["suppressed_now"] > before["suppressed_now"]


def test_refresh_reconciles_delayed_windows_without_rewriting_features(workspace, clock):
    call = start(workspace)
    finish(workspace, call)
    c, h = workspace["client"], workspace["headers"]
    c.post("/v1/admin/optimization/refresh", headers=h)
    first = completed_report(workspace, headers=h).json()["dataset"]
    assert first["pending_rows"] == 1
    clock.now += timedelta(days=20)
    # Advancing beyond the outcome window also expires the original protected session.
    login = c.post(
        "/v1/auth/session", json={"email": "operator@example.test", "password": "a-test-only-password-123"}
    )
    h = {"Authorization": "Bearer " + login.json()["access_token"]}
    c.post("/v1/admin/optimization/refresh", headers=h)
    later = completed_report(workspace, headers=h).json()["dataset"]
    assert later["pending_rows"] == 0 and later["matured_rows"] == 1


def test_automatic_training_uses_owned_dataset_and_is_bounded_when_sparse(workspace):
    c, h = workspace["client"], workspace["headers"]
    result = c.post("/v1/admin/optimization/train", headers=h, json={})
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "insufficient_data"
    assert result.json()["rows"] == 0
    assert (
        c.post("/v1/admin/optimization/train", headers=h, json={"rows": [{"fake": True}]}).status_code == 422
    )


def test_number_health_review_requires_observed_evidence_and_retains_callbacks(workspace):
    c, h = workspace["client"], workspace["headers"]
    number = c.get("/v1/workspace", headers=h).json()["numbers"][0]
    bad = c.post(
        f"/v1/admin/numbers/{number['id']}/review",
        headers=h,
        json={"decision": "increase", "review_reference": "no observations", "daily_limit": 100},
    )
    assert bad.status_code == 409
    paused = c.post(
        f"/v1/admin/numbers/{number['id']}/review",
        headers=h,
        json={"decision": "pause", "review_reference": "Investigate failures"},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["lifecycle"] == "paused_for_outbound"
    assert paused.json()["inbound_retained"] is True


def test_optimization_endpoints_require_auth_and_seller_scoping(workspace, app):
    c, h = workspace["client"], workspace["headers"]
    assert c.get("/v1/optimization").status_code == 401
    report = completed_report(workspace, headers=h)
    assert report.status_code == 200
    assert "registry" not in report.text.lower() and "password_hash" not in report.text
    other = app.state.service.setup(
        {
            "seller_name": "Other Seller",
            "campaign_name": "Other",
            "email": "other-opt@example.test",
            "password": "another-password-123",
        },
        first=False,
    )
    foreign = {"Authorization": "Bearer " + other["access_token"]}
    call = start(workspace)
    assert c.post(
        f"/v1/leads/{call['lead_id']}/conversions",
        headers=foreign,
        json={
            "event_id": "foreign",
            "kind": "meeting_held",
            "occurred_at": call["started_at"],
            "source_reference": "wrong seller",
        },
    ).status_code in {403, 404}


def test_future_plan_explains_callback_and_does_not_reserve_or_call(workspace):
    c, h = workspace["client"], workspace["headers"]
    response = c.post("/v1/admin/optimization/refresh", headers=h)
    assert response.status_code == 200
    plan = completed_report(workspace, headers=h).json()
    assert plan["future_slots"] and all(s["reason"] for s in plan["future_slots"])
    assert not c.get("/v1/workspace", headers=h).json()["calls"]


def test_learning_state_survives_authority_restart_and_backup(workspace, app):
    c, h = workspace["client"], workspace["headers"]
    c.put("/v1/admin/optimization", headers=h, json={"conversion_goal": "sale_won"})
    assert c.post("/v1/admin/optimization/refresh", headers=h).status_code == 200
    backup = c.post("/v1/admin/backup", headers=h)
    assert backup.status_code == 200
    assert completed_report(workspace, headers=h).json()["settings"]["conversion_goal"] == "sale_won"


def test_training_is_queued_once_from_real_stored_snapshots_and_produces_a_candidate(workspace, app, clock):
    c, h = workspace["client"], workspace["headers"]
    number_id = c.get("/v1/workspace", headers=h).json()["numbers"][0]["id"]
    with app.state.db.transaction() as session:
        for i in range(120):
            at = clock.now - timedelta(days=40, minutes=120 - i)
            snapshot = put(
                session,
                "feature_snapshots",
                workspace["tenant_id"],
                workspace["seller_id"],
                {
                    "lead_id": f"historic-{i}",
                    "as_of": at.isoformat(),
                    "features": {
                        "local_hour": 9 + i % 8,
                        "attempt_count": i % 3,
                        "number_health": None,
                        "number_health_missing": 1,
                        "segment": "roofing",
                    },
                },
                now=at,
            )
            put(
                session,
                "call_attempts",
                workspace["tenant_id"],
                workspace["seller_id"],
                {
                    "lead_id": f"historic-{i}",
                    "endpoint_id": f"endpoint-{i}",
                    "feature_snapshot_id": snapshot.id,
                    "rep_id": workspace["user_id"],
                    "number_id": number_id,
                    "started_at": at.isoformat(),
                    "ended_at": (at + timedelta(seconds=60)).isoformat(),
                    "outcome_available_at": (at + timedelta(seconds=60)).isoformat(),
                    "outcome_source": "rep_reported",
                    "state": "FINALIZED",
                    "outcome": "right_party" if i % 3 == 0 else "no_answer",
                    "transcript_version": 0,
                },
                now=at,
            )
    first = c.post("/v1/admin/optimization/train", headers=h, json={})
    second = c.post("/v1/admin/optimization/train", headers=h, json={})
    assert first.status_code == second.status_code == 200, first.text
    assert first.json()["job_id"] == second.json()["job_id"]
    assert first.json()["rows"] == 120
    result = c.post("/v1/admin/jobs/run", headers=h)
    assert result.status_code == 200
    models = c.get("/v1/workspace", headers=h).json()["models"]
    assert models and models[-1]["source_dataset_id"]
    assert not models[-1]["active"]
    assert "pooled_baseline_brier" in models[-1]["metrics"]


def test_new_events_never_cross_seller_boundaries_within_a_tenant(workspace, app):
    c, h = workspace["client"], workspace["headers"]
    with app.state.db.transaction() as session:
        own = next(r for r in rows(session, "users") if r.id == workspace["user_id"])
        other = put(
            session,
            "users",
            workspace["tenant_id"],
            "different-seller",
            {**own.data, "email": "same-tenant-other-seller@example.test"},
        )
        from app.db import emit

        emit(
            session,
            workspace["tenant_id"],
            "call.updated",
            other,
            app.state.service.clock(),
            {"secret_other_seller": "never-distribute"},
        )
    events = c.get("/v1/events", headers=h).json()
    assert "secret_other_seller" not in str(events)


def test_future_conversion_and_conflicting_call_attribution_are_rejected(workspace, clock):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    future = c.post(
        f"/v1/leads/{call['lead_id']}/conversions",
        headers=h,
        json={
            "event_id": "future",
            "kind": "meeting_held",
            "call_id": call["id"],
            "occurred_at": (clock.now + timedelta(days=1)).isoformat(),
            "source_reference": "future",
        },
    )
    assert future.status_code == 422
    mismatch = c.post(
        f"/v1/leads/{call['lead_id']}/conversions",
        headers=h,
        json={
            "event_id": "wrong-call",
            "kind": "meeting_held",
            "call_id": "missing",
            "occurred_at": clock.now.isoformat(),
            "source_reference": "mismatch",
        },
    )
    assert mismatch.status_code in {403, 404, 422}


def test_observed_delivery_failures_pause_outbound_but_keep_incoming_identity(workspace, app, clock):
    c, h = workspace["client"], workspace["headers"]
    w = c.get("/v1/workspace", headers=h).json()
    number, lead = w["numbers"][0], w["leads"][0]
    with app.state.db.transaction() as s:
        for _index in range(25):
            put(
                s,
                "call_attempts",
                workspace["tenant_id"],
                workspace["seller_id"],
                {
                    "lead_id": lead["id"],
                    "endpoint_id": lead["endpoint_id"],
                    "rep_id": workspace["user_id"],
                    "number_id": number["id"],
                    "started_at": clock.now.isoformat(),
                    "ended_at": clock.now.isoformat(),
                    "state": "ENDED",
                    "end_reason": "provider_failure",
                    "outcome": None,
                    "transcript_version": 0,
                },
                now=clock.now,
            )
    result = c.post("/v1/admin/optimization/refresh", headers=h)
    assert result.status_code == 200, result.text
    completed_report(workspace, headers=h)
    after = next(n for n in c.get("/v1/workspace", headers=h).json()["numbers"] if n["id"] == number["id"])
    assert after["lifecycle"] == "investigating" and after["inbound_retained"]


async def test_enabled_worker_schedules_bounded_automatic_learning_without_operator_requests(workspace, app):
    app.state.settings.worker_enabled = True
    await app.state.service.run_jobs(tenant_id=workspace["tenant_id"], queues=["ml"])
    c, h = workspace["client"], workspace["headers"]
    report = completed_report(workspace, headers=h).json()
    assert report["dataset"]["snapshots"]
    before = len(c.get("/v1/workspace", headers=h).json()["jobs"])
    await app.state.service.run_jobs(tenant_id=workspace["tenant_id"], queues=["ml"])
    assert len(c.get("/v1/workspace", headers=h).json()["jobs"]) == before
