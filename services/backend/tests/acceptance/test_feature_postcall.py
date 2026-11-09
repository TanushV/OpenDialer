"""After the call over HTTP (R10.1, R10.2, R10.9): Review shows the summary, the rep's note and the transcript, and the
next call's brief carries what earlier calls left."""

from journey import answered_by_human, call, get, post, run_jobs, start, wrap_up


def chunk(ws, call_id, segment_id, text, *, side="remote"):
    return post(
        ws,
        f"/v1/calls/{call_id}/transcript",
        {"segment_id": segment_id, "revision": 1, "side": side, "is_final": True, "text": text},
    )


def finished_call(ws, *, outcome="not_interested", said="We have no budget for this right now."):
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "r1", "Hi, this is Example Seller calling about a brief introduction.", side="rep")
    chunk(ws, call_id, "s1", said)
    call(ws, "POST", f"/v1/calls/{call_id}/hangup")
    wrap_up(ws, call_id, outcome=outcome, contact_role="right_party")
    return call_id


def test_review_shows_the_outcome_summary_transcript_and_call_shape(workspace):
    ws = workspace
    call(ws, "PUT", "/v1/admin/settings", json={"openrouter_api_key": "test-openrouter"})
    cid = finished_call(ws)
    run_jobs(ws)
    review = get(ws, f"/v1/calls/{cid}/review")
    assert review["call"]["state"] == "FINALIZED" and review["call"]["outcome"] == "not_interested"
    assert review["summary"]["status"] == "ready"
    assert "We have no budget for this right now." in review["summary"]["summary"]
    assert {t["side"] for t in review["transcript"]} == {"rep", "remote"}
    assert review["metrics"]["total_segments"] == 2 and review["human_notes"] is None
    assert review["script_trace"] is not None, (
        "The script trace is written with the summary, for script learning"
    )


def test_without_a_model_key_review_still_works_and_says_what_to_add(workspace):
    ws = workspace
    cid = finished_call(ws)
    run_jobs(ws)
    review = get(ws, f"/v1/calls/{cid}/review")
    assert review["summary"]["status"] == "unavailable"
    assert "OpenRouter" in review["summary"]["error"]
    assert review["call"]["outcome"] == "not_interested" and len(review["transcript"]) == 2


def test_the_reps_note_is_saved_as_written_and_the_last_save_wins(workspace):
    ws = workspace
    cid = finished_call(ws)
    assert call(ws, "PATCH", f"/v1/calls/{cid}/notes", json={"text": "Wants a pricing sheet."}) == {
        "human_notes": "Wants a pricing sheet."
    }
    call(ws, "PATCH", f"/v1/calls/{cid}/notes", json={"text": "Wants pricing by Friday."})
    run_jobs(ws)
    assert get(ws, f"/v1/calls/{cid}/review")["human_notes"] == "Wants pricing by Friday."


def test_the_next_calls_brief_carries_the_earlier_summary_next_step_and_note(workspace):
    ws = workspace
    call(ws, "PUT", "/v1/admin/settings", json={"openrouter_api_key": "test-openrouter"})
    cid = finished_call(ws, outcome="callback_requested", said="Call me back next week, it works then.")
    lead_id = get(ws, f"/v1/calls/{cid}")["lead_id"]
    call(ws, "PATCH", f"/v1/calls/{cid}/notes", json={"text": "Wants a pricing sheet."})
    before = get(ws, f"/v1/leads/{lead_id}/packet")
    assert [c["id"] for c in before["previous_calls"]] == [cid]
    assert before["prospect_brief"]["summary"] == "No earlier call summary yet."
    assert before["prospect_brief"]["human_notes"] == [{"call_id": cid, "text": "Wants a pricing sheet."}]
    run_jobs(ws)
    brief = get(ws, f"/v1/leads/{lead_id}/packet")["prospect_brief"]
    assert "Call me back next week" in brief["summary"]
    assert brief["earlier_calls"][0]["call_id"] == cid
    assert brief["earlier_calls"][0]["outcome"] == "callback_requested"
    assert brief["evidence_status"] == "observed"
