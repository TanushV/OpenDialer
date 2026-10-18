"""Observed work time, breaks, correction conflicts and connected-duration evidence."""

from datetime import timedelta

import pytest
from app.db import doc, put
from conftest import ready, start
from test_remediation_rep_workflow import p


def metrics(w):
    return w["client"].get("/v1/metrics", headers=w["headers"]).json()


def heartbeat(w, activity="ready"):
    return w["client"].post("/v1/rep/heartbeat", headers=w["headers"], json={"activity": activity})


def test_missing_work_evidence_is_unknown_not_attempt_duration(workspace):
    assert metrics(workspace).get("rep_work_seconds", "missing") is None
    assert metrics(workspace).get("conversions_per_rep_hour", "missing") is None


def test_heartbeat_work_excludes_pauses_and_stale_disconnected_time(workspace, clock):
    ready(workspace)
    for _ in range(3):
        clock.now += timedelta(seconds=60)
        response = heartbeat(workspace)
        assert response.status_code == 200, response.text
    assert metrics(workspace)["rep_work_seconds"] == 180
    workspace["client"].post("/v1/rep/presence", headers=workspace["headers"], json={"status": "paused"})
    clock.now += timedelta(minutes=10)
    assert metrics(workspace)["rep_work_seconds"] == 180
    ready(workspace)
    clock.now += timedelta(minutes=10)
    assert metrics(workspace)["rep_work_seconds"] == 270  # 90s maximum without a heartbeat
    assert metrics(workspace)["work_time"]["stale_intervals"] == 1


def test_idle_heartbeat_closes_work_and_does_not_make_paused_rep_ready(workspace, clock):
    ready(workspace)
    clock.now += timedelta(seconds=50)
    assert heartbeat(workspace, "idle").status_code == 200
    clock.now += timedelta(minutes=10)
    assert metrics(workspace)["rep_work_seconds"] == 50
    assert (
        workspace["client"]
        .get("/v1/workspace", headers=workspace["headers"])
        .json()["user"]["presence"]["status"]
        == "paused"
    )
    assert heartbeat(workspace, "ready").status_code == 409


def test_work_union_counts_overlapping_devices_once_per_rep(clock):
    from app.measurement import work_summary

    now = clock.now
    intervals = [
        {
            "rep_id": "one",
            "started_at": (now - timedelta(seconds=100)).isoformat(),
            "ended_at": (now - timedelta(seconds=40)).isoformat(),
        },
        {
            "rep_id": "one",
            "started_at": (now - timedelta(seconds=70)).isoformat(),
            "ended_at": now.isoformat(),
        },
        {
            "rep_id": "two",
            "started_at": (now - timedelta(seconds=20)).isoformat(),
            "ended_at": now.isoformat(),
        },
    ]
    result = work_summary(intervals, [], now=now)
    assert result["seconds"] == 120
    assert result["by_rep"] == {"one": 100, "two": 20}


def test_verified_call_work_extends_past_the_last_presence_heartbeat(workspace, app, clock):
    call = start(workspace)
    clock.now += timedelta(minutes=5)
    workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    assert metrics(workspace)["rep_work_seconds"] == 300
    clock.now += timedelta(minutes=3)
    assert metrics(workspace)["rep_work_seconds"] == 300


def test_current_verified_call_counts_while_listening_and_stops_at_carrier_lifetime(workspace, clock, app):
    start(workspace)
    clock.now += timedelta(minutes=5)
    assert metrics(workspace)["rep_work_seconds"] == 300
    clock.now += timedelta(seconds=app.state.service.settings.max_call_seconds)
    assert metrics(workspace)["rep_work_seconds"] == app.state.service.settings.max_call_seconds
    assert heartbeat(workspace, "call").status_code == 409


