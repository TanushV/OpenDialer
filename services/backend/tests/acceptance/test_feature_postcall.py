"""Post-call and coaching over HTTP: rundown, scores, rubrics, notes, trackers, next-call brief, goals and replay."""

import pytest
from journey import answered_by_human, call, get, post, run_jobs, start, wrap_up

PASSWORD = "a-test-only-password-123"


def send(ws, method, path, **kwargs):
    return ws["client"].request(method, path, headers=ws["headers"], **kwargs)


def chunk(ws, call_id, segment_id, text, *, side="remote"):
    return post(
        ws,
        f"/v1/calls/{call_id}/transcript",
        {"segment_id": segment_id, "revision": 1, "side": side, "is_final": True, "text": text},
    )


def finished_call(ws, *, outcome="not_interested"):
    """An answered call with an opener and one budget remark, hung up and wrapped up."""
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "r1", "Hi, this is Example Seller calling about a brief introduction.", side="rep")
    chunk(ws, call_id, "s1", "We have no budget for this right now.")
    call(ws, "POST", f"/v1/calls/{call_id}/hangup")
    wrap_up(ws, call_id, outcome=outcome, contact_role="right_party")
    return call_id


def test_rundown_finalizes_honestly_when_no_model_is_configured(workspace):
    ws = workspace
    cid = finished_call(ws)
    run_jobs(ws)
    live = get(ws, f"/v1/calls/{cid}")
    assert live["state"] == "FINALIZED" and live["outcome"] == "not_interested"
    assert live["rundown_status"] == "partial"
    an = get(ws, f"/v1/calls/{cid}/analysis")
    assert an["finalization"]["status"] == "partial" and an["finalization"]["reason"] == "ai_unavailable"
    assert an["notes"]["review_required"] is True and "No model rundown" in an["notes"]["summary"]
    assert an["notes"]["outcome"] == "not_interested" and live["suggested_outcome"] is None
    assert an["interpretation_coverage"]["total_segments"] == 2
    assert an["interpretation_coverage"]["interpreted_segments"] == 0
    assert {t["side"] for t in an["transcript"]} == {"rep", "remote"}
    items = an["scorecard"]["items"]
    assert len(items) == 7 and all(i["status"] == "INSUFFICIENT_EVIDENCE" for i in items)
    assert an["scorecard"]["overall"]["scorable_items"] == 0


def test_human_notes_save_against_the_current_edit_version(workspace):
    ws = workspace
    cid = finished_call(ws)
    run_jobs(ws)
    saved = call(
        ws,
        "PATCH",
        f"/v1/calls/{cid}/notes",
        json={"expected_edit_version": 0, "text": "Wants a pricing sheet."},
    )
    assert saved["notes_edit_version"] == 1 and saved["human_notes"] == "Wants a pricing sheet."
    stale = send(
        ws, "PATCH", f"/v1/calls/{cid}/notes", json={"expected_edit_version": 0, "text": "Overwrite attempt"}
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {"code": "NOTES_VERSION_CONFLICT", "current_edit_version": 1}
    edited = call(
        ws,
        "PATCH",
        f"/v1/calls/{cid}/notes",
        json={"expected_edit_version": 1, "text": "Wants pricing by Friday."},
    )
    assert edited["notes_edit_version"] == 2
    run_jobs(ws)
    an = get(ws, f"/v1/calls/{cid}/analysis")
    assert an["human_notes"] == "Wants pricing by Friday." and an["status"] == "human_confirmed"
    assert an["notes_edit_version"] == 2, "Machine finalization never moves the human edit version"


@pytest.mark.slow
def test_next_call_brief_carries_prior_conversation_before_the_next_call(workspace):
    ws = workspace
    cid = finished_call(ws)
    lead_id = get(ws, f"/v1/calls/{cid}")["lead_id"]
    call(
        ws,
        "PATCH",
        f"/v1/calls/{cid}/notes",
        json={"expected_edit_version": 0, "text": "Wants a pricing sheet."},
    )
    brief = get(ws, f"/v1/leads/{lead_id}/packet")
    assert [c["id"] for c in brief["previous_calls"]] == [cid]
    assert brief["previous_calls"][0]["outcome"] == "not_interested"
    assert brief["prospect_brief"]["human_notes"] == [{"call_id": cid, "text": "Wants a pricing sheet."}]

    run_jobs(ws)
    refreshed = get(ws, f"/v1/leads/{lead_id}/packet")["prospect_brief"]
    assert cid in [v["call_id"] for v in refreshed["source_versions"]]
    assert refreshed["evidence_status"] == "observed"
    assert [g["call_id"] for g in refreshed["interpretation_gaps"]] == [cid], (
        "The unavailable rundown is disclosed"
    )

    call(
        ws,
        "PATCH",
        f"/v1/calls/{cid}/notes",
        json={"expected_edit_version": 1, "text": "Wants pricing by Friday."},
    )
    updated = get(ws, f"/v1/leads/{lead_id}/packet")["prospect_brief"]
    assert [n["text"] for n in updated["human_notes"]] == ["Wants pricing by Friday."]
