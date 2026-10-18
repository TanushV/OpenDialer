"""Local-only follow-through: identities, conflicts, holds and recovery. Tests first."""

from datetime import timedelta

import pytest
from app.compliance.policy import authorize
from app.db import Job, put
from conftest import start
from sqlalchemy import select
from test_remediation_rep_workflow import finish, p


def tasks(w, call):
    return [
        t
        for t in w["client"].get("/v1/workspace", headers=w["headers"]).json()["tasks"]
        if t.get("call_id") == call["id"]
    ]


def test_duplicate_wrapup_has_one_outcome_and_does_not_reopen_completed_task(workspace, app, clock):
    call = start(workspace)
    due = (clock.now + timedelta(days=1)).isoformat()
    finish(workspace, call, callback_at=due)
    task = tasks(workspace, call)[0]
    c, h = workspace["client"], workspace["headers"]
    assert (
        c.patch(
            "/v1/tasks/" + task["id"],
            headers=h,
            json={"status": "completed", "expected_version": task["version"]},
        ).status_code
        == 200
    )
    assert (
        c.post(
            "/v1/calls/" + call["id"] + "/disposition",
            headers=h,
            json={"outcome": "callback_requested", "callback_at": due},
        ).status_code
        == 200
    )
    assert len(tasks(workspace, call)) == 1
    assert tasks(workspace, call)[0]["status"] == "completed"
    with app.state.db.transaction(False) as s:
        records = [
            r
            for r in app.state.service.scoped(s, "outcomes", p(workspace))
            if r.data["call_id"] == call["id"]
        ]
        assert len(records) == 1


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_terminal_task_clears_only_its_callback(workspace, app, clock, status):
    call = start(workspace)
    finish(workspace, call, callback_at=(clock.now + timedelta(days=1)).isoformat())
    task = tasks(workspace, call)[0]
    c, h = workspace["client"], workspace["headers"]
    result = c.patch(
        "/v1/tasks/" + task["id"], headers=h, json={"status": status, "expected_version": task["version"]}
    )
    assert result.status_code == 200, result.text
    packet = c.get("/v1/leads/" + call["lead_id"] + "/packet", headers=h).json()
    assert packet["lead"].get("callback_due_at") is None


def test_task_rescheduling_survives_duplicate_wrapup(workspace, clock):
    call = start(workspace)
    old_due = (clock.now + timedelta(days=1)).isoformat()
    new_due = (clock.now + timedelta(days=2)).isoformat()
    finish(workspace, call, callback_at=old_due)
    task = tasks(workspace, call)[0]
    c, h = workspace["client"], workspace["headers"]
    assert (
        c.patch(
            "/v1/tasks/" + task["id"],
            headers=h,
            json={"due_at": new_due, "timezone": "America/Los_Angeles", "expected_version": task["version"]},
        ).status_code
        == 200
    )
    assert (
        c.post(
            "/v1/calls/" + call["id"] + "/disposition",
            headers=h,
            json={"outcome": "callback_requested", "callback_at": old_due},
        ).status_code
        == 200
    )
    assert tasks(workspace, call)[0]["due_at"] == new_due


@pytest.mark.parametrize(
    "edit",
    [
        {"due_at": "2026-10-16T12:00:00"},
        {"timezone": "Mars/Base"},
        {"owner_id": "outside-seller"},
        {"status": "invented"},
        {"due_at": "not-a-date"},
    ],
)
def test_invalid_task_edit_leaves_state_unchanged(workspace, clock, edit):
    call = start(workspace)
    finish(workspace, call, callback_at=(clock.now + timedelta(days=1)).isoformat())
    task = tasks(workspace, call)[0]
    response = workspace["client"].patch(
        "/v1/tasks/" + task["id"],
        headers=workspace["headers"],
        json={"status": "open", "expected_version": task["version"], **edit},
    )
    assert response.status_code in {403, 404, 422}, response.text
    assert tasks(workspace, call)[0] == task


def test_undated_callback_needs_confirmation_and_holds_automatic_outreach(workspace):
    call = start(workspace)
    finish(workspace, call)
    task = tasks(workspace, call)[0]
    packet = (
        workspace["client"]
        .get("/v1/leads/" + call["lead_id"] + "/packet", headers=workspace["headers"])
        .json()
    )
    assert task["status"] == "needs_confirmation"
    assert packet["lead"]["outreach_hold"] == "callback_time_confirmation"


