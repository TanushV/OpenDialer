"""Every call state and every event that moves it, in one pure table (section 4 rule 1, R4.1).

Carrier events only move a call forward in `STATES` order, so a late or repeated event never undoes progress.
App events (`dial.*`, `hangup.requested`, `outcome.saved`) name the app's own steps. A terminal call stays terminal,
except that saving an outcome finalizes an ended call.

`TIMEOUTS` gives every non-terminal state a limit and the event the recovery pass applies when it is reached, so no
call can stay in a state forever (G3). `scripts/gen_call_states.py` writes the TypeScript copy of this list.
"""

STATES = (
    "AUTHORIZED",
    "RECONCILING",
    # No carrier reference or event before the dial deadline; recovery or Needs attention settles it.
    "DIAL_UNCONFIRMED",
    "DIALING",
    "RINGING",
    "ANSWERED_UNCLASSIFIED",
    "SCREENING",
    "VOICEMAIL_GREETING",
    "VOICEMAIL_READY",
    "HUMAN",
    "ENDING",
    "ENDED",
    "FINALIZED",
)
ORDER = {state: index for index, state in enumerate(STATES)}
TERMINAL = frozenset({"ENDED", "FINALIZED"})
# The rep is on the line or the carrier leg is still live.
LIVE = frozenset(STATES) - TERMINAL - {"AUTHORIZED", "RECONCILING", "DIAL_UNCONFIRMED"}

# Documented Telnyx AMD results, as "<event_type>:<payload.result>". Other results leave the classification unknown.
HUMAN_EVENTS = (
    "call.machine.detection.ended:human",
    "call.machine.premium.detection.ended:human_residence",
    "call.machine.premium.detection.ended:human_business",
)
CARRIER = {
    "call.initiated": "DIALING",
    "call.ringing": "RINGING",
    "call.answered": "ANSWERED_UNCLASSIFIED",
    **dict.fromkeys(HUMAN_EVENTS, "HUMAN"),
    "human.confirmed": "HUMAN",
    "call.machine.detection.ended:machine": "VOICEMAIL_GREETING",
    "call.machine.premium.detection.ended:machine": "VOICEMAIL_GREETING",
    "call.machine.greeting.ended:beep_detected": "VOICEMAIL_READY",
    "call.machine.premium.greeting.ended:beep_detected": "VOICEMAIL_READY",
    "call.machine.premium.call_screening.detected:screening": "SCREENING",
}
# App events: (states it applies from, target). Anything else leaves the state as it is.
APP = {
    "dial.sent": ({"AUTHORIZED"}, "DIALING"),
    "dial.uncertain": ({"DIALING"}, "RECONCILING"),
    "dial.unconfirmed": ({"AUTHORIZED", "RECONCILING", "DIALING", "ENDING"}, "DIAL_UNCONFIRMED"),
    "hangup.requested": (frozenset(STATES) - TERMINAL - {"ENDING"}, "ENDING"),
    "call.hangup": (frozenset(STATES) - TERMINAL, "ENDED"),
    "call.ended": (frozenset(STATES) - TERMINAL, "ENDED"),
    "outcome.saved": ({"ENDED", "FINALIZED"}, "FINALIZED"),
}
EVENTS = (*CARRIER, *APP)

# (seconds in the state, event applied when the limit passes). The answered states are bounded by the call time
# limit, which the carrier also enforces.
MAX_CALL_SECONDS = 7200
TIMEOUTS = {
    "AUTHORIZED": (30, "dial.unconfirmed"),
    "RECONCILING": (60, "dial.unconfirmed"),
    "DIAL_UNCONFIRMED": (300, "call.ended"),
    "DIALING": (120, "hangup.requested"),
    "RINGING": (120, "hangup.requested"),
    **dict.fromkeys(
        ("ANSWERED_UNCLASSIFIED", "SCREENING", "VOICEMAIL_GREETING", "VOICEMAIL_READY", "HUMAN"),
        (MAX_CALL_SECONDS, "hangup.requested"),
    ),
    "ENDING": (60, "call.ended"),
}


def transition(state, event):
    """The state after `event`. Unknown states, unknown events and events that do not apply change nothing."""
    if event in APP:
        sources, target = APP[event]
        return target if state in sources else state
    if state in TERMINAL or state not in ORDER:
        return state
    target = CARRIER.get(event, state)
    return target if ORDER[target] >= ORDER[state] else state


def moved(call, event, now):
    """The fields to store after `event`: the state, and when it was entered if it changed."""
    after = transition(call["state"], event)
    return {"state": after} if after == call["state"] else {"state": after, "state_at": now.isoformat()}
