TERMINAL = {"ENDED", "FINALIZED", "BUSY", "NO_ANSWER", "TEMPORARY_FAILURE", "INVALID_ENDPOINT"}
ORDER = {
    v: i
    for i, v in enumerate(
        [
            "QUEUED",
            "RESERVED",
            "AUTHORIZED",
            "RECONCILING",
            "DIALING",
            "RINGING",
            "ANSWERED_UNCLASSIFIED",
            "SCREENING",
            "IVR",
            "GATEKEEPER",
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


def reduce_state(state, event):
    if state in TERMINAL:
        return state
    mapping = {
        "call.initiated": "DIALING",
        "call.ringing": "RINGING",
        "call.answered": "ANSWERED_UNCLASSIFIED",
        "call.hangup": "ENDED",
        "call.machine.detection.ended:human": "HUMAN",
        "call.machine.detection.ended:machine": "VOICEMAIL_GREETING",
        "call.machine.detection.ended:not_sure": "ANSWERED_UNCLASSIFIED",
        "call.machine.greeting.ended:beep_detected": "VOICEMAIL_READY",
        "call.machine.premium.detection.ended:human_residence": "HUMAN",
        "call.machine.premium.detection.ended:human_business": "HUMAN",
        "call.machine.premium.detection.ended:machine": "VOICEMAIL_GREETING",
        "call.machine.premium.detection.ended:screening": "SCREENING",
        "call.playback.started": "VM_PLAYING",
        "call.playback.ended": "ENDING",
        "call.playback.failed": "ENDING",
        "human.confirmed": "HUMAN",
    }
    target = mapping.get(event, state)
    return target if ORDER.get(target, 0) >= ORDER.get(state, 0) else state