def test_manual_work_correction_is_scoped_versioned_and_audited(workspace, clock, app):
    ready(workspace)
    clock.now += timedelta(seconds=60)
    workspace["client"].post("/v1/rep/presence", headers=workspace["headers"], json={"status": "paused"})
    c, h = workspace["client"], workspace["headers"]
    result = c.get("/v1/rep/work", headers=h)
    assert result.status_code == 200, result.text
    row = result.json()["intervals"][0]
    body = {
        "expected_version": row["version"],
        "started_at": row["started_at"],
        "ended_at": (clock.now - timedelta(seconds=20)).isoformat(),
        "reason": "Break started earlier",
    }
    result = c.patch("/v1/rep/work/" + row["id"], headers=h, json=body)
    assert result.status_code == 200, result.text
    assert metrics(workspace)["rep_work_seconds"] == 40
    assert c.patch("/v1/rep/work/" + row["id"], headers=h, json=body).status_code == 409
    with app.state.service.db.transaction(False) as s:
        audit = [
            r
            for r in app.state.service.scoped(s, "audit_events", p(workspace))
            if r.data.get("action") == "work.corrected"
        ]
        assert len(audit) == 1


def test_dashboard_and_optimization_share_goal_and_work_denominators(workspace, app, clock):
    ready(workspace)
    clock.now += timedelta(seconds=60)
    heartbeat(workspace)
    lead_id = workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["leads"][0]["id"]
    with app.state.service.db.transaction() as s:
        put(
            s,
            "conversion_events",
            workspace["tenant_id"],
            workspace["seller_id"],
            {
                "lead_id": lead_id,
                "kind": "sale_won",
                "occurred_at": clock.now.isoformat(),
                "recorded_at": clock.now.isoformat(),
            },
            now=clock.now,
        )
    workspace["client"].put(
        "/v1/admin/optimization",
        headers=workspace["headers"],
        json={"conversion_goal": "sale_won", "window_days": 30},
    )
    dashboard = metrics(workspace)
    report = workspace["client"].get("/v1/optimization", headers=workspace["headers"]).json()["metrics"]
    assert dashboard["unique_conversions"] == report["unique_conversions"] == 1
    assert dashboard["rep_work_seconds"] == report["rep_work_seconds"] == 60
    assert dashboard["conversions_per_rep_hour"] == report["conversions_per_rep_hour"] == 60
    assert dashboard["outcome_window_days"] == 30


def test_answer_to_end_duration_excludes_ringing_and_late_answer_never_revives(workspace, app, clock):
    call = start(workspace)
    service = app.state.service
    clock.now += timedelta(seconds=60)
    answer_at = clock.now
    service.event(
        p(workspace), "answer-1", "call.answered", call["id"], {"occurred_at": answer_at.isoformat()}
    )
    clock.now += timedelta(seconds=4)
    service.event(p(workspace), "hangup-1", "call.hangup", call["id"], {"occurred_at": clock.now.isoformat()})
    clock.now += timedelta(seconds=20)
    service.event(
        p(workspace), "answer-late", "call.answered", call["id"], {"occurred_at": answer_at.isoformat()}
    )
    with service.db.transaction(False) as s:
        saved = doc(service.required(s, "call_attempts", call["id"], p(workspace)))
    assert saved["state"] == "ENDED"
    assert saved["connected_seconds"] == 4
    assert saved["answered_at"] == answer_at.isoformat()


def test_missing_answer_timestamp_is_not_zero_second_connection(workspace, clock, app):
    call = start(workspace)
    clock.now += timedelta(seconds=60)
    workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    with app.state.service.db.transaction(False) as s:
        saved = doc(app.state.service.required(s, "call_attempts", call["id"], p(workspace)))
    assert saved.get("connected_seconds") is None


def test_rep_human_classification_does_not_invent_carrier_answer_timing(workspace, app, clock):
    call = start(workspace)
    clock.now += timedelta(seconds=60)
    app.state.service.event(p(workspace), "manual-classification", "human.confirmed", call["id"])
    clock.now += timedelta(seconds=4)
    workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    saved = workspace["client"].get("/v1/calls/" + call["id"], headers=workspace["headers"]).json()
    assert saved.get("answered_at") is None and saved.get("connected_seconds") is None


