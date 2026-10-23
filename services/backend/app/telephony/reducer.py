from datetime import datetime

TERMINAL = {"ENDED", "FINALIZED", "BUSY", "NO_ANSWER", "TEMPORARY_FAILURE", "INVALID_ENDPOINT"}
ORDER = {
    v: i
    for i, v in enumerate(
        [
            "AUTHORIZED",
            "RECONCILING",
            # No carrier reference or event before the dial deadline; operator review owns it.
            "DIAL_UNCONFIRMED",
            "DIALING",
            "RINGING",
            "ANSWERED_UNCLASSIFIED",
            "SCREENING",
            "VOICEMAIL_GREETING",
            "VOICEMAIL_READY",
            "VM_PENDING",
            "VM_PLAYING",
            "HUMAN",
            "ENDING",
            "ENDED",
            "FINALIZED",
        ]
    )
}


def owns_effect(data, owner, now):
    """A response cannot publish after its exclusive execution lease expired."""
    try:
        expiry = datetime.fromisoformat(data.get("lease_until") or "")
        return bool(owner and data.get("lease_owner") == owner and expiry > now)
    except (ValueError, TypeError):
        return False


# Documented Telnyx AMD results, normalized as "<event_type>:<payload.result>". Other results
# (not_sure, silence, fax_detected, ended, no_beep_detected) leave the classification unknown.
HUMAN_EVENTS = {
    "call.machine.detection.ended:human",
    "call.machine.premium.detection.ended:human_residence",
    "call.machine.premium.detection.ended:human_business",
}
MAPPING = {
    "call.initiated": "DIALING",
    "call.ringing": "RINGING",
    "call.answered": "ANSWERED_UNCLASSIFIED",
    "call.hangup": "ENDED",
    **dict.fromkeys(HUMAN_EVENTS, "HUMAN"),
    "call.machine.detection.ended:machine": "VOICEMAIL_GREETING",
    "call.machine.premium.detection.ended:machine": "VOICEMAIL_GREETING",
    "call.machine.greeting.ended:beep_detected": "VOICEMAIL_READY",
    "call.machine.premium.greeting.ended:beep_detected": "VOICEMAIL_READY",
    "call.machine.premium.call_screening.detected:screening": "SCREENING",
    "call.playback.started": "VM_PLAYING",
    "call.playback.ended": "ENDING",
    "human.confirmed": "HUMAN",
}


def reduce_state(state, event):
    if state in TERMINAL:
        return state
    target = MAPPING.get(event, state)
    return target if ORDER.get(target, 0) >= ORDER.get(state, 0) else state
