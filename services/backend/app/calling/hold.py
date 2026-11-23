"""The short-call hold (work package 6.10).

Telnyx counts connected calls of 6 seconds or less as short calls and may charge or block them above 15% of a day's
connected calls. An early End as voicemail frees the rep at once, but the carrier stop waits until 7 seconds after
answer. Escape and any end before answer stay immediate. The held stop is a durable command due at the hold's end, so
the recovery pass (about once a second) sends it, including after a restart, and never earlier.
"""

from datetime import timedelta

from app import ops
from app.calling.records import CommandView
from app.calling.state import TERMINAL, moved
from app.platform.db import emit, put, update, workspace
from app.platform.errors import dt

SHORT_CALL_HOLD = timedelta(seconds=7)
REASON = "short_call"


def until(view, voicemail, now):
    """When the carrier stop of an End as voicemail may be sent, if that is still in the future."""
    if not voicemail or not view.answered_at or not view.provider_id:
        return None
    end = dt(view.answered_at) + SHORT_CALL_HOLD
    return end if now < end else None


def settle(service, s, p, call, view, existing, *, voicemail):
    """True when the hold decides this stop: a held stop the carrier made moot, or one that must still wait."""
    if view.state in TERMINAL:
        return cancel_if_carrier_ended(existing)
    return defer(service, s, p, call, view, existing, voicemail=voicemail)


def defer(service, s, p, call, view, existing, *, voicemail):
    """Record a held stop and move the call to ending; True when the stop must wait."""
    hold_until = until(view, voicemail, service.clock())
    if not hold_until or (existing and CommandView.model_validate(existing.data).status != "pending"):
        return False
    fields = {"next_attempt_at": hold_until.isoformat(), "hold_reason": REASON}
    if existing:
        update(existing, **fields)
    else:
        put(
            s,
            "command_outbox",
            workspace(p),
            p["seller_id"],
            {
                "kind": "hangup",
                "call_id": call.id,
                "rep_id": view.rep_id,
                "provider_id": view.provider_id,
                "status": "pending",
                **fields,
            },
            key=call.id + ":hangup",
            now=service.clock(),
        )
    update(
        call,
        **moved(call.data, "hangup.requested", service.clock()),
        carrier_hold_until=hold_until.isoformat(),
    )
    ops.stop_agent_transcription_in(service, s, p, call)
    service.tips.end(call.id)
    emit(s, workspace(p), "call.updated", call, service.clock(), {"state": call.data["state"]})
    return True


def cancel_if_carrier_ended(existing):
    """The carrier ended the call during the hold: nothing is left to stop. True when a held stop was cancelled."""
    if not existing:
        return False
    command = CommandView.model_validate(existing.data)
    if command.hold_reason != REASON or command.status != "pending":
        return False
    update(existing, status="cancelled", reason="CARRIER_ENDED_DURING_HOLD")
    return True