def test_connect_warning_requires_supported_comparison_and_keeps_unknown_visible(clock):
    from app.measurement import connect_health

    baseline = [
        {
            "started_at": (clock.now - timedelta(days=2)).isoformat(),
            "outcome": "right_party" if i % 2 else "no_answer",
        }
        for i in range(80)
    ]
    recent = [
        {"started_at": (clock.now - timedelta(minutes=i)).isoformat(), "outcome": "no_answer"}
        for i in range(80)
    ]
    report = connect_health(baseline + recent, now=clock.now)
    assert report["status"] == "warning" and report["samples"] == 80
    assert report["reference_samples"] == 80 and report["confidence_high"] < report["reference_low"]
    assert connect_health(recent[:2], now=clock.now)["status"] == "insufficient_data"
    assert connect_health([{"started_at": clock.now.isoformat()}], now=clock.now)["unknown"] == 1


def test_work_table_migration_preserves_previous_rows(tmp_path):
    from app.migrate import migrate_database
    from sqlalchemy import create_engine, inspect

    url = f"sqlite:///{tmp_path}/metrics.db"
    migrate_database(url)
    assert "work_intervals" in inspect(create_engine(url)).get_table_names()


def test_late_answer_does_not_start_speech_processing_on_an_ended_call(workspace, app, clock):
    call = start(workspace)
    clock.now += timedelta(seconds=5)
    workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    clock.now += timedelta(seconds=1)
    app.state.service.event(p(workspace), "late-only-answer", "call.answered", call["id"])
    with app.state.service.db.transaction(False) as s:
        actions = [r.data for r in app.state.service.scoped(s, "command_outbox", p(workspace))]
    assert not any(r.get("action") == "transcription_start" for r in actions)


@pytest.mark.parametrize("activity", ["call", "wrapup"])
def test_heartbeat_cannot_claim_nonexistent_call_work(workspace, activity):
    assert heartbeat(workspace, activity).status_code == 409
    assert metrics(workspace)["rep_work_seconds"] is None


def test_heartbeat_accepts_only_the_reps_current_call_or_recent_wrapup(workspace, clock):
    call = start(workspace)
    assert heartbeat(workspace, "call").status_code == 200
    assert heartbeat(workspace, "wrapup").status_code == 409
    workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    assert heartbeat(workspace, "call").status_code == 409
    assert heartbeat(workspace, "wrapup").status_code == 200
    clock.now += timedelta(minutes=16)
    assert heartbeat(workspace, "wrapup").status_code == 409


def test_work_review_filters_ownership_before_applying_limit(workspace, app, clock):
    service, principal = app.state.service, p(workspace) | {"role": "rep"}
    ready(workspace)
    with service.db.transaction() as s:
        for i in range(101):
            put(
                s,
                "work_intervals",
                principal["tenant_id"],
                principal["seller_id"],
                {
                    "rep_id": "another-rep",
                    "started_at": clock.now.isoformat(),
                    "ended_at": clock.now.isoformat(),
                },
                now=clock.now + timedelta(seconds=i + 1),
            )
    report = service.work_intervals(principal)
    assert len(report["intervals"]) == 1
    assert report["intervals"][0]["rep_id"] == principal["user_id"]


def test_two_devices_union_and_logout_do_not_extend_stale_work(workspace, clock):
    c, h = workspace["client"], workspace["headers"]
    ready(workspace)
    clock.now += timedelta(seconds=30)
    login = c.post(
        "/v1/auth/session",
        json={
            "email": "operator@example.test",
            "password": "a-test-only-password-123",
        },
    ).json()
    second = {**workspace, "headers": {"Authorization": "Bearer " + login["access_token"]}}
    ready(second)
    clock.now += timedelta(seconds=30)
    assert c.post("/v1/auth/logout", headers=h).status_code == 200
    clock.now += timedelta(seconds=30)
    heartbeat(second)
    assert metrics(second)["rep_work_seconds"] == 90
    c.post("/v1/rep/presence", headers=second["headers"], json={"status": "paused"})
    clock.now += timedelta(minutes=5)
    assert metrics(second)["rep_work_seconds"] == 90