def test_assignment_keeps_callback_with_selected_rep(workspace, app, clock):
    call = start(workspace)
    finish(workspace, call, callback_at=(clock.now + timedelta(days=1)).isoformat())
    with app.state.db.transaction() as s:
        rep = put(
            s,
            "users",
            workspace["tenant_id"],
            workspace["seller_id"],
            {"name": "Teammate", "role": "rep", "disabled": False},
            now=clock(),
        )
    task = tasks(workspace, call)[0]
    response = workspace["client"].patch(
        "/v1/tasks/" + task["id"],
        headers=workspace["headers"],
        json={"owner_id": rep.id, "expected_version": task["version"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["owner_id"] == rep.id
    with app.state.db.transaction(False) as s:
        lead = app.state.service.required(s, "leads", call["lead_id"], p(workspace))
        assert lead.data["callback_owner_id"] == rep.id


def test_booked_hold_is_enforced_by_final_authorization_and_not_time_passage(workspace, app, clock):
    call = start(workspace)
    finish(workspace, call, "meeting_booked")
    clock.now += timedelta(days=1)
    service = app.state.service
    with service.db.transaction(False) as s:
        lead = service.required(s, "leads", call["lead_id"], p(workspace))
        number = service.required(s, "number_inventory", call["number_id"], p(workspace))
        decision = authorize(service.context(s, p(workspace), lead, number))
        assert not decision.allowed and "OUTREACH_HOLD" in decision.reasons


def test_meeting_changes_require_explicit_next_action_and_are_versioned(workspace, clock):
    call = start(workspace)
    finish(workspace, call, "meeting_booked")
    c, h = workspace["client"], workspace["headers"]
    path = "/v1/leads/" + call["lead_id"]
    lead = c.get(path + "/packet", headers=h).json()["lead"]
    due = (clock.now + timedelta(days=2)).isoformat()
    changed = c.post(
        path + "/meeting",
        headers=h,
        json={
            "status": "no_show",
            "expected_version": lead["version"],
            "next_action": "callback",
            "callback_at": due,
            "timezone": "America/Los_Angeles",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["meeting"]["source"] == "rep_reported"
    assert changed.json()["meeting"]["held"] is False
    assert (
        c.post(
            path + "/meeting", headers=h, json={"status": "held", "expected_version": lead["version"]}
        ).status_code
        == 409
    )
    packet = c.get(path + "/packet", headers=h).json()
    assert packet["lead"].get("outreach_hold") is None
    assert packet["lead"]["callback_due_at"] == due
    assert len([t for t in packet["tasks"] if t["status"] == "open"]) == 1


def test_cancel_meeting_without_next_action_keeps_hold(workspace):
    call = start(workspace)
    finish(workspace, call, "meeting_booked")
    c, h = workspace["client"], workspace["headers"]
    path = "/v1/leads/" + call["lead_id"]
    lead = c.get(path + "/packet", headers=h).json()["lead"]
    result = c.post(
        path + "/meeting", headers=h, json={"status": "cancelled", "expected_version": lead["version"]}
    )
    assert result.status_code == 200, result.text
    assert result.json()["outreach_hold"] == "meeting_next_action"


def test_wrapup_and_refresh_job_commit_together(workspace, app, monkeypatch):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post("/v1/calls/" + call["id"] + "/hangup", headers=h)
    original = app.state.service.jobs.enqueue_in

    def fail_prepare(s, queue, *args, **kwargs):
        if queue == "research":
            raise RuntimeError("simulated queue persistence failure")
        return original(s, queue, *args, **kwargs)

    monkeypatch.setattr(app.state.service.jobs, "enqueue_in", fail_prepare)
    with pytest.raises(RuntimeError):
        c.post("/v1/calls/" + call["id"] + "/disposition", headers=h, json={"outcome": "right_party"})
    with app.state.db.transaction(False) as s:
        persisted = app.state.service.required(s, "call_attempts", call["id"], p(workspace))
        assert persisted.data["state"] != "FINALIZED"
        assert not list(s.scalars(select(Job).where(Job.queue == "research")))


def test_callback_assignment_is_enforced_at_submission(workspace, app, clock):
    call = start(workspace)
    finish(workspace, call, callback_at=(clock.now + timedelta(days=1)).isoformat())
    service = app.state.service
    with service.db.transaction() as s:
        rep = put(
            s,
            "users",
            workspace["tenant_id"],
            workspace["seller_id"],
            {"name": "Other rep", "role": "rep"},
            now=clock(),
        )
    task = tasks(workspace, call)[0]
    service.edit_task(p(workspace), task["id"], {"owner_id": rep.id, "expected_version": task["version"]})
    clock.now += timedelta(days=1)
    with service.db.transaction(False) as s:
        lead = service.required(s, "leads", call["lead_id"], p(workspace))
        number = service.required(s, "number_inventory", call["number_id"], p(workspace))
        decision = authorize(service.context(s, p(workspace), lead, number))
        assert "CALLBACK_ASSIGNED_TO_ANOTHER_REP" in decision.reasons


def test_naive_callback_time_is_a_validation_error_not_server_error(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    path = "/v1/calls/" + call["id"]
    c.post(path + "/hangup", headers=h)
    result = c.post(
        path + "/disposition",
        headers=h,
        json={"outcome": "callback_requested", "callback_at": "2026-10-20T12:00:00"},
    )
    assert result.status_code == 422


def test_meeting_endpoint_rejects_unused_task_fields(workspace):
    call = start(workspace)
    finish(workspace, call, "meeting_booked")
    c, h = workspace["client"], workspace["headers"]
    path = "/v1/leads/" + call["lead_id"]
    lead = c.get(path + "/packet", headers=h).json()["lead"]
    result = c.post(
        path + "/meeting",
        headers=h,
        json={"status": "booked", "expected_version": lead["version"], "owner_id": "ignored"},
    )
    assert result.status_code == 422


def test_null_task_status_cannot_remove_task_from_the_workflow(workspace, clock):
    call = start(workspace)
    finish(workspace, call, callback_at=(clock.now + timedelta(days=1)).isoformat())
    task = tasks(workspace, call)[0]
    response = workspace["client"].patch(
        "/v1/tasks/" + task["id"],
        headers=workspace["headers"],
        json={"status": None, "expected_version": task["version"]},
    )
    assert response.status_code == 422
    assert tasks(workspace, call)[0] == task


def test_explicit_new_callback_reopens_previous_meeting_task(workspace, clock):
    call = start(workspace)
    finish(workspace, call, "meeting_booked")
    c, h = workspace["client"], workspace["headers"]
    path = "/v1/leads/" + call["lead_id"]

    def report(status, **extra):
        lead = c.get(path + "/packet", headers=h).json()["lead"]
        result = c.post(
            path + "/meeting",
            headers=h,
            json={"status": status, "expected_version": lead["version"], **extra},
        )
        assert result.status_code == 200, result.text
        return result.json()

    report("no_show", next_action="callback", callback_at=(clock.now + timedelta(days=1)).isoformat())
    old = c.get(path + "/packet", headers=h).json()["tasks"][0]
    report("rescheduled", due_at=(clock.now + timedelta(days=3)).isoformat())
    due = (clock.now + timedelta(days=4)).isoformat()
    report("no_show", next_action="callback", callback_at=due)
    packet = c.get(path + "/packet", headers=h).json()
    assert len(packet["tasks"]) == 1
    assert packet["tasks"][0]["id"] == old["id"]
    assert packet["tasks"][0]["status"] == "open"
    assert packet["lead"]["callback_due_at"] == due


def test_stale_disposition_cannot_overwrite_new_outcome(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    path = "/v1/calls/" + call["id"]
    old = c.post(path + "/hangup", headers=h).json()
    saved = c.post(
        path + "/disposition",
        headers=h,
        json={"outcome": "meeting_booked", "expected_version": old["version"]},
    )
    assert saved.status_code == 200
    stale = c.post(
        path + "/disposition", headers=h, json={"outcome": "no_answer", "expected_version": old["version"]}
    )
    assert stale.status_code == 409
    assert c.get(path, headers=h).json()["outcome"] == "meeting_booked"
