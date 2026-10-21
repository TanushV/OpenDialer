"""Note conflicts are human edits, not finalization bookkeeping on the note row."""

from test_wrapup_conflicts import ended_call


def analysis(workspace, call_id):
    response = workspace["client"].get(f"/v1/calls/{call_id}/analysis", headers=workspace["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def save_notes(workspace, call_id, text, **versions):
    return workspace["client"].patch(
        f"/v1/calls/{call_id}/notes", headers=workspace["headers"], json={"text": text, **versions}
    )


def finalization_rewrites_the_note_row(app, workspace, call_id):
    """The unavailable-rundown publication and other views re-materialize derived note fields."""
    from app.db import update
    from test_remediation_privacy import principal

    service = app.state.service
    with service.db.transaction() as s:
        note = service._by_key(s, principal(workspace), "notes", call_id)
        if note is None:
            call = service.required(s, "call_attempts", call_id, principal(workspace))
            note, _ = service._materialize_views_in(s, principal(workspace), call, scorecard=False)
        update(note, derived_state_revision=note.data.get("derived_state_revision", 0) + 1)


def test_finalization_after_the_rep_read_the_notes_does_not_reject_the_human_save(workspace, app):
    call = ended_call(workspace)
    read = analysis(workspace, call["id"])
    finalization_rewrites_the_note_row(app, workspace, call["id"])
    saved = save_notes(
        workspace, call["id"], "Rep correction.", expected_edit_version=read["notes_edit_version"]
    )
    assert saved.status_code == 200, saved.text
    assert analysis(workspace, call["id"])["human_notes"] == "Rep correction."


def test_another_human_edit_still_conflicts(workspace, app):
    call = ended_call(workspace)
    read = analysis(workspace, call["id"])
    first = save_notes(workspace, call["id"], "First.", expected_edit_version=read["notes_edit_version"])
    assert first.status_code == 200, first.text
    stale = save_notes(workspace, call["id"], "Second.", expected_edit_version=read["notes_edit_version"])
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "NOTES_VERSION_CONFLICT"
    assert analysis(workspace, call["id"])["human_notes"] == "First."


def test_old_clients_keep_the_whole_row_version_check(workspace, app):
    call = ended_call(workspace)
    read = analysis(workspace, call["id"])
    finalization_rewrites_the_note_row(app, workspace, call["id"])
    stale = save_notes(workspace, call["id"], "Old client.", expected_version=read["notes_version"])
    assert stale.status_code == 409, stale.text
