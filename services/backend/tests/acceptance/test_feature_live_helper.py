"""Live helper over HTTP: stable-chunk tips, private help, preferences, feedback and speech lifetime."""

import json
from datetime import timedelta
from uuid import uuid4

from journey import answered_by_human, call, get, post, run_jobs, start, wrap_up

MARKER = "Our budget sentence must not outlive five minutes"


def chunk(ws, call_id, segment_id, text, *, side="remote", final=True, revision=1):
    return post(
        ws,
        f"/v1/calls/{call_id}/transcript",
        {"segment_id": segment_id, "revision": revision, "side": side, "is_final": final, "text": text},
    )


def helper_tip(ws, clock, call_id):
    """Let the debounced live-helper job run, as the background lane does, and read the visible tip."""
    clock.now += timedelta(milliseconds=400)
    run_jobs(ws)
    return get(ws, f"/v1/calls/{call_id}")["coach"]


def private_help(ws, call_id, question, request_id):
    return post(
        ws,
        f"/v1/calls/{call_id}/coach",
        {"question": question, "urgent": True, "request_id": request_id},
    )


def send(ws, method, path, **kwargs):
    return ws["client"].request(method, path, headers=ws["headers"], **kwargs)


def test_tip_follows_stable_prospect_chunks_only(workspace, clock):
    ws = workspace
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "r1", "What does your budget look like?", side="rep")
    chunk(ws, call_id, "s0", "We have no budget for this", final=False)
    assert helper_tip(ws, clock, call_id) is None, "Interim words never drive guidance"
    chunk(ws, call_id, "r2", "Thanks, that helps.", side="rep")
    assert helper_tip(ws, clock, call_id) is None, "Rep speech alone has nothing to answer"
    chunk(ws, call_id, "s1", "We have no budget for this right now.")
    tip = helper_tip(ws, clock, call_id)
    assert tip["kind"] == "cached_tip" and tip["status"] == "ready" and tip["say_or_ask"]
    assert tip["evidence_segment_ids"] == ["s1"] and tip["playbook_card_ids"] == ["budget"]
    assert tip["pinned"] is False and tip["remaining_ms"] > 0
    state = get(ws, f"/v1/calls/{call_id}")["conversation_state"]
    assert state["helper_status"]["status"] == "cached_knowledge"
    clock.now += timedelta(seconds=35)
    live = get(ws, f"/v1/calls/{call_id}")
    assert live["coach"] is None, "An unpinned tip leaves the screen after its display window"
    assert live["raw_evidence_status"] == "available_temporarily"


def test_a_corrected_chunk_withdraws_the_tip_and_the_new_words_get_their_own(workspace, clock):
    ws = workspace
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "s1", "We have no budget for this right now.")
    assert helper_tip(ws, clock, call_id)["playbook_card_ids"] == ["budget"]
    chunk(ws, call_id, "s1", "We have no timing for this right now.", revision=2)
    live = get(ws, f"/v1/calls/{call_id}")
    assert live["coach"] is None, "Advice that relied on revised words is withdrawn"
    assert [segment["text"] for segment in live["transcript"]] == ["We have no timing for this right now."]
    assert helper_tip(ws, clock, call_id)["playbook_card_ids"] == ["timing"]


def test_mute_silences_automatic_tips_and_unmute_restores_them(workspace, clock):
    ws = workspace
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "s1", "We have no budget for this right now.")
    assert helper_tip(ws, clock, call_id) is not None
    prefs = call(ws, "PATCH", f"/v1/calls/{call_id}/coach", json={"muted": True})
    assert prefs["muted"] is True
    clock.now += timedelta(seconds=10)
    chunk(ws, call_id, "s2", "We have no budget for next quarter.")
    assert helper_tip(ws, clock, call_id) is None, "Muted tips are not shown automatically"
    call(ws, "PATCH", f"/v1/calls/{call_id}/coach", json={"muted": False})
    clock.now += timedelta(seconds=10)
    chunk(ws, call_id, "s3", "We have no budget at all.")
    assert helper_tip(ws, clock, call_id) is not None


