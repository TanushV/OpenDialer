"""Local-only follow-through: identities, conflicts, holds and recovery. Tests first."""

from datetime import timedelta
from uuid import uuid4

import pytest
from app.compliance.policy import authorize
from app.db import Job, put
from conftest import start
from sqlalchemy import select
from test_remediation_rep_workflow import finish, p


@pytest.mark.parametrize("missing", ["request_id", "expected_version"])
def test_wrapup_requires_identified_versioned_edits_but_read_and_hangup_remain_available(workspace, missing):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    path = f"/v1/calls/{call['id']}"
    ended = c.post(path + "/hangup", headers=h).json()
    body = {"request_id": str(uuid4()), "expected_version": ended["version"], "outcome": "no_answer"}
    body.pop(missing)
    response = c.post(path + "/disposition", headers=h, json=body)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "client_upgrade_required"
    assert c.get(path, headers=h).status_code == 200
    assert c.post(path + "/hangup", headers=h).status_code == 200


def test_task_replay_returns_original_receipt_and_current_view_without_overwriting_newer_edit(
    workspace, clock
):
    call = start(workspace)
    finish(workspace, call, callback_at=(clock.now + timedelta(days=1)).isoformat())
    task = tasks(workspace, call)[0]
    c, h = workspace["client"], workspace["headers"]
    path = f"/v1/tasks/{task['id']}"
    body = {"request_id": str(uuid4()), "expected_version": task["version"], "note": "First human decision"}
    first = c.patch(path, headers=h, json=body)
    assert first.status_code == 200, first.text
    second = c.patch(
        path,
        headers=h,
        json={
            "request_id": str(uuid4()),
            "expected_version": first.json()["version"],
            "note": "Later human decision",
        },
    )
    assert second.status_code == 200, second.text
    replay = c.patch(path, headers=h, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["note"] == "Later human decision"
    assert replay.json()["version"] == second.json()["version"]
    assert replay.json()["edit_receipt"] == first.json()["edit_receipt"]
    conflict = c.patch(path, headers=h, json={**body, "note": "Different request content"})
    assert conflict.status_code == 409


def test_wrapup_replay_has_one_outcome_one_task_and_does_not_repeat_preparation(workspace, app, clock):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    path = f"/v1/calls/{call['id']}"
    ended = c.post(path + "/hangup", headers=h).json()
    body = {
        "request_id": str(uuid4()),
        "expected_version": ended["version"],
        "outcome": "callback_requested",
        "contact_role": "right_party",
        "callback_at": (clock.now + timedelta(days=1)).isoformat(),
        "callback_note": "Rep confirmed the next discussion",
    }
    saved = c.post(path + "/disposition", headers=h, json=body)
    assert saved.status_code == 200, saved.text
    version = c.get(f"/v1/leads/{call['lead_id']}/packet", headers=h).json()["lead"]["version"]
    replay = c.post(path + "/disposition", headers=h, json=body)
    assert replay.status_code == 200 and replay.json()["edit_receipt"] == saved.json()["edit_receipt"]
    assert replay.json()["canonical_task_id"] == tasks(workspace, call)[0]["id"]
    assert replay.json()["rundown_status"] in {"pending", "partial", "ready", "unavailable"}
    assert c.get(f"/v1/leads/{call['lead_id']}/packet", headers=h).json()["lead"]["version"] == version
    with app.state.db.transaction(False) as s:
        outcomes = [
            r
            for r in app.state.service.scoped(s, "outcomes", p(workspace))
            if r.external_key == f"disposition:{call['id']}"
        ]
        assert len(outcomes) == 1 and outcomes[0].data["contact_role"] == "right_party"


def test_meeting_report_replays_without_repeating_outcome_or_clearing_newer_hold(workspace):
    call = start(workspace)
    finish(workspace, call, "meeting_booked")
    c, h = workspace["client"], workspace["headers"]
    path = f"/v1/leads/{call['lead_id']}"
    lead = c.get(path + "/packet", headers=h).json()["lead"]
    first_body = {"request_id": str(uuid4()), "expected_version": lead["version"], "status": "held"}
    first = c.post(path + "/meeting", headers=h, json=first_body)
    assert first.status_code == 200, first.text
    changed = c.post(
        path + "/meeting",
        headers=h,
        json={
            "request_id": str(uuid4()),
            "expected_version": first.json()["version"],
            "status": "no_show",
            "next_action": "keep_hold",
        },
    )
    assert changed.status_code == 200, changed.text
    replay = c.post(path + "/meeting", headers=h, json=first_body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["meeting"]["status"] == "no_show"
    assert replay.json()["outreach_hold"] == "meeting_next_action"
    assert replay.json()["edit_receipt"] == first.json()["edit_receipt"]


def test_continuity_refresh_coalesces_and_retains_running_owner(workspace, app):
    service, who = app.state.service, p(workspace)
    lead_id = workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["leads"][0]["id"]
    for _ in range(3):
        with service.db.transaction() as s:
            service.refresh_packet_in(s, who, service.required(s, "leads", lead_id, who))
    with service.db.transaction(False) as s:
        jobs = list(s.scalars(select(Job).where(Job.queue == "scripts")))
        assert len(jobs) == 1
        identity = jobs[0].id
        assert jobs[0].payload["kind"] == "packet_refresh"
    claimed = service.jobs.claim("scripts", "original-worker", tenant_id=who["tenant_id"])
    assert claimed["id"] == identity
    with service.db.transaction() as s:
        service.refresh_packet_in(s, who, service.required(s, "leads", lead_id, who))
    service.jobs.complete(identity, "original-worker")
    next_work = service.jobs.claim("scripts", "next-worker", tenant_id=who["tenant_id"])
    assert next_work and next_work["id"] == identity
    assert next_work["attempts"] == 1, "Changed input is newer work, not a failed attempt"


def test_human_note_correction_marks_the_next_packet_dirty_atomically(workspace, app):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    analysis = c.get(f"/v1/calls/{call['id']}/analysis", headers=h).json()
    before = c.get(f"/v1/leads/{call['lead_id']}/packet", headers=h).json()
    response = c.patch(
        f"/v1/calls/{call['id']}/notes",
        headers=h,
        json={
            "expected_version": analysis["notes_version"],
            "text": "The rep corrected the next conversation goal.",
        },
    )
    assert response.status_code == 200, response.text
    after = c.get(f"/v1/leads/{call['lead_id']}/packet", headers=h).json()
    assert after["lead"].get("preparation_revision", 0) > before["lead"].get("preparation_revision", 0)
    assert "The rep corrected the next conversation goal." in str(after["prospect_brief"])


@pytest.mark.parametrize("operation", ["history", "meeting"])
def test_rep_task_history_and_meeting_edits_remain_scoped_to_relationship_owner(
    workspace, app, clock, operation
):
    from app.db import update
    from fastapi import HTTPException

    call = start(workspace)
    finish(workspace, call, callback_at=(clock.now + timedelta(days=1)).isoformat())
    service, who = app.state.service, p(workspace)
    with service.db.transaction() as s:
        user = put(
            s, "users", who["tenant_id"], who["seller_id"], {"name": "Other rep", "role": "rep"}, now=clock()
        )
        lead = service.required(s, "leads", call["lead_id"], who)
        update(lead, rep_id=who["user_id"])
        version = lead.version
        other = {**who, "user_id": user.id, "role": "rep"}
    if operation == "history":
        page = service.workspace_page(other, "tasks")
        assert not page["items"], "A rep's task history must not reveal another rep's relationships"
        return
    with pytest.raises(HTTPException) as denied:
        service.report_meeting(
            other,
            call["lead_id"],
            {"request_id": str(uuid4()), "expected_version": version, "status": "booked"},
        )
    assert denied.value.status_code in {403, 404}


@pytest.mark.parametrize("replay", [False, True])
def test_task_replay_still_rechecks_current_account_authority(workspace, app, clock, replay):
    from app.db import update
    from fastapi import HTTPException

    call = start(workspace)
    finish(workspace, call, callback_at=(clock.now + timedelta(days=1)).isoformat())
    task = tasks(workspace, call)[0]
    service, who = app.state.service, p(workspace)
    body = {"request_id": str(uuid4()), "expected_version": task["version"], "note": "Confirmed context"}
    first = service.edit_task(who, task["id"], body)
    with service.db.transaction() as s:
        update(service.required(s, "users", who["user_id"], who), disabled=True)
    if not replay:
        body = {**body, "request_id": str(uuid4()), "expected_version": first["version"]}
    with pytest.raises(HTTPException) as denied:
        service.edit_task(who, task["id"], body)
    assert denied.value.status_code in {403, 404}


def test_callback_time_and_snooze(workspace, clock):
    """C011: a reminder date cannot silently replace the promised callback."""
    from uuid import uuid4

    call = start(workspace)
    due = (clock.now + timedelta(days=2)).isoformat()
    finish(workspace, call, callback_at=due)
    task = tasks(workspace, call)[0]
    snooze = (clock.now + timedelta(hours=1)).isoformat()
    response = workspace["client"].patch(
        f"/v1/tasks/{task['id']}",
        headers=workspace["headers"],
        json={
            "request_id": str(uuid4()),
            "expected_version": task["version"],
            "status": "snoozed",
            "snoozed_until": snooze,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["due_at"] == due
    assert response.json()["snoozed_until"] == snooze
    assert response.json()["confirmation_state"] == "confirmed"


@pytest.mark.parametrize("conflicting_human", [False, True])
def test_callback_identity_and_alias(workspace, app, clock, conflicting_human):
    """C010: legacy duplication is reconciled without losing human decisions."""
    from uuid import uuid4

    call = start(workspace)
    due = (clock.now + timedelta(days=1)).isoformat()
    finish(workspace, call, callback_at=due)
    original = tasks(workspace, call)[0]
    service, who = app.state.service, p(workspace)
    with service.db.transaction() as s:
        duplicate = put(
            s,
            "followup_tasks",
            who["tenant_id"],
            who["seller_id"],
            {
                **{k: v for k, v in original.items() if k not in {"id", "version"}},
                **(
                    {
                        "human_edited": True,
                        "due_at": (clock.now + timedelta(days=2)).isoformat(),
                        "note": "Rep promised a different day",
                    }
                    if conflicting_human
                    else {}
                ),
            },
            key="legacy-duplicate",
            now=clock.now,
        )
        alias = duplicate.id
    # Startup/backfill is the required idempotent reconciliation boundary.
    service.migrate_followthrough()
    view = tasks(workspace, call)
    if conflicting_human:
        assert len(view) == 2 and any(t.get("source_conflicts") for t in view)
    else:
        assert len(view) == 1 and view[0]["id"] == original["id"]
        edit = workspace["client"].patch(
            f"/v1/tasks/{alias}",
            headers=workspace["headers"],
            json={
                "request_id": str(uuid4()),
                "expected_version": view[0]["version"],
                "status": "completed",
            },
        )
        assert edit.status_code == 200 and edit.json()["id"] == original["id"]


def tasks(w, call):
    return [
        t
        for t in w["client"].get("/v1/workspace", headers=w["headers"]).json()["tasks"]
        if t.get("call_id") == call["id"]
    ]


def test_next_call_brief_reuses_prior_answers_questions_and_exposes_expired_gaps(workspace, app, clock):
    from app.coaching.context import upgrade_state
    from test_live_memory import turn

    service, who = app.state.service, p(workspace)
    call = start(workspace)
    turn(service, who, call, "prior-context")
    with service.db.transaction() as s:
        row = service.required(s, "call_attempts", call["id"], who)
        state = upgrade_state(
            {
                "call_id": call["id"],
                "version": 1,
                "transcript_version": 1,
                "answered_questions": [
                    {
                        "id": "answer",
                        "text": "Operations owns the evaluation.",
                        "status": "confirmed",
                        "evidence_revisions": {"prior-context": 1},
                    }
                ],
                "open_questions": [
                    {
                        "id": "question",
                        "text": "Which approval step needs clarification?",
                        "status": "open",
                        "evidence_revisions": {"prior-context": 1},
                    }
                ],
            },
            call_id=call["id"],
        )
        state["coverage"] = {"unavailable_segments": 1}
        state["finalization"] = {"status": "partial", "reason": "raw_expired_before_interpretation"}
        service._store_conversation(s, who, row, state)
    service.analysis(who, call["id"])
    clock.now += timedelta(seconds=301)
    packet = (
        workspace["client"].get(f"/v1/leads/{call['lead_id']}/packet", headers=workspace["headers"]).json()
    )
    brief = packet["prospect_brief"]
    assert brief["prior_answers"][0]["text"] == "Operations owns the evaluation."
    assert brief["unanswered_questions"][0]["id"] == "question"
    assert brief["next_question"] == "Which approval step needs clarification?"
    assert brief["interpretation_gaps"][0]["call_id"] == call["id"]
    assert brief["interpretation_gaps"][0]["status"] == "partial"
    assert brief["source_versions"] and brief["freshness"]["state"] in {
        "missing",
        "stale",
        "ready",
        "fallback",
    }
    assert "overall_score" not in brief


def test_note_only_task_edit_cannot_silently_resolve_source_conflict(workspace, app, clock):
    from app.db import update

    call = start(workspace)
    finish(workspace, call, callback_at=(clock.now + timedelta(days=1)).isoformat())
    task = tasks(workspace, call)[0]
    with app.state.db.transaction() as s:
        record = app.state.service.required(s, "followup_tasks", task["id"], p(workspace))
        update(record, source_stale=True, review_required=True, confirmation_state="needs_review")
        version = record.version
    response = workspace["client"].patch(
        f"/v1/tasks/{task['id']}",
        headers=workspace["headers"],
        json={"request_id": str(uuid4()), "expected_version": version, "note": "Added a human reminder."},
    )
    assert response.status_code == 200, response.text
    assert response.json()["review_required"] and response.json()["confirmation_state"] == "needs_review"


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


def test_cancel_meeting_without_explicit_next_action_is_rejected_and_keeps_original_hold(workspace):
    call = start(workspace)
    finish(workspace, call, "meeting_booked")
    c, h = workspace["client"], workspace["headers"]
    path = "/v1/leads/" + call["lead_id"]
    lead = c.get(path + "/packet", headers=h).json()["lead"]
    result = c.post(
        path + "/meeting",
        headers=h,
        json={"request_id": str(uuid4()), "status": "cancelled", "expected_version": lead["version"]},
    )
    assert result.status_code == 422, result.text
    assert "next_action" in result.text
    current = c.get(path + "/packet", headers=h).json()["lead"]
    assert current["outreach_hold"] == "meeting_booked"
    assert current["meeting"]["status"] == "booked"


def test_wrapup_and_refresh_job_commit_together(workspace, app, monkeypatch):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    ended = c.post("/v1/calls/" + call["id"] + "/hangup", headers=h).json()
    original = app.state.service.jobs.enqueue_in

    def fail_prepare(s, queue, *args, **kwargs):
        if queue == "scripts":
            raise RuntimeError("simulated queue persistence failure")
        return original(s, queue, *args, **kwargs)

    monkeypatch.setattr(app.state.service.jobs, "enqueue_in", fail_prepare)
    with pytest.raises(RuntimeError):
        c.post(
            "/v1/calls/" + call["id"] + "/disposition",
            headers=h,
            json={"request_id": str(uuid4()), "expected_version": ended["version"], "outcome": "right_party"},
        )
    with app.state.db.transaction(False) as s:
        persisted = app.state.service.required(s, "call_attempts", call["id"], p(workspace))
        assert persisted.data["state"] != "FINALIZED"
        assert not list(s.scalars(select(Job).where(Job.queue == "scripts")))


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
