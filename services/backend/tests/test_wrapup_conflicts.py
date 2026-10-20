"""Wrap-up conflicts are rep edits, not background bookkeeping on the call row."""

from uuid import uuid4

from conftest import start


def ended_call(workspace):
    c, h = workspace["client"], workspace["headers"]
    call = start(workspace)
    assert c.post(f"/v1/calls/{call['id']}/hangup", headers=h).status_code == 200
    return c.get(f"/v1/calls/{call['id']}", headers=h).json()


def background_write(app, workspace, call_id):
    """Maintenance (orphan checks, receipts, finalization bookkeeping) bumps the row version."""
    from app.db import update
    from test_remediation_privacy import principal

    service = app.state.service
    with service.db.transaction() as s:
        row = service.required(s, "call_attempts", call_id, principal(workspace))
        update(row, orphan_checked_at=service.clock().isoformat())


def save(workspace, call, outcome="no_answer", **extra):
    body = {
        "request_id": str(uuid4()),
        "outcome": outcome,
        "expected_version": call["version"],
        "expected_edit_version": call.get("edit_version", 0),
        **extra,
    }
    return workspace["client"].post(
        f"/v1/calls/{call['id']}/disposition", headers=workspace["headers"], json=body
    )


def test_background_bookkeeping_after_hangup_does_not_reject_the_reps_wrap_up(workspace, app):
    call = ended_call(workspace)
    background_write(app, workspace, call["id"])
    saved = save(workspace, call)
    assert saved.status_code == 200, saved.text


def test_a_concurrent_wrap_up_from_another_device_still_conflicts(workspace, app):
    call = ended_call(workspace)
    first = save(workspace, call, outcome="no_answer")
    assert first.status_code == 200, first.text
    second = save(workspace, call, outcome="voicemail")  # Same stale edit version, different input.
    assert second.status_code == 409, second.text


def test_edit_version_advances_only_on_rep_edits(workspace, app):
    call = ended_call(workspace)
    before = call.get("edit_version", 0)
    background_write(app, workspace, call["id"])
    c, h = workspace["client"], workspace["headers"]
    assert c.get(f"/v1/calls/{call['id']}", headers=h).json().get("edit_version", 0) == before
    assert save(workspace, call).status_code == 200
    assert c.get(f"/v1/calls/{call['id']}", headers=h).json()["edit_version"] == before + 1
