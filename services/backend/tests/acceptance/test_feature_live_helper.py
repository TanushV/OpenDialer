"""R10.1 over HTTP: the live helper gives tips while the prospect talks and private answers when the rep asks.

Nothing else: it saves no outcome, task or interpretation. Transcripts are kept 30 days (D7, R10.5).
"""

import json
from datetime import timedelta
from uuid import uuid4

from app import ops
from journey import answered_by_human, call, get, post, run_jobs, start, wrap_up

MARKER = "Our budget sentence must stay on this computer"


def chunk(ws, call_id, segment_id, text, *, side="remote", final=True, revision=1):
    return post(
        ws,
        f"/v1/calls/{call_id}/transcript",
        {"segment_id": segment_id, "revision": revision, "side": side, "is_final": final, "text": text},
    )


def with_helper(ws):
    call(ws, "PUT", "/v1/admin/settings", json={"openrouter_api_key": "test-openrouter"})


def tip_after_speech(ws, clock, call_id):
    """Let the debounced helper job run, as the background lane does, and read the call as the Dialer does."""
    clock.now += timedelta(milliseconds=400)
    run_jobs(ws)
    return get(ws, f"/v1/calls/{call_id}")


def test_a_tip_follows_what_the_prospect_says_and_leaves_after_its_moment(workspace, clock):
    ws = workspace
    with_helper(ws)
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "r1", "What does your budget look like?", side="rep")
    assert tip_after_speech(ws, clock, call_id)["coach"] is None, "Rep speech alone has nothing to answer"
    chunk(ws, call_id, "s0", "We have no budget", final=False)
    assert tip_after_speech(ws, clock, call_id)["coach"] is None, "Interim words never drive a tip"
    chunk(ws, call_id, "s1", "We have no budget for this right now.")
    live = tip_after_speech(ws, clock, call_id)
    tip = live["coach"]
    assert tip["kind"] == "TIP" and tip["status"] == "ready" and not tip["private"]
    assert "We have no budget for this right now." in tip["say_or_ask"]
    assert live["helper_status"] == {"status": "available"}
    clock.now += timedelta(seconds=31)
    assert get(ws, f"/v1/calls/{call_id}")["coach"] is None, "A tip is for the next moment only"


def test_without_an_openrouter_key_the_helper_is_off_and_says_why(workspace, clock):
    ws = workspace
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "s1", "We have no budget for this right now.")
    live = tip_after_speech(ws, clock, call_id)
    assert live["coach"] is None
    assert live["helper_status"] == {"status": "paused", "reason": "not_configured"}
    answer = post(ws, f"/v1/calls/{call_id}/coach", {"question": "What now?", "request_id": str(uuid4())})
    assert answer["status"] == "unavailable"


def test_mute_stops_automatic_tips_but_a_private_question_is_still_answered(workspace, clock):
    ws = workspace
    with_helper(ws)
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    assert call(ws, "PATCH", f"/v1/calls/{call_id}/coach", json={"muted": True}) == {"muted": True}
    chunk(ws, call_id, "s1", "We have no budget for this right now.")
    assert tip_after_speech(ws, clock, call_id)["coach"] is None
    request_id = str(uuid4())
    answer = post(
        ws, f"/v1/calls/{call_id}/coach", {"question": "How do I handle budget?", "request_id": request_id}
    )
    assert answer == {"request_id": request_id, "private": True, "status": "shown"}
    tip = get(ws, f"/v1/calls/{call_id}")["coach"]
    assert tip["kind"] == "ANSWER" and tip["private"] and "How do I handle budget" in tip["say_or_ask"]
    clock.now += timedelta(seconds=60)
    assert get(ws, f"/v1/calls/{call_id}")["coach"]["id"] == tip["id"], "An answer stays long enough to read"
    call(ws, "PATCH", f"/v1/calls/{call_id}/coach", json={"muted": False})
    chunk(ws, call_id, "s2", "Maybe next quarter.")
    assert "Maybe next quarter." in tip_after_speech(ws, clock, call_id)["coach"]["say_or_ask"]


