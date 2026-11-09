from datetime import timedelta

import pytest
from journey import event, get, run_jobs, start, wrap_up


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


def test_voicemail_auto_save_keeps_playback_receipt_boundary(workspace):
    placed = start(workspace)
    event(workspace, placed["id"], "call.answered")
    event(workspace, placed["id"], "call.machine.detection.ended:machine")
    event(workspace, placed["id"], "call.hangup")
    run_jobs(workspace, rounds=2)
    result = get(workspace, f"/v1/calls/{placed['id']}")
    assert result["state"] == "FINALIZED"
    assert result["outcome"] == "voicemail"
    assert not result.get("voicemail_result")  # Reaching a mailbox is not a delivered message.


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


def test_confirmed_mailbox_is_saved_after_rep_hangup_receipt(workspace):
    from journey import post

    placed = start(workspace)
    event(workspace, placed["id"], "call.answered")
    event(workspace, placed["id"], "call.machine.detection.ended:machine")
    post(workspace, f"/v1/calls/{placed['id']}/hangup")
    run_jobs(workspace, rounds=2)
    result = get(workspace, f"/v1/calls/{placed['id']}")
    assert result["state"] == "FINALIZED"
    assert result["outcome"] == "voicemail"
    assert result["outcome_source"] == "carrier"


def test_mailbox_auto_hangup_saves_outcome_without_rep_hangup(workspace, clock):
    from journey import post

    placed = start(workspace)
    event(workspace, placed["id"], "call.answered")
    event(workspace, placed["id"], "call.machine.greeting.ended:beep_detected")
    clock.now += timedelta(seconds=8)  # A skip waits until 7 seconds after the answer (D6).
    post(workspace, "/v1/admin/control/run")
    run_jobs(workspace, rounds=2)
    result = get(workspace, f"/v1/calls/{placed['id']}")
    assert result["state"] == "FINALIZED"
    assert result["outcome"] == "voicemail"
    assert result["outcome_source"] == "carrier"
