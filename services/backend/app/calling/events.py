"""What one carrier event means for a call, as small pure steps (R4.2).

Each step reads the call's current data and the event and returns the changes to make. Storing them, sending
commands and starting jobs stays with the caller, so every rule here is testable without a database.
"""

import re

from app.calling.state import HUMAN_EVENTS, TERMINAL

MACHINE_EVENTS = frozenset(
    {"call.machine.detection.ended:machine", "call.machine.premium.detection.ended:machine"}
)
BEEP_EVENTS = frozenset(
    {"call.machine.greeting.ended:beep_detected", "call.machine.premium.greeting.ended:beep_detected"}
)
SCREENING_EVENTS = frozenset(
    {
        "call.machine.premium.call_screening.detected:screening",
        "call.machine.premium.greeting.ended:prompt_ended",
    }
)
ANSWER_EVENTS = frozenset({*HUMAN_EVENTS, "human.confirmed"})
SIP_BLOCK = re.compile(r"(603\+?)(?:[ \t].*)?")
CAUSE_OUTCOMES = {"no_answer": "no_answer", "timeout": "no_answer", "user_busy": "busy"}


def hangup_facts(payload):
    """Who hung up and why, as the carrier reported it."""
    changes = {}
    cause = str(payload.get("hangup_cause") or "")
    if cause:
        changes["provider_hangup_cause"] = cause[:100]
    if payload.get("hangup_source") in {"caller", "callee", "unknown"}:
        changes["provider_hangup_source"] = payload["hangup_source"]
    return changes


def sip_block(call, payload):
    """A SIP 603 or 603+ before any answer is a carrier block on the caller number."""
    match = SIP_BLOCK.fullmatch(str(payload.get("sip_hangup_cause") or ""))
    if match and not call.get("sip_block_code") and not call.get("answered_at"):
        return match[1]
    return None


def carrier_outcome(call, payload):
    """The outcome the carrier already settled at hangup, or None when the rep must choose it."""
    if call.get("human_answered_at") or call.get("sip_block_code"):
        return None
    if call.get("machine_detected"):
        return "voicemail"
    if call.get("answered_at"):
        return None
    return CAUSE_OUTCOMES.get(payload.get("hangup_cause"))


def final_attempt(call, policy):
    return call.get("attempt_ordinal", 0) >= call.get("attempt_allowance", policy.get("max_attempts", 4))


def machine(call, event_type, *, final):
    """(changes, hang up now) for a machine or beep result. The last attempt keeps the line open for a message."""
    if event_type not in MACHINE_EVENTS | BEEP_EVENTS or call.get("human_answered_at"):
        return {}, False
    if final:
        return {"machine_detected": True, "voicemail_turn": True}, False
    hang_up = (
        event_type in BEEP_EVENTS
        and not call.get("stay_on_voicemail")
        and call.get("state") not in TERMINAL | {"ENDING"}
    )
    return {"machine_detected": True}, hang_up


def screening(call, event_type):
    if event_type in SCREENING_EVENTS and not call.get("human_answered_at"):
        return {"screening_detected": True, "machine_detected": False}
    return {}


def human(call, event_type, now):
    """(changes, first answer) when a person is confirmed on the line."""
    if event_type not in ANSWER_EVENTS:
        return {}, False
    first = not call.get("human_answered_at")
    return {"human_answered_at": now.isoformat(), "machine_detected": False, "carrier_outcome": None}, first
