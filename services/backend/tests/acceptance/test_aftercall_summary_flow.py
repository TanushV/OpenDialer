"""R10.2 end to end: a call ends, one summary is written over the whole transcript, and the Dialer reads its suggestion."""

from journey import answered_by_human, call, get, run_jobs, start
from test_feature_live_helper import chunk


def use_summary_model(ws):
    ws["client"].put(
        "/v1/admin/settings", headers=ws["headers"], json={"openrouter_api_key": "sk-or-test-key"}
    )


def test_an_ended_call_gets_one_summary_and_a_suggested_outcome(workspace):
    ws = workspace
    use_summary_model(ws)
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "r1", "Could we look at this together next week?", side="rep")
    chunk(ws, call_id, "s1", "Thursday at 2 works for me.")
    call(ws, "POST", f"/v1/calls/{call_id}/hangup")
    run_jobs(ws)
    ended = get(ws, f"/v1/calls/{call_id}")
    summary = ended["aftercall"]
    assert summary["status"] == "ready" and "Thursday at 2 works" in summary["summary"]
    assert ended["suggested_outcome"]["code"] == "meeting_booked"
    assert ended["suggested_outcome"]["source"] == "summary"


def test_a_call_with_no_speech_says_so_instead_of_failing(workspace):
    ws = workspace
    use_summary_model(ws)
    call_id = start(ws)["id"]
    call(ws, "POST", f"/v1/calls/{call_id}/hangup")
    run_jobs(ws)
    assert get(ws, f"/v1/calls/{call_id}")["aftercall"]["status"] == "no_speech"


def test_without_a_model_key_the_summary_says_what_to_add(workspace):
    ws = workspace
    call_id = start(ws)["id"]
    call(ws, "POST", f"/v1/calls/{call_id}/hangup")
    run_jobs(ws)
    summary = get(ws, f"/v1/calls/{call_id}")["aftercall"]
    assert summary["status"] == "unavailable" and "OpenRouter" in summary["error"]


def test_a_summary_that_could_not_be_written_is_written_again_on_retry(workspace):
    """R10.4: a failed summary fails alone and has Retry; the call and its outcome are untouched."""
    ws = workspace
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "s1", "Send me the details by email.")
    call(ws, "POST", f"/v1/calls/{call_id}/hangup")
    run_jobs(ws)
    assert get(ws, f"/v1/calls/{call_id}")["aftercall"]["status"] == "unavailable"
    use_summary_model(ws)
    assert call(ws, "POST", f"/v1/calls/{call_id}/summary") == {"queued": True}
    run_jobs(ws)
    summary = get(ws, f"/v1/calls/{call_id}")["aftercall"]
    assert summary["status"] == "ready" and "Send me the details by email." in summary["summary"]


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
    ended = get(ws, f"/v1/calls/{call_id}")["aftercall"]
    assert ended["ended_at_part"]["number"] >= 1 and ended["ended_at_part"]["stage"]
    wrap_up(ws, call_id, outcome="not_interested")
    report = get(ws, "/v1/review/drop-off")
    assert report["answered"] == 1 and report["placed"] == 1
    rows = {row["stage"]: row for row in report["stages"]}
    ended_in = rows["Opening"]
    assert ended_in["calls"] == 1 and ended_in["outcomes"] == {"not_interested": 1}
    assert sum(row["calls"] for row in report["stages"]) == 1 and len(rows) > 1
