import pytest
from journey import event, get, run_jobs, start, wrap_up


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


@pytest.mark.parametrize("outcome", ["not_interested", "meeting_booked"])
def test_reviewed_model_saves_supported_outcome_but_not_a_booking(workspace, app, monkeypatch, outcome):
    from journey import answered_by_human, post
    from test_feature_models import delta, install_fake_models, observation, turn_output

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
            "text": "No thank you, I am not interested.",
        },
    )
    post(workspace, f"/v1/calls/{placed['id']}/hangup")

    def finalizer(request):
        item = observation(request, "signal", "The prospect declined", segment="answer", review_required=True)
        item["outcome"] = {"code": outcome, "contact_role": "unknown"}
        return turn_output(request, state_delta=delta(request, observations=[item]))

    install_fake_models(app, monkeypatch, {"finalize": finalizer})
    run_jobs(workspace, rounds=4)
    result = get(workspace, f"/v1/calls/{placed['id']}")
    if outcome == "meeting_booked":
        assert result["state"] == "ENDED"
        assert not result.get("outcome")
        return
    assert result["state"] == "FINALIZED"
    assert result["outcome"] == "not_interested"
    assert result["outcome_source"] == "ai_reviewed"
    corrected = wrap_up(workspace, placed["id"], outcome="not_interested", contact_role="unknown")
    assert corrected["outcome_source"] == "rep_reported"


def test_answered_but_unclassified_call_is_not_saved_as_no_answer(workspace):
    placed = start(workspace)
    event(workspace, placed["id"], "call.answered")
    event(workspace, placed["id"], "call.hangup", hangup_cause="timeout")
    run_jobs(workspace, rounds=2)
    assert not get(workspace, f"/v1/calls/{placed['id']}").get("outcome")