def test_dismiss_removes_the_tip_and_cancel_reports_when_nothing_is_running(workspace, clock):
    ws = workspace
    with_helper(ws)
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "s1", "Send me an email.")
    tip = tip_after_speech(ws, clock, call_id)["coach"]
    call(ws, "PATCH", f"/v1/calls/{call_id}/coach", json={"dismissed": tip["id"]})
    assert get(ws, f"/v1/calls/{call_id}")["coach"] is None
    cancelled = post(ws, f"/v1/calls/{call_id}/coach/cancel", {"request_id": str(uuid4())})
    assert cancelled == {"status": "not_running"}


def test_an_opt_out_ends_help_on_that_call(workspace, clock):
    ws = workspace
    with_helper(ws)
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "s1", "Please do not call me again.")
    assert tip_after_speech(ws, clock, call_id)["coach"] is None
    answer = post(ws, f"/v1/calls/{call_id}/coach", {"question": "What now?", "request_id": str(uuid4())})
    assert answer["status"] == "ended"


def test_the_tip_ends_with_the_call(workspace, clock):
    ws = workspace
    with_helper(ws)
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "s1", "Tell me more about pricing.")
    assert tip_after_speech(ws, clock, call_id)["coach"]
    call(ws, "POST", f"/v1/calls/{call_id}/hangup")
    live = get(ws, f"/v1/calls/{call_id}")
    assert live["coach"] is None and live["helper_status"] is None


def test_the_transcript_is_kept_30_days_then_deleted_while_notes_and_outcome_remain(workspace, clock):
    """D7, R10.5: transcript text stays on this computer for 30 days, replacing the old 5-minute lifetime."""
    ws = workspace
    call_id = start(ws)["id"]
    lead_id = get(ws, f"/v1/calls/{call_id}")["lead_id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "r1", "Hi, this is Example Seller calling about a brief introduction.", side="rep")
    chunk(ws, call_id, "s1", MARKER)
    assert call(ws, "PATCH", f"/v1/calls/{call_id}/notes", json={"text": "Wants a pricing sheet."}) == {
        "human_notes": "Wants a pricing sheet."
    }
    call(ws, "POST", f"/v1/calls/{call_id}/hangup")
    wrap_up(ws, call_id, outcome="not_interested", contact_role="right_party")
    run_jobs(ws)
    clock.now += timedelta(minutes=10)
    ws["client"].app.state.service.raw.clear()  # a restart empties memory; the saved text remains
    assert MARKER in json.dumps(get(ws, f"/v1/calls/{call_id}/review"))
    clock.now += timedelta(days=30)
    token = ws["client"].post("/v1/local/session", headers={"X-Local-Token": "test-local-secret"}, json={})
    ws["headers"] = {"Authorization": f"Bearer {token.json()['access_token']}"}
    ops.purge_transcripts(ws["client"].app.state.service)
    review = get(ws, f"/v1/calls/{call_id}/review")
    assert MARKER not in json.dumps(review) and review["transcript"] == []
    assert review["human_notes"] == "Wants a pricing sheet."
    assert review["call"]["outcome"] == "not_interested"
    for path in ("/v1/events", "/v1/workspace", f"/v1/leads/{lead_id}/packet"):
        assert MARKER not in json.dumps(get(ws, path)), path


def test_the_rep_can_delete_a_transcript_at_once(workspace):
    ws = workspace
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "s1", MARKER)
    call(ws, "POST", f"/v1/calls/{call_id}/hangup")
    ws["client"].app.state.service.raw.clear()
    assert MARKER in json.dumps(get(ws, f"/v1/calls/{call_id}/review"))
    assert call(ws, "DELETE", f"/v1/calls/{call_id}/transcript") == {"deleted": True}
    assert MARKER not in json.dumps(get(ws, f"/v1/calls/{call_id}/review"))


def test_a_tip_pulls_up_the_script_part_that_fits_what_the_prospect_said(workspace, clock):
    """Owner direction, November 10: the helper shows what to say and the most relevant part of the script."""
    ws = workspace
    with_helper(ws)
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "s1", "Can you just send me an email about the pricing?")
    coach = tip_after_speech(ws, clock, call_id)["coach"]
    assert coach["say_or_ask"].startswith("They said")
    assert coach["script_part"]["label"] and coach["script_part"]["text"]
