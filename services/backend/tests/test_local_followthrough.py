"""Local-only follow-through: identities, conflicts, holds and recovery. Tests first."""

from datetime import timedelta
from uuid import uuid4

import pytest
from app.compliance.policy import authorize
from app.db import Job, put, update
from conftest import disposition_body, start
from sqlalchemy import select
from test_remediation_rep_workflow import finish, p


@pytest.mark.parametrize("original", ["meeting_held", "meeting_booked"])
def test_corrected_meeting_disposition_retracts_only_its_derived_evidence(workspace, app, clock, original):
    from app.db import doc
    from app.optimization.learning import conversion_at, training_rows
    from app.optimization.projections import lead_values

    call = start(workspace)
    finish(workspace, call, original)
    before = clock.now
    clock.now += timedelta(minutes=1)
    c, h = workspace["client"], workspace["headers"]
    corrected = c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json=disposition_body(c, h, call["id"], outcome="right_party", contact_role="right_party"),
    )
    assert corrected.status_code == 200, corrected.text
    service, actor = app.state.service, p(workspace)
    with service.db.transaction(False) as s:
        lead = service.required(s, "leads", call["lead_id"], actor)
        event = service._by_key(s, actor, "conversion_events", "rep-meeting:" + lead.id)
        calls = [doc(service.required(s, "call_attempts", call["id"], actor))]
        events = [doc(event)] if event else []
        config = service.optimization_config(s, actor, workspace["campaign_id"])
        now_values = lead_values(doc(lead), calls, events, suppressed=False, config=config, now=clock.now)
        assert now_values["held_meetings"] == 0 and now_values["unique_conversions"] == 0
        assert lead.data.get("conversion_goal_achieved") is None
        assert lead.data["meeting"]["status"] == "needs_review"
        assert lead.data["outreach_hold"] == "meeting_next_action"
        features = [doc(service.required(s, "feature_snapshots", calls[0]["feature_snapshot_id"], actor))]
        learned = training_rows(calls, features, events, now=clock.now, goal="meeting_held", window_days=14)
        assert learned[0]["conversion"] is None
        if original == "meeting_held":
            assert event.data["retracted"] is True
            assert conversion_at(doc(event), before)["retracted"] is False
            historical = lead_values(doc(lead), calls, events, suppressed=False, config=config, now=before)
            assert historical["held_meetings"] == 1
    packet = c.get(f"/v1/leads/{call['lead_id']}/packet", headers=h).json()
    reviewed = c.post(
        f"/v1/leads/{call['lead_id']}/meeting",
        headers=h,
        json={
            "request_id": str(uuid4()),
            "expected_version": packet["lead"]["version"],
            "status": "cancelled",
            "next_action": "resume",
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["outreach_hold"] is None


@pytest.mark.parametrize("corrected_outcome", ["right_party", "meeting_booked"])
def test_call_correction_cannot_retract_a_later_independent_meeting_report(
    workspace, app, clock, corrected_outcome
):
    call = start(workspace)
    finish(workspace, call, "meeting_held")
    clock.now += timedelta(minutes=1)
    c, h = workspace["client"], workspace["headers"]
    path = f"/v1/leads/{call['lead_id']}"
    packet = c.get(path + "/packet", headers=h).json()
    confirmed = c.post(
        path + "/meeting",
        headers=h,
        json={
            "request_id": str(uuid4()),
            "expected_version": packet["lead"]["version"],
            "status": "held",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    clock.now += timedelta(minutes=1)
    changed = c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json=disposition_body(c, h, call["id"], outcome=corrected_outcome),
    )
    assert changed.status_code == 200, changed.text
    with app.state.service.db.transaction(False) as s:
        event = app.state.service._by_key(
            s, p(workspace), "conversion_events", "rep-meeting:" + call["lead_id"]
        )
        assert event.data["retracted"] is False
        lead = app.state.service.required(s, "leads", call["lead_id"], p(workspace))
        assert lead.data["meeting"]["status"] == "held"


def test_legacy_unlinked_meeting_requires_source_review_before_disposition_correction(workspace, app):
    from app.db import update

    call = start(workspace)
    finish(workspace, call, "meeting_held")
    with app.state.service.db.transaction() as s:
        lead = app.state.service.required(s, "leads", call["lead_id"], p(workspace))
        update(lead, meeting={k: v for k, v in lead.data["meeting"].items() if k != "source_call_id"})
    c, h = workspace["client"], workspace["headers"]
    response = c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json=disposition_body(c, h, call["id"], outcome="right_party"),
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "meeting_source_review_required"


@pytest.mark.postgres
def test_wrapup_keeps_call_locked_through_version_check_and_commit(workspace, app, monkeypatch):
    """Background interpretation cannot invalidate an already-checked human edit."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.db import get, update
    from sqlalchemy import event

    service, actor = app.state.service, p(workspace)
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    ended = c.post(f"/v1/calls/{call['id']}/hangup", headers=h).json()
    reached = threading.Event()
    original = service.check_edit_version
    futures = []

    def query(connection, cursor, statement, *args):
        if threading.current_thread().name.startswith("finalizer") and "FOR UPDATE" in statement:
            reached.set()

    def finalize():
        with service.db.transaction() as s:
            row = get(s, "call_attempts", call["id"])
            s.refresh(row, with_for_update=True)
            update(row, transcript_version=row.data["transcript_version"] + 1)

    event.listen(service.db.engine, "before_cursor_execute", query)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="finalizer") as pool:

            def checked(row, body):
                original(row, body)
                futures.append(pool.submit(finalize))
                assert reached.wait(5)
                # Without the source lock the real publisher finishes before the edit.
                if not service.db.sqlite:
                    try:
                        futures[-1].result(timeout=0.2)
                    except TimeoutError:
                        pass

            monkeypatch.setattr(service, "check_edit_version", checked)
            saved = service.save_disposition(
                actor,
                call["id"],
                {
                    "request_id": str(uuid4()),
                    "expected_version": ended["version"],
                    "outcome": "meeting_booked",
                    "contact_role": "right_party",
                },
            )
            for future in futures:
                future.result(timeout=8)
        assert saved["outcome"] == "meeting_booked"
        with service.db.transaction(False) as s:
            current = get(s, "call_attempts", call["id"])
            assert current.data["transcript_version"] == ended["transcript_version"] + 1
            assert current.data["outcome"] == "meeting_booked"
    finally:
        event.remove(service.db.engine, "before_cursor_execute", query)


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


def test_wrapup_note_is_saved_as_human_context_without_waiting_for_inference(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    result = c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json=disposition_body(
            c, h, call["id"], outcome="right_party", callback_note="Rep confirmed the purchasing owner."
        ),
    )
    assert result.status_code == 200, result.text
    analysis = c.get(f"/v1/calls/{call['id']}/analysis", headers=h).json()
    assert analysis["human_notes"] == "Rep confirmed the purchasing owner."
    brief = c.get(f"/v1/leads/{call['lead_id']}/packet", headers=h).json()["prospect_brief"]
    assert brief["human_notes"][0]["text"] == analysis["human_notes"]


def test_alias_reconciliation_retains_human_note_and_protection_on_survivor(workspace, app, clock):
    call = start(workspace)
    finish(workspace, call, callback_at=(clock.now + timedelta(days=1)).isoformat())
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
                "human_edited": True,
                "note": "Do not change the date the rep confirmed.",
            },
            key="legacy-human-note",
            now=clock() + timedelta(seconds=1),
        )
        alias_id = duplicate.id
    service.migrate_followthrough()
    survivor = tasks(workspace, call)[0]
    assert survivor["human_edited"]
    assert survivor["retained_notes"] == [
        {"task_id": alias_id, "text": "Do not change the date the rep confirmed."}
    ]


@pytest.mark.parametrize("ambiguous", [False, True])
def test_manual_wrapup_preserves_source_identity_and_never_adds_an_ambiguous_third_callback(
    workspace, app, clock, ambiguous
):
    call = start(workspace)
    service, who = app.state.service, p(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    with service.db.transaction() as s:
        for index in range(2 if ambiguous else 1):
            promise = put(
                s,
                "commitments",
                who["tenant_id"],
                who["seller_id"],
                {
                    "lead_id": call["lead_id"],
                    "call_id": call["id"],
                    "kind": "callback",
                    "action": "Revisit the purchasing discussion",
                    "status": "confirmed",
                },
                now=clock(),
            )
            task = put(
                s,
                "followup_tasks",
                who["tenant_id"],
                who["seller_id"],
                {
                    "lead_id": call["lead_id"],
                    "call_id": call["id"],
                    "commitment_id": promise.id,
                    "kind": "callback",
                    "source": "inferred",
                    "status": "open",
                    "action": "Call back",
                    "owner_id": who["user_id"],
                    "due_at": (clock() + timedelta(days=index + 1)).isoformat(),
                },
                key="commitment:" + promise.id,
                now=clock(),
            )
            task_id, key = task.id, task.external_key
    response = c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json=disposition_body(
            c,
            h,
            call["id"],
            outcome="callback_requested",
            callback_at=(clock() + timedelta(days=1)).isoformat(),
        ),
    )
    if ambiguous:
        # The rep's save never fails; the two existing callbacks are named for review.
        assert response.status_code == 200, response.text
        assert response.json()["callback_review"]["status"] == "needs_review"
        assert response.json()["canonical_task_id"] is None
        assert len(tasks(workspace, call)) == 2
    else:
        assert response.status_code == 200, response.text
        assert response.json()["canonical_task_id"] == task_id
        with service.db.transaction(False) as s:
            assert service.required(s, "followup_tasks", task_id, who).external_key == key
        repeated = c.post(
            f"/v1/calls/{call['id']}/disposition",
            headers=h,
            json=disposition_body(
                c,
                h,
                call["id"],
                outcome="callback_requested",
                callback_at=(clock() + timedelta(days=1)).isoformat(),
                callback_note="The rep clarified the follow-up purpose.",
            ),
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["canonical_task_id"] == task_id and len(tasks(workspace, call)) == 1
        changed = c.post(
            f"/v1/calls/{call['id']}/disposition",
            headers=h,
            json=disposition_body(c, h, call["id"], outcome="no_answer"),
        )
        assert changed.status_code == 200, changed.text
        assert tasks(workspace, call)[0]["status"] == "cancelled"


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
    assert replay.json()["outcome_id"] == first.json()["outcome_id"]
    assert replay.json()["packet_version"] == changed.json()["packet_version"]


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
        assert service.preparation_status_in(s, who)[0]["queued"] == 1
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


def test_task_owner_is_read_after_authorization_fence(workspace, app, clock, monkeypatch):
    from app.db import update
    from fastapi import HTTPException

    call = start(workspace)
    finish(workspace, call, callback_at=(clock.now + timedelta(days=1)).isoformat())
    task = tasks(workspace, call)[0]
    service, who = app.state.service, p(workspace)
    who = {**who, "role": "rep"}
    original = service.db.authorize_stream

    def reassign_at_fence(session, principal):
        original(session, principal)
        row = service.required(session, "followup_tasks", task["id"], principal)
        update(row, owner_id="another-rep")
        session.flush()

    monkeypatch.setattr(service.db, "authorize_stream", reassign_at_fence)
    with pytest.raises(HTTPException) as denied:
        service.edit_task(
            who,
            task["id"],
            {
                "request_id": str(uuid4()),
                "expected_version": task["version"],
                "note": "Stale owner must not edit",
            },
        )
    assert denied.value.status_code == 403


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


def test_followthrough_backfill_resumes_after_committed_page_without_reediting_human_tasks(
    workspace, app, clock, monkeypatch
):
    from contextlib import contextmanager

    from app.db import MODELS

    service, who = app.state.service, p(workspace)
    with service.db.transaction() as s:
        for index in range(503):
            put(
                s,
                "followup_tasks",
                who["tenant_id"],
                who["seller_id"],
                {
                    "kind": "callback",
                    "lead_id": f"legacy-{index}",
                    "status": "open",
                    "owner_id": who["user_id"],
                    "due_at": clock().isoformat(),
                    "human_edited": True,
                    "note": f"Human note {index}",
                },
                object_id=f"legacy-{index:04d}",
                now=clock(),
            )
    original = service.db.transaction
    calls = 0

    @contextmanager
    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("Synthetic restart after committed page")
        with original(*args, **kwargs) as session:
            yield session

    monkeypatch.setattr(service.db, "transaction", interrupted)
    with pytest.raises(RuntimeError, match="committed page"):
        service.migrate_followthrough()
    monkeypatch.setattr(service.db, "transaction", original)
    with original(False) as s:
        marker = service.required(s, "sellers", who["seller_id"], who).data["completion_contracts"][
            "followthrough"
        ]
        assert marker["last_id"] == "legacy-0499" and not marker["completed_at"]
    service.migrate_followthrough()
    with original(False) as s:
        before = {r.id: (r.version, dict(r.data)) for r in s.scalars(select(MODELS["followup_tasks"]))}
        assert len(before) == 503
        assert all(v[1]["confirmation_state"] == "confirmed" for v in before.values())
        assert all(v[1]["note"] == f"Human note {int(k[-4:])}" for k, v in before.items())
    service.migrate_followthrough()
    with original(False) as s:
        assert {
            r.id: (r.version, dict(r.data)) for r in s.scalars(select(MODELS["followup_tasks"]))
        } == before


def test_next_call_brief_reuses_prior_answers_questions_and_exposes_expired_gaps(workspace, app, clock):
    from app.coaching.context import upgrade_state
    from app.postcall.rubrics import effective
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
            rubric=effective(row.data),
        )
        state["coverage"] = {"unavailable_segments": 1}
        state["finalization"] = {"status": "partial", "reason": "raw_expired_before_interpretation"}
        service._store_conversation(s, who, row, state)
        # The finalization writer, not a read, materializes the call's note for the brief.
        service._materialize_views_in(s, who, row, scorecard=False)
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
    current = response.json()
    confirm = {
        "request_id": str(uuid4()),
        "expected_version": current["version"],
        "confirmation_state": "confirmed",
        "status": "open",
    }
    path = f"/v1/tasks/{task['id']}"
    refused = workspace["client"].patch(path, headers=workspace["headers"], json=confirm)
    assert refused.status_code == 422, refused.text
    accepted = workspace["client"].patch(
        path,
        headers=workspace["headers"],
        json={**confirm, "confirmation_reason": "I independently confirmed the promised time."},
    )
    assert accepted.status_code == 200, accepted.text
    assert not accepted.json()["review_required"]
    assert accepted.json()["source_stale"], "Rep resolution must not falsify original source validity"
    assert accepted.json()["confirmation_reason"] == "I independently confirmed the promised time."


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
            json={"request_id": str(uuid4()), "status": "completed", "expected_version": task["version"]},
        ).status_code
        == 200
    )
    assert (
        c.post(
            "/v1/calls/" + call["id"] + "/disposition",
            headers=h,
            json=disposition_body(c, h, call["id"], **{"outcome": "callback_requested", "callback_at": due}),
        ).status_code
        == 200
    )
    assert len(tasks(workspace, call)) == 1
    assert tasks(workspace, call)[0]["status"] == "completed"
    with app.state.db.transaction(False) as s:
        records = [
            r
            for r in app.state.service.scoped(s, "outcomes", p(workspace))
            if r.external_key == "disposition:" + call["id"]
        ]
        assert len(records) == 1


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_terminal_task_clears_only_its_callback(workspace, app, clock, status):
    call = start(workspace)
    finish(workspace, call, callback_at=(clock.now + timedelta(days=1)).isoformat())
    task = tasks(workspace, call)[0]
    c, h = workspace["client"], workspace["headers"]
    result = c.patch(
        "/v1/tasks/" + task["id"],
        headers=h,
        json={"request_id": str(uuid4()), "status": status, "expected_version": task["version"]},
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
            json={
                "request_id": str(uuid4()),
                "due_at": new_due,
                "timezone": "America/Los_Angeles",
                "expected_version": task["version"],
            },
        ).status_code
        == 200
    )
    assert (
        c.post(
            "/v1/calls/" + call["id"] + "/disposition",
            headers=h,
            json=disposition_body(
                c, h, call["id"], **{"outcome": "callback_requested", "callback_at": old_due}
            ),
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
        json={"request_id": str(uuid4()), "status": "open", "expected_version": task["version"], **edit},
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
        json={"request_id": str(uuid4()), "owner_id": rep.id, "expected_version": task["version"]},
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
            "request_id": str(uuid4()),
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
            path + "/meeting",
            headers=h,
            json={"request_id": str(uuid4()), "status": "held", "expected_version": lead["version"]},
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
    service.edit_task(
        p(workspace),
        task["id"],
        {"request_id": str(uuid4()), "owner_id": rep.id, "expected_version": task["version"]},
    )
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
        json=disposition_body(
            c, h, call["id"], **{"outcome": "callback_requested", "callback_at": "2026-10-20T12:00:00"}
        ),
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
        json={
            "request_id": str(uuid4()),
            "status": "booked",
            "expected_version": lead["version"],
            "owner_id": "ignored",
        },
    )
    assert result.status_code == 422


def test_null_task_status_cannot_remove_task_from_the_workflow(workspace, clock):
    call = start(workspace)
    finish(workspace, call, callback_at=(clock.now + timedelta(days=1)).isoformat())
    task = tasks(workspace, call)[0]
    response = workspace["client"].patch(
        "/v1/tasks/" + task["id"],
        headers=workspace["headers"],
        json={"request_id": str(uuid4()), "status": None, "expected_version": task["version"]},
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
            json={"request_id": str(uuid4()), "status": status, "expected_version": lead["version"], **extra},
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
        json=disposition_body(
            c, h, call["id"], **{"outcome": "meeting_booked", "expected_version": old["version"]}
        ),
    )
    assert saved.status_code == 200
    stale = c.post(
        path + "/disposition",
        headers=h,
        json=disposition_body(
            c, h, call["id"], **{"outcome": "no_answer", "expected_version": old["version"]}
        ),
    )
    assert stale.status_code == 409
    assert c.get(path, headers=h).json()["outcome"] == "meeting_booked"


# W3 task semantics: one identity, explicit review states, bounded suggestions.


def inferred_callback(service, who, call, clock, *, status="open", human_edited=False, days=1):
    with service.db.transaction() as s:
        promise = put(
            s,
            "commitments",
            who["tenant_id"],
            who["seller_id"],
            {
                "lead_id": call["lead_id"],
                "call_id": call["id"],
                "kind": "callback",
                "owner": "rep",
                "action": "Revisit the purchasing discussion",
                "status": "confirmed",
            },
            now=clock(),
        )
        task = put(
            s,
            "followup_tasks",
            who["tenant_id"],
            who["seller_id"],
            {
                "lead_id": call["lead_id"],
                "call_id": call["id"],
                "commitment_id": promise.id,
                "kind": "callback",
                "source": "inferred",
                "status": status,
                "human_edited": human_edited,
                "action": "Call back",
                "owner_id": who["user_id"],
                "timezone": "America/Los_Angeles",
                "due_at": (clock() + timedelta(days=days)).isoformat()
                if status != "needs_confirmation"
                else None,
            },
            key="commitment:" + promise.id,
            now=clock(),
        )
        update(promise, task_id=task.id)
        return task.id, promise.id


def task_row(service, who, task_id):
    with service.db.transaction(False) as s:
        return service.required(s, "followup_tasks", task_id, who).data


def test_non_callback_outcome_cancels_the_calls_untouched_inferred_callbacks(workspace, app, clock):
    service, who = app.state.service, p(workspace)
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    untouched, _ = inferred_callback(service, who, call, clock)
    edited, _ = inferred_callback(service, who, call, clock, human_edited=True, days=2)
    saved = c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json=disposition_body(c, h, call["id"], outcome="no_answer"),
    )
    assert saved.status_code == 200, saved.text
    assert task_row(service, who, untouched)["status"] == "cancelled"
    assert task_row(service, who, edited)["status"] == "open", (
        "A rep's own edit is never cancelled by a model"
    )


@pytest.mark.parametrize("existing", ["ambiguous", "human_edited", "completed"])
def test_wrapup_callback_never_fails_the_save_and_reports_needs_review(workspace, app, clock, existing):
    service, who = app.state.service, p(workspace)
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    first, _ = inferred_callback(
        service, who, call, clock, human_edited=existing == "human_edited", status="open"
    )
    others = [inferred_callback(service, who, call, clock, days=2)[0]] if existing == "ambiguous" else []
    if existing == "completed":
        with service.db.transaction() as s:
            update(service.required(s, "followup_tasks", first, who), status="completed", human_edited=True)
    before = {t: task_row(service, who, t) for t in [first, *others]}
    requested = (clock.now + timedelta(days=5)).isoformat()
    saved = c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json=disposition_body(c, h, call["id"], outcome="callback_requested", callback_at=requested),
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["outcome"] == "callback_requested", "The rep's outcome is always saved"
    assert body["callback_review"]["status"] == "needs_review"
    assert set(body["callback_review"]["task_ids"]) == {first, *others}
    assert len(tasks(workspace, call)) == 1 + len(others), "No ambiguous extra callback"
    for task_id, previous in before.items():
        current = task_row(service, who, task_id)
        assert current["due_at"] == previous["due_at"] and current["status"] == previous["status"]
        if current["status"] != "completed":
            assert current["confirmation_state"] == "needs_review" and current["review_required"]
            assert current["suggested_update"]["due_at"] == requested


def test_cancelling_a_task_closes_its_promise_in_the_next_call_brief(workspace, app, clock):
    service, who = app.state.service, p(workspace)
    call = start(workspace)
    finish(workspace, call, "right_party")
    task_id, promise_id = inferred_callback(service, who, call, clock)
    current = task_row(service, who, task_id)
    with service.db.transaction(False) as s:
        version = service.required(s, "followup_tasks", task_id, who).version
    service.edit_task(
        who, task_id, {"request_id": str(uuid4()), "expected_version": version, "status": "cancelled"}
    )
    with service.db.transaction(False) as s:
        assert service.required(s, "commitments", promise_id, who).data["status"] == "cancelled"
    brief = (
        workspace["client"].get(f"/v1/leads/{call['lead_id']}/packet", headers=workspace["headers"]).json()
    )
    promise = next(c for c in brief["prospect_brief"]["commitments"] if c["id"] == promise_id)
    assert promise["status"] == "cancelled" and current["status"] == "open"


@pytest.mark.parametrize(
    "due_at,expected",
    [
        ("2026-10-21T14:00:00-04:00", 422),  # An Eastern offset for a Pacific task.
        ("2026-10-21T14:00:00-07:00", 200),
        ("2026-10-21T21:00:00Z", 200),  # A UTC instant is unambiguous.
    ],
)
def test_task_time_offset_must_match_the_task_timezone(workspace, app, clock, due_at, expected):
    service, who = app.state.service, p(workspace)
    call = start(workspace)
    finish(workspace, call, "right_party")
    task_id, _ = inferred_callback(service, who, call, clock)
    with service.db.transaction(False) as s:
        version = service.required(s, "followup_tasks", task_id, who).version
    response = workspace["client"].patch(
        f"/v1/tasks/{task_id}",
        headers=workspace["headers"],
        json={"request_id": str(uuid4()), "expected_version": version, "due_at": due_at},
    )
    assert response.status_code == expected, response.text


def test_meeting_report_keeps_human_edited_callbacks_for_review(workspace, app, clock):
    service, who = app.state.service, p(workspace)
    call = start(workspace)
    finish(workspace, call, "right_party")
    untouched, _ = inferred_callback(service, who, call, clock)
    edited, _ = inferred_callback(service, who, call, clock, human_edited=True, days=2)
    c, h = workspace["client"], workspace["headers"]
    lead = c.get(f"/v1/leads/{call['lead_id']}/packet", headers=h).json()["lead"]
    booked = c.post(
        f"/v1/leads/{call['lead_id']}/meeting",
        headers=h,
        json={"request_id": str(uuid4()), "expected_version": lead["version"], "status": "booked"},
    )
    assert booked.status_code == 200, booked.text
    assert task_row(service, who, untouched)["status"] == "cancelled"
    kept = task_row(service, who, edited)
    assert (
        kept["status"] == "open" and kept["confirmation_state"] == "needs_review" and kept["review_required"]
    )


def test_ai_suggested_callback_defers_outreach_for_a_bounded_time_only(workspace, app, clock):
    service, who = app.state.service, p(workspace)
    call = start(workspace)
    finish(workspace, call, "right_party")
    inferred_callback(service, who, call, clock, status="needs_confirmation")
    with service.db.transaction() as s:
        lead = service.required(s, "leads", call["lead_id"], who)
        service._sync_callback(s, who, lead)
    with service.db.transaction(False) as s:
        lead = service.required(s, "leads", call["lead_id"], who)
        assert lead.data.get("outreach_hold") is None, "Only a rep-stated callback holds outreach"
        deferred = lead.data["next_attempt_at"]
        assert clock.now < datetime_of(deferred) <= clock.now + timedelta(days=7)
    clock.now += timedelta(days=7, minutes=1)
    with service.db.transaction(False) as s:
        lead = service.required(s, "leads", call["lead_id"], who)
        number = service.required(s, "number_inventory", call["number_id"], who)
        reasons = authorize(service.context(s, who, lead, number)).reasons
        assert "NOT_DUE" not in reasons and "OUTREACH_HOLD" not in reasons


@pytest.mark.asyncio
async def test_wrapup_save_defers_lead_recomputation_to_the_packet_refresh_job(workspace, app, monkeypatch):
    service, who = app.state.service, p(workspace)
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    seen = []
    for name in ("refresh_conversation_features_in", "reconcile_goal_status"):
        original = getattr(service, name)
        monkeypatch.setattr(
            service,
            name,
            lambda *args, name=name, original=original, **kwargs: (
                seen.append(name) or original(*args, **kwargs)
            ),
        )
    saved = c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json=disposition_body(c, h, call["id"], outcome="right_party"),
    )
    assert saved.status_code == 200, saved.text
    assert seen == [], "The rep's save writes outcome, note, task and hold; history scans run later"
    with service.db.transaction(False) as s:
        jobs = [j for j in s.scalars(select(Job).where(Job.queue == "scripts"))]
        assert [j.payload["kind"] for j in jobs] == ["packet_refresh"]
    monkeypatch.setattr(service, "prepare", lambda *args, **kwargs: asyncio_noop())
    await service.run_jobs(who["tenant_id"], ["scripts"])
    assert set(seen) == {"refresh_conversation_features_in", "reconcile_goal_status"}


async def asyncio_noop():
    return None


def datetime_of(value):
    from app.service import dt

    return dt(value)


def test_reextraction_preserves_a_reviewed_retraction_and_rejects_only_ambiguous_items(workspace, app, clock):
    from app.postcall.commitments import publish

    service, who = app.state.service, p(workspace)
    call = start(workspace)
    finish(workspace, call, "right_party")
    item = {
        "kind": "callback",
        "owner": "rep",
        "evidence": ["s"],
        "due_at": (clock.now + timedelta(days=1)).isoformat(),
        "timezone": "America/Los_Angeles",
        "time_window": None,
        "needs_time_confirmation": False,
        "action": "Call back about procurement",
        "observation_id": "promise",
    }
    with service.db.transaction() as s:
        row = service.required(s, "call_attempts", call["id"], who)
        publish(service, s, who, row, [item])
        publish(service, s, who, row, [], retracted_observations={"promise"})
    with service.db.transaction() as s:
        row = service.required(s, "call_attempts", call["id"], who)
        other = {**item, "evidence": ["t"], "observation_id": "other", "action": "Send the pricing sheet"}
        duplicate = {**other, "observation_id": "other-copy"}
        publish(
            service,
            s,
            who,
            row,
            [
                item,
                other,
                duplicate,
                {**item, "kind": "followup", "evidence": ["u"], "observation_id": "material"},
            ],
        )
    with service.db.transaction(False) as s:
        records = {
            r.data.get("observation_id"): r.data
            for r in service.lead_records(s, who, "commitments", call["lead_id"])
        }
        tasks_now = service.lead_records(s, who, "followup_tasks", call["lead_id"])
    assert records["promise"]["status"] == "retracted", "Re-extraction never resurrects a retraction"
    assert not {"other", "other-copy"} & set(records), "Two items with one source identity are both rejected"
    assert any(r.get("kind") == "followup" for r in records.values()), "Unrelated items still publish"
    assert [t.data["status"] for t in tasks_now if t.data.get("kind") == "callback"] == ["cancelled"]
