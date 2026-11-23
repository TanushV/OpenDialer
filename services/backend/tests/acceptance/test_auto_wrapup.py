from datetime import timedelta

import pytest
from journey import event, get, post, run_jobs, start, wrap_up


@pytest.mark.slow
def test_carrier_outcome_is_saved_without_rep_form_and_can_be_corrected(workspace):
    placed = start(workspace)
    event(workspace, placed["id"], "call.hangup", hangup_cause="no_answer")
    run_jobs(workspace, rounds=2)
    result = get(workspace, f"/v1/calls/{placed['id']}")
    assert result["state"] == "FINALIZED"
    assert result["outcome"] == "no_answer"
    assert result["outcome_source"] == "carrier"
    corrected = wrap_up(workspace, placed["id"], outcome="busy")
    assert corrected["outcome"] == "busy"
    assert corrected["outcome_source"] == "rep_reported"
    run_jobs(workspace, rounds=2)
    assert get(workspace, f"/v1/calls/{placed['id']}")["outcome"] == "busy"


def test_carrier_mailbox_detection_does_not_save_an_outcome(workspace):
    placed = start(workspace)
    event(workspace, placed["id"], "call.answered")
    event(workspace, placed["id"], "call.machine.detection.ended:machine")
    event(workspace, placed["id"], "call.hangup")
    run_jobs(workspace, rounds=2)
    result = get(workspace, f"/v1/calls/{placed['id']}")
    assert result["state"] == "ENDED"
    assert not result.get("outcome")


def test_unknown_hangup_does_not_invent_an_outcome(workspace):
    placed = start(workspace)
    event(workspace, placed["id"], "call.hangup")
    run_jobs(workspace, rounds=2)
    result = get(workspace, f"/v1/calls/{placed['id']}")
    assert result["state"] == "ENDED"
    assert not result.get("outcome")


def test_confirming_same_automatic_outcome_records_human_provenance(workspace):
    placed = start(workspace)
    event(workspace, placed["id"], "call.hangup", hangup_cause="user_busy")
    run_jobs(workspace, rounds=2)
    saved = wrap_up(workspace, placed["id"], outcome="busy", contact_role="unknown")
    assert saved["outcome_source"] == "rep_reported"


def test_the_summary_suggests_a_persons_answer_but_never_saves_it(workspace):
    """Section 6: only carrier facts save themselves. What a person said is a suggestion the rep confirms with Enter."""
    from journey import answered_by_human, call, post

    call(workspace, "PUT", "/v1/admin/settings", json={"openrouter_api_key": "test-openrouter"})
    placed = start(workspace)
    answered_by_human(workspace, placed["id"])
    post(
        workspace,
        f"/v1/calls/{placed['id']}/transcript",
        {
            "segment_id": "answer",
            "revision": 1,
            "side": "remote",
            "is_final": True,
            "text": "I am not interested.",
        },
    )
    post(workspace, f"/v1/calls/{placed['id']}/hangup")
    run_jobs(workspace, rounds=4)
    result = get(workspace, f"/v1/calls/{placed['id']}")
    assert result["state"] == "ENDED" and not result.get("outcome")
    assert result["suggested_outcome"]["code"] == "not_interested"
    saved = wrap_up(workspace, placed["id"], outcome="not_interested", contact_role="right_party")
    assert saved["outcome_source"] == "rep_reported"


@pytest.mark.slow
def test_answered_but_unclassified_call_is_not_saved_as_no_answer(workspace):
    placed = start(workspace)
    event(workspace, placed["id"], "call.answered")
    event(workspace, placed["id"], "call.hangup", hangup_cause="timeout")
    run_jobs(workspace, rounds=2)
    assert not get(workspace, f"/v1/calls/{placed['id']}").get("outcome")


def test_end_as_voicemail_ends_the_call_and_the_rep_says_whether_a_message_was_left(
    workspace, carrier, clock
):
    """Work package 6.12: no detection and no preselected choice; the rep records left or none."""
    placed = start(workspace)
    event(workspace, placed["id"], "call.answered")
    clock.now += timedelta(seconds=10)
    path = f"/v1/calls/{placed['id']}/hangup"
    result = post(workspace, path, {"mark_voicemail": True})
    post(workspace, path, {"mark_voicemail": True})
    assert result["state"] in {"ENDING", "ENDED"} and not result.get("outcome")
    event(workspace, placed["id"], "call.hangup")
    saved = wrap_up(workspace, placed["id"], outcome="voicemail", voicemail_message="none")
    assert saved["outcome"] == "voicemail" and saved["outcome_source"] == "rep_reported"
    assert not saved.get("voicemail_result")  # Logging voicemail does not claim a delivered message.
    assert [h["provider_id"] for h in carrier.hangups] == [placed["provider_id"]]


def test_a_late_mailbox_beep_receipt_leaves_the_call_open_for_the_rep(workspace, clock):
    placed = start(workspace)
    event(workspace, placed["id"], "call.answered")
    event(workspace, placed["id"], "call.machine.greeting.ended:beep_detected")
    clock.now += timedelta(seconds=15)
    post(workspace, "/v1/admin/control/run")
    run_jobs(workspace, rounds=2)
    result = get(workspace, f"/v1/calls/{placed['id']}")
    assert result["state"] == "ANSWERED_UNCLASSIFIED"
    assert not result.get("outcome")


def test_mark_voicemail_cannot_hang_up_a_call_that_is_still_dialing(workspace, carrier):
    placed = start(workspace)
    post(workspace, f"/v1/calls/{placed['id']}/hangup", {"mark_voicemail": True}, status=409)
    assert not carrier.hangups


def test_voicemail_choice_survives_a_lost_hangup_receipt(workspace, carrier, clock, monkeypatch):
    placed = start(workspace)
    event(workspace, placed["id"], "call.answered")
    send = carrier.hangup

    async def receipt_lost(*args):
        await send(*args)
        raise TimeoutError("Simulated lost hangup response")

    monkeypatch.setattr(carrier, "hangup", receipt_lost)
    path = f"/v1/calls/{placed['id']}"
    ending = post(workspace, path + "/hangup", {"mark_voicemail": True})
    assert ending["state"] == "ENDING" and not ending.get("outcome")
    # Ended inside the short-call window: the held stop is sent by recovery, its receipt is lost, and a later
    # recovery pass reconciles it without sending a second stop.
    for _ in range(2):
        clock.now += timedelta(seconds=31)
        post(workspace, "/v1/admin/recovery")
        run_jobs(workspace, rounds=2)
    recovered = get(workspace, path)
    assert recovered["state"] == "ENDED" and not recovered.get("outcome")
    assert recovered["rep_voicemail_at"], "The rep's choice survives the lost receipt"
    assert len(carrier.hangups) == 1