@pytest.mark.parametrize("action", ["pause", "idle"])
def test_deliberate_pause_closes_all_devices_but_idle_device_does_not_pause_another(workspace, clock, action):
    c, h = workspace["client"], workspace["headers"]
    ready(workspace)
    login = c.post(
        "/v1/auth/session",
        json={
            "email": "operator@example.test",
            "password": "a-test-only-password-123",
        },
    ).json()
    second = {**workspace, "headers": {"Authorization": "Bearer " + login["access_token"]}}
    ready(second)
    clock.now += timedelta(seconds=60)
    heartbeat(second)
    if action == "pause":
        c.post("/v1/rep/presence", headers=h, json={"status": "paused"})
        clock.now += timedelta(minutes=5)
        assert metrics(workspace)["rep_work_seconds"] == 60
    else:
        heartbeat(workspace, "idle")
        assert c.get("/v1/workspace", headers=h).json()["user"]["presence"]["status"] == "ready"
        clock.now += timedelta(seconds=60)
        assert heartbeat(second).status_code == 200
        assert metrics(workspace)["rep_work_seconds"] == 120


@pytest.mark.parametrize("start_delta,end_delta", [(0, 1), (10, 0), (-90000, 0)])
def test_manual_work_correction_rejects_future_reversed_or_unbounded_time(
    workspace,
    clock,
    start_delta,
    end_delta,
):
    ready(workspace)
    clock.now += timedelta(seconds=60)
    c, h = workspace["client"], workspace["headers"]
    c.post("/v1/rep/presence", headers=h, json={"status": "paused"})
    row = c.get("/v1/rep/work", headers=h).json()["intervals"][0]
    result = c.patch(
        "/v1/rep/work/" + row["id"],
        headers=h,
        json={
            "expected_version": row["version"],
            "reason": "Invalid time fixture",
            "started_at": (clock.now + timedelta(seconds=start_delta)).isoformat(),
            "ended_at": (clock.now + timedelta(seconds=end_delta)).isoformat(),
        },
    )
    assert result.status_code == 422
    assert metrics(workspace)["rep_work_seconds"] == 60


@pytest.mark.asyncio
async def test_delayed_webhooks_use_source_answer_and_end_times(workspace, app, clock):
    call = start(workspace)
    began = clock.now
    clock.now += timedelta(seconds=100)
    for kind, seconds in [("call.hangup", 64), ("call.answered", 60)]:
        await app.state.service.accept_webhook(
            workspace["tenant_id"],
            {
                "id": "delayed-" + kind,
                "event_type": kind,
                "occurred_at": (began + timedelta(seconds=seconds)).isoformat(),
                "payload": {"call_control_id": call["provider_id"]},
            },
            "hash-" + kind,
        )
        await app.state.service.run_jobs(tenant_id=workspace["tenant_id"], queues=["webhook"])
    saved = workspace["client"].get("/v1/calls/" + call["id"], headers=workspace["headers"]).json()
    assert saved["connected_seconds"] == 4
    assert saved["answer_timing_source"] == "provider"
    assert saved["state"] == "ENDED"


def test_short_call_rate_uses_all_measured_connections_and_excludes_unknown_or_invalid(clock):
    from app.optimization.numbering import number_health

    attempts = [
        {"outcome": "voicemail", "connected_seconds": 4},
        {"outcome": "right_party", "connected_seconds": 20},
        {"outcome": "right_party", "duration_seconds": 3},
        {"outcome": "right_party", "connected_seconds": -4},
    ]
    report = number_health(attempts, pooled_attempts=attempts, now=clock.now)
    assert report["short_connected_calls"] == 1
    assert report["connected_duration_samples"] == 2
    assert report["short_connected_rate"] == 0.5
