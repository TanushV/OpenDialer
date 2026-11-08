"""What the recovery pass does with each unfinished call (R4.6, R4.8, G3).

Every non-terminal state has a time limit (`state.TIMEOUTS`). While the carrier leg is live and quiet, the pass reads
its status every `POLL_SECONDS` as the fallback for a missed event. When a limit passes, the state's exit event applies.
These functions only decide; the pass in the telephony module carries the decision out.
"""

from datetime import UTC, datetime, timedelta

from app.calling.records import CallView
from app.calling.state import LIVE, MAX_CALL_SECONDS, TERMINAL, TIMEOUTS

POLL_SECONDS = 30
RECHECK_SECONDS = 30
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
ANSWERED = frozenset({"ANSWERED_UNCLASSIFIED", "SCREENING", "VOICEMAIL_GREETING", "VOICEMAIL_READY", "HUMAN"})


def _time(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def deadline(call):
    """When the call's current state runs out. Answered calls are bounded by the call time limit from its start."""
    seconds, _ = TIMEOUTS[call.state]
    if call.state in ANSWERED:
        start = _time(call.started_at) or _time(call.state_at)
        seconds = call.max_call_seconds or MAX_CALL_SECONDS
    else:
        start = _time(call.state_at) or _time(call.submitted_at) or _time(call.started_at)
    return start + timedelta(seconds=seconds) if start else EPOCH


def pollable(call):
    return bool(call.provider_id) and (call.state in LIVE or call.state == "ENDING")


def due_at(data, last_check=None):
    """When the pass should next look at this call; None once it is terminal."""
    call = CallView.model_validate(data)
    if call.state in TERMINAL or call.state not in TIMEOUTS:
        return None
    due = deadline(call)
    if pollable(call):
        due = min(due, (last_check or _time(call.state_at) or EPOCH) + timedelta(seconds=POLL_SECONDS))
    return max(due, last_check + timedelta(seconds=RECHECK_SECONDS)) if last_check else due


def next_step(data, now):
    """ "poll", the exit event of an overrun state, or None."""
    call = CallView.model_validate(data)
    if call.state in TERMINAL or call.state not in TIMEOUTS:
        return None
    if now >= deadline(call):
        return TIMEOUTS[call.state][1]
    return "poll" if pollable(call) else None
