"""Transcripts feed only the live call and are never kept (work package 9.3 C5, owner decisions D1 and D7).

Speech text exists while a call is live, for the helper and the script follower, and is deleted when the call ends.
Word-free metadata (which script part matched, timing, speaker) stays for drop-off and call statistics. A Settings
switch turns live transcription and the helper off.
"""

from datetime import timedelta

from app.platform.db import MODELS, Job
from journey import call, event, get, post, run_jobs, start
from sqlalchemy import select

SCRIPT = """# Opening
Hi {first_name}, this is {rep} with {seller}.

# Objections
## Budget
Is the budget set for this year?

# Close
Would a short call next week be useful?
"""


def with_helper(ws):
    version = get(ws, "/v1/optimization/script")["base"]["version"]
    post(ws, "/v1/admin/script/import", {"markdown": SCRIPT, "expected_version": version, "preview": False})
    call(ws, "PUT", "/v1/admin/settings", json={"openrouter_api_key": "test-openrouter"})


def chunk(ws, call_id, segment_id, text, side="remote"):
    return post(
        ws,
        f"/v1/calls/{call_id}/transcript",
        {"segment_id": segment_id, "revision": 1, "side": side, "is_final": True, "text": text},
    )


def saved_text(app, call_id):
    with app.state.db.transaction(False) as s:
        model = MODELS["transcript_segments"]
        return [row.data.get("text") for row in s.scalars(select(model).where(model.ctx_key == call_id))]


def test_transcript_text_is_deleted_when_the_call_ends_and_late_text_is_discarded(workspace, app, clock):
    ws = workspace
    with_helper(ws)
    placed = start(ws)
    event(ws, placed["id"], "call.answered")
    chunk(ws, placed["id"], "s1", "We have no budget for this right now.")
    clock.now += timedelta(seconds=1)
    run_jobs(ws)
    assert get(ws, f"/v1/calls/{placed['id']}")["coach"], "The live helper uses the text during the call"
    assert any(saved_text(app, placed["id"])), "Text exists only while the call is live"
    event(ws, placed["id"], "call.hangup")
    assert get(ws, f"/v1/calls/{placed['id']}")["state"] in {"ENDED", "FINALIZED"}
    assert not any(saved_text(app, placed["id"]))
    late = chunk(ws, placed["id"], "s2", "Call me back next quarter.")
    assert late.get("unavailable") == "call_ended"
    assert not any(saved_text(app, placed["id"]))
    with app.state.db.transaction(False) as s:
        assert not [j for j in s.scalars(select(Job)) if j.payload.get("kind") == "aftercall_summary"]


def test_turning_live_transcription_off_keeps_call_audio_on_this_computer(workspace, clock):
    ws = workspace
    with_helper(ws)
    call(ws, "PUT", "/v1/admin/settings", json={"live_transcription": False})
    assert get(ws, "/v1/admin/settings")["live_transcription"] is False
    placed = start(ws)
    event(ws, placed["id"], "call.answered")
    live = get(ws, f"/v1/calls/{placed['id']}")
    assert not live.get("transcription_layout"), "No speech stream starts"
    chunk(ws, placed["id"], "s1", "We have no budget for this right now.")
    clock.now += timedelta(seconds=1)
    run_jobs(ws)
    assert not get(ws, f"/v1/calls/{placed['id']}")["coach"], "The helper does not run"