def test_pinned_tip_outlives_its_display_window_but_not_its_raw_speech(workspace, clock):
    ws = workspace
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "s1", "We have no budget for this right now.")
    tip = helper_tip(ws, clock, call_id)
    pinned = call(ws, "PATCH", f"/v1/calls/{call_id}/coach", json={"pinned": True})
    assert pinned["pinned_event_id"] == tip["id"]
    clock.now += timedelta(seconds=200)
    kept = get(ws, f"/v1/calls/{call_id}")["coach"]
    assert kept["id"] == tip["id"] and kept["pinned"] is True
    clock.now += timedelta(seconds=105)
    live = get(ws, f"/v1/calls/{call_id}")
    assert live["coach"] is None and live["transcript"] == []


def test_private_help_answers_the_asker_and_cancel_withdraws_it(workspace, clock):
    ws = workspace
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "s1", "We have no budget for this right now.")
    request_id = str(uuid4())
    reply = private_help(ws, call_id, "How should I handle the budget objection?", request_id)
    assert reply["status"] == "ready" and reply["private"] is True and reply["action"] == "SHOW_CARD"
    assert reply["request_id"] == request_id and reply["say_or_ask"]
    again = private_help(ws, call_id, "How should I handle the budget objection?", request_id)
    assert again["id"] == reply["id"], "A repeated request identity returns the same answer"
    live = get(ws, f"/v1/calls/{call_id}")["coach"]
    assert live["id"] == reply["id"] and live["private"] is True
    cancelled = post(ws, f"/v1/calls/{call_id}/coach/cancel", {"request_id": request_id})
    assert cancelled["status"] == "cancelled"
    assert get(ws, f"/v1/calls/{call_id}")["coach"] is None
    help_now = post(
        ws, f"/v1/calls/{call_id}/coach", {"question": None, "urgent": True, "request_id": str(uuid4())}
    )
    assert help_now["status"] == "ready" and help_now["private"] is False and help_now["say_or_ask"]


def test_feedback_records_display_and_rep_judgement_with_version_checks(workspace, clock):
    ws = workspace
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "s1", "We have no budget for this right now.")
    tip = helper_tip(ws, clock, call_id)
    path = f"/v1/calls/{call_id}/coach/feedback"
    before = get(ws, f"{path}?coach_event_id={tip['id']}")
    assert before["version"] == 0 and before["used_by_rep"] is None
    post(ws, path, {"coach_event_id": tip["id"], "request_id": str(uuid4()), "displayed": True})
    body = {"coach_event_id": tip["id"], "request_id": str(uuid4()), "expected_version": 0}
    judged = post(ws, path, {**body, "useful": True, "grounded": True})
    assert judged["version"] == 1 and judged["useful"] is True and judged["grounded"] is True
    assert judged["displayed_at"], "The display acknowledgement is recorded with the judgement"
    assert post(ws, path, {**body, "useful": True, "grounded": True})["version"] == 1, (
        "Retries are idempotent"
    )
    send_stale = send(
        ws,
        "POST",
        path,
        json={
            "coach_event_id": tip["id"],
            "request_id": str(uuid4()),
            "expected_version": 0,
            "used_by_rep": True,
        },
    )
    assert send_stale.status_code == 409
    used = post(
        ws,
        path,
        {"coach_event_id": tip["id"], "request_id": str(uuid4()), "expected_version": 1, "used_by_rep": True},
    )
    assert used["version"] == 2 and used["used_by_rep"] is True
    after = get(ws, f"{path}?coach_event_id={tip['id']}")
    assert after["version"] == 2 and after["used_by_rep"] is True and after["useful"] is True


def settled_report(ws):
    for _ in range(30):
        report = get(ws, "/v1/optimization")
        if report["projection"]["status"] == "ready":
            return report
        run_jobs(ws)
    raise AssertionError(report["projection"])


