"""After a call: no summary (work package 9.3 C5), and drop-off still comes from word-free script matches."""

from journey import answered_by_human, call, get, run_jobs, start
from test_feature_live_helper import chunk


def use_summary_model(ws):
    ws["client"].put(
        "/v1/admin/settings", headers=ws["headers"], json={"openrouter_api_key": "sk-or-test-key"}
    )


def test_an_ended_call_gets_no_summary_because_its_transcript_is_gone(workspace):
    """Work package 9.3 C5: transcripts are deleted at call end, so no after-call summary or suggestion is made."""
    ws = workspace
    use_summary_model(ws)
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "s1", "We have no budget for this right now.")
    call(ws, "POST", f"/v1/calls/{call_id}/hangup")
    run_jobs(ws)
    ended = get(ws, f"/v1/calls/{call_id}")
    assert not ended.get("aftercall") and not ended.get("suggested_outcome")


def test_review_shows_the_script_stage_where_answered_calls_end(workspace):
    """Review uses observed rep speech even when the summary names a different stage."""
    from journey import wrap_up

    ws = workspace
    use_summary_model(ws)
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    snapshot = get(ws, f"/v1/calls/{call_id}")["script_snapshot"]
    chunk(ws, call_id, "r1", snapshot["opener"], side="rep")
    chunk(ws, call_id, "s1", "Not interested, thanks.")
    call(ws, "POST", f"/v1/calls/{call_id}/hangup")
    run_jobs(ws)
    # Work package 9.3 C5: no summary; the stage comes from word-free script matches kept after the text is gone.
    assert not get(ws, f"/v1/calls/{call_id}").get("aftercall")
    wrap_up(ws, call_id, outcome="not_interested")
    report = get(ws, "/v1/review/drop-off")
    assert report["answered"] == 1 and report["placed"] == 1
    rows = {row["stage"]: row for row in report["stages"]}
    ended_in = rows["Opening"]
    assert ended_in["calls"] == 1 and ended_in["outcomes"] == {"not_interested": 1}
    assert sum(row["calls"] for row in report["stages"]) == 1 and len(rows) > 1
