"""Review opens with what needs the rep and the one thing to do about each (November 9)."""

from datetime import UTC, datetime, timedelta

from app.review_issues import issues

NOW = datetime(2026, 11, 9, 15, tzinfo=UTC)


def codes(rows):
    return [r["code"] for r in rows]


def test_an_empty_workspace_has_no_issues():
    assert issues(blockers=[], leads=[], calls=[], tasks=[], now=NOW) == []


def test_blockers_come_first_and_self_clearing_ones_are_left_out():
    blockers = [
        {
            "code": "TELNYX_NOT_CONFIGURED",
            "message": "Add your Telnyx API key in Settings.",
            "fix": "settings",
        },
        {"code": "CONNECTION_RECONNECTING", "message": "Reconnecting.", "fix": "dialer"},
    ]
    tasks = [{"status": "open", "due_at": (NOW - timedelta(days=1)).isoformat()}]
    rows = issues(blockers=blockers, leads=[], calls=[], tasks=tasks, now=NOW)
    assert codes(rows) == ["TELNYX_NOT_CONFIGURED", "FOLLOWUPS_OVERDUE"]
    assert rows[0]["fix"] == {"page": "settings", "label": "Open Settings"}


def test_calls_without_an_outcome_count_real_ended_calls_only():
    calls = [
        {"state": "FINALIZED", "outcome": None},
        {"state": "FINALIZED", "outcome": "not_interested"},
        {"state": "FINALIZED", "outcome": None, "is_test": True},
        {"state": "ringing", "outcome": None},
    ]
    [row] = issues(blockers=[], leads=[], calls=calls, tasks=[], now=NOW)
    assert row["title"] == "1 call has no outcome saved" and row["fix"]["page"] == "review"


def test_overdue_follow_ups_skip_closed_and_future_ones():
    tasks = [
        {"status": "open", "due_at": "2026-11-04T11:39:36.719014+00:00"},
        {"status": "open", "due_at": "2026-11-04T13:24:10+00:00"},
        {"status": "completed", "due_at": "2026-11-04T13:24:10+00:00"},
        {"status": "open", "due_at": (NOW + timedelta(hours=2)).isoformat()},
    ]
    [row] = issues(blockers=[], leads=[], calls=[], tasks=tasks, now=NOW)
    assert row["title"] == "2 follow-ups are overdue"


def test_held_leads_are_grouped_with_what_to_do_and_settled_ones_last():
    leads = [
        {"live_status": "dead"},
        {"number_status": "conflict"},
        {"number_status": "conflict"},
        {"number_status": "conflict", "archived": True},
        {"number_status": "confirmed"},
    ]
    rows = issues(blockers=[], leads=leads, calls=[], tasks=[], now=NOW)
    assert codes(rows) == ["NUMBER_CONFLICT", "NUMBER_DEAD"]
    assert rows[0]["title"] == "2 leads have a number that may belong to someone else"
    assert rows[0]["fix"]["page"] == "leads"
    assert rows[1]["title"] == "1 lead has a dead number and is skipped"
    assert rows[1]["level"] == "info" and rows[1]["fix"] is None


def test_the_route_answers_for_the_simulated_workspace(workspace):
    body = workspace["client"].get("/v1/review/issues", headers=workspace["headers"]).json()
    assert isinstance(body["issues"], list)
    for row in body["issues"]:
        assert row["title"] and row["level"] in {"fix", "info"}


def test_a_stuck_self_clearing_blocker_is_listed():
    blockers = [
        {
            "code": "CONNECTION_RECONNECTING",
            "message": "Reconnecting. Press Reconnect.",
            "fix": "settings",
            "stuck": True,
        }
    ]
    rows = issues(blockers=blockers, leads=[], calls=[], tasks=[], now=NOW)
    assert codes(rows) == ["CONNECTION_RECONNECTING"]