def test_reviewed_tips_reach_the_coaching_report_with_their_timing(workspace, clock):
    ws = workspace
    first = start(ws, key="metrics-call-1")["id"]
    answered_by_human(ws, first)
    chunk(ws, first, "s1", "We have no budget for this right now.")
    tip = helper_tip(ws, clock, first)
    path = f"/v1/calls/{first}/coach/feedback"
    shown = {"coach_event_id": tip["id"], "request_id": str(uuid4()), "displayed": True, "render_ms": 40}
    post(ws, path, shown)
    judged = {"useful": True, "grounded": True, "timely": False}
    post(ws, path, {"coach_event_id": tip["id"], "request_id": str(uuid4()), "expected_version": 0, **judged})
    call(ws, "POST", f"/v1/calls/{first}/hangup")
    wrap_up(ws, first, outcome="not_interested", contact_role="right_party")
    second = start(ws, key="metrics-call-2")["id"]
    answered_by_human(ws, second)
    chunk(ws, second, "s1", "We have no budget for next quarter.")
    assert helper_tip(ws, clock, second) is not None, "Generated but never displayed or reviewed"
    coaching = settled_report(ws)["coaching"]
    assert coaching["reviewed_cards"] == 1
    assert coaching["useful_fraction"] == 1 and coaching["grounded_fraction"] == 1
    assert coaching["timely_fraction"] == 0 and coaching["distracting_fraction"] is None
    # The rep's render timing comes from the one displayed tip; server timing from both.
    assert coaching["latency_support"]["render_ms"] == 1 and coaching["p95_render_ms"] == 40
    assert coaching["latency_support"]["inference_ms"] == 2
    assert coaching["latency_support"]["stt_ms"] == 0 and coaching["p95_stt_ms"] is None


def test_raw_speech_expires_at_five_minutes_while_notes_comments_and_outcome_remain(workspace, clock):
    ws = workspace
    call_id = start(ws)["id"]
    lead_id = get(ws, f"/v1/calls/{call_id}")["lead_id"]
    answered_by_human(ws, call_id)
    chunk(ws, call_id, "r1", "Hi, this is Example Seller calling about a brief introduction.", side="rep")
    chunk(ws, call_id, "s1", MARKER)
    assert helper_tip(ws, clock, call_id) is not None
    call(
        ws,
        "PATCH",
        f"/v1/calls/{call_id}/notes",
        json={"expected_edit_version": 0, "text": "Wants a pricing sheet."},
    )
    post(ws, f"/v1/calls/{call_id}/comments", {"text": "Handled the objection well"})
    call(ws, "POST", f"/v1/calls/{call_id}/hangup")
    wrap_up(ws, call_id, outcome="not_interested", contact_role="right_party")
    run_jobs(ws)
    clock.now += timedelta(seconds=299)
    assert MARKER in json.dumps(get(ws, f"/v1/calls/{call_id}"))
    assert MARKER in json.dumps(get(ws, f"/v1/calls/{call_id}/analysis"))
    clock.now += timedelta(seconds=1)
    live = get(ws, f"/v1/calls/{call_id}")
    assert live["coach"] is None and live["transcript"] == [] and live["raw_evidence_status"] == "expired"
    assert MARKER not in json.dumps(live)
    analysis = get(ws, f"/v1/calls/{call_id}/analysis")
    assert MARKER not in json.dumps(analysis) and analysis["transcript"] == []
    assert analysis["human_notes"] == "Wants a pricing sheet." and analysis["status"] == "human_confirmed"
    assert [c["text"] for c in analysis["comments"]] == ["Handled the objection well"]
    assert analysis["call"]["outcome"] == "not_interested"
    for path in ("/v1/events", "/v1/workspace", "/v1/coaching", "/v1/metrics", f"/v1/leads/{lead_id}/packet"):
        assert MARKER not in json.dumps(get(ws, path)), path


def test_helper_route_is_reported_as_simulation_only_on_the_pinned_cerebras_route(workspace):
    ws = workspace
    settings = get(ws, "/v1/admin/settings")
    assert settings["effective"]["coach_provider"] == "openrouter"
    assert settings["configured"]["openrouter_api_key"] is False
    preflight = get(ws, "/v1/admin/preflight")
    helper = next(c for c in preflight["capabilities"] if c["id"] == "live_helper")
    assert helper["state"] == "software_verified" and helper["reason_codes"] == ["SIMULATION_ONLY"]
    assert "Cerebras" in helper["remedy"]
