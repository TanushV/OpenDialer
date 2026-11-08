"""R4.2: each step of handling a carrier event, tested alone."""

from datetime import UTC, datetime

from app.calling import events

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)


def test_hangup_facts_keep_cause_and_known_sources_only():
    assert events.hangup_facts({"hangup_cause": "normal_clearing", "hangup_source": "callee"}) == {
        "provider_hangup_cause": "normal_clearing",
        "provider_hangup_source": "callee",
    }
    assert events.hangup_facts({"hangup_source": "someone"}) == {}
    assert len(events.hangup_facts({"hangup_cause": "x" * 500})["provider_hangup_cause"]) == 100


def test_a_603_before_answer_is_a_carrier_block_once():
    assert events.sip_block({}, {"sip_hangup_cause": "603+ Decline"}) == "603+"
    assert events.sip_block({}, {"sip_hangup_cause": "603"}) == "603"
    assert events.sip_block({"answered_at": "x"}, {"sip_hangup_cause": "603"}) is None
    assert events.sip_block({"sip_block_code": "603"}, {"sip_hangup_cause": "603"}) is None
    assert events.sip_block({}, {"sip_hangup_cause": "486 Busy"}) is None


def test_carrier_outcomes_are_settled_only_when_no_person_answered():
    assert events.carrier_outcome({}, {"hangup_cause": "timeout"}) == "no_answer"
    assert events.carrier_outcome({}, {"hangup_cause": "user_busy"}) == "busy"
    assert events.carrier_outcome({}, {"hangup_cause": "normal_clearing"}) is None
    assert events.carrier_outcome({"answered_at": "x"}, {"hangup_cause": "timeout"}) is None
    assert events.carrier_outcome({"answered_at": "x", "machine_detected": True}, {}) == "voicemail"
    assert events.carrier_outcome({"human_answered_at": "x", "machine_detected": True}, {}) is None
    assert events.carrier_outcome({"sip_block_code": "603"}, {"hangup_cause": "timeout"}) is None


def test_a_beep_hangs_up_unless_final_staying_or_ending():
    beep = "call.machine.premium.greeting.ended:beep_detected"
    machine = "call.machine.premium.detection.ended:machine"
    assert events.machine({"state": "VOICEMAIL_GREETING"}, beep, final=False) == (
        {"machine_detected": True},
        True,
    )
    assert events.machine({"state": "VOICEMAIL_GREETING"}, machine, final=False) == (
        {"machine_detected": True},
        False,
    )
    assert events.machine({}, beep, final=True) == ({"machine_detected": True, "voicemail_turn": True}, False)
    assert events.machine({"stay_on_voicemail": True}, beep, final=False)[1] is False
    assert events.machine({"state": "ENDING"}, beep, final=False)[1] is False
    assert events.machine({"human_answered_at": "x"}, beep, final=False) == ({}, False)
    assert events.machine({}, "call.answered", final=False) == ({}, False)


def test_final_attempt_reads_the_allowance_or_the_policy():
    assert events.final_attempt({"attempt_ordinal": 4}, {"max_attempts": 4})
    assert not events.final_attempt({"attempt_ordinal": 3}, {"max_attempts": 4})
    assert events.final_attempt({"attempt_ordinal": 2, "attempt_allowance": 2}, {"max_attempts": 4})


def test_screening_clears_a_machine_guess():
    event = "call.machine.premium.call_screening.detected:screening"
    assert events.screening({}, event) == {"screening_detected": True, "machine_detected": False}
    assert events.screening({"human_answered_at": "x"}, event) == {}


def test_a_confirmed_person_clears_machine_and_outcome_and_reports_the_first_answer():
    changes, first = events.human({}, "human.confirmed", NOW)
    assert changes == {
        "human_answered_at": NOW.isoformat(),
        "machine_detected": False,
        "carrier_outcome": None,
    }
    assert first is True
    assert events.human({"human_answered_at": "x"}, "call.machine.detection.ended:human", NOW)[1] is False
    assert events.human({}, "call.ringing", NOW) == ({}, False)


def test_a_carrier_outcome_is_saved_when_the_hangup_arrives_without_any_job(workspace):
    """R4.9: no answer, busy and voicemail are carrier facts, saved at once."""
    from conftest import start

    client, headers = workspace["client"], workspace["headers"]
    call = start(workspace, key="r49-no-answer")
    client.post(
        "/v1/simulation/event",
        headers=headers,
        json={
            "event_id": "r49-hangup",
            "event_type": "call.hangup",
            "call_id": call["id"],
            "payload": {"hangup_cause": "timeout", "hangup_source": "callee"},
        },
    )
    saved = client.get(f"/v1/calls/{call['id']}", headers=headers).json()
    assert (saved["state"], saved["outcome"], saved["outcome_source"]) == (
        "FINALIZED",
        "no_answer",
        "carrier",
    )
