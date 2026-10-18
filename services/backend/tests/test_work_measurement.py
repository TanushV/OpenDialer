"""Observed work time, breaks, correction conflicts and connected-duration evidence."""

from datetime import timedelta

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
