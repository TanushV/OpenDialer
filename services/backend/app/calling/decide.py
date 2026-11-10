"""R9E.1: the one answer to "does the next dial happen?", for the queue and for the dial itself.

`decide(situation)` is pure. Its inputs are everything the backend knows that stops a dial: the blockers (setup, keys,
relay, numbers, opt-outs, rep audio, the account's call limit), the rep's own claim (a lead held or a call live), the
reservation behind a dial and the queue's answer. The queue (`scheduling/queue.py`) and the dial (`calling/dial.py`)
ask it and refuse with what it says; nothing else decides. The Dialer's own side (Pause, Start, keys) is the screen
model in `calling/screen.ts`, checked over every input by `tests/screen-model.test.ts`.

Laws, checked over every combination by `tests/test_calling_model.py`: a blocker always stops the dial and is named
first; a live call is never joined by a second dial; a lead already held is never held twice; a dial needs the rep's
own live reservation; every refusal names a way out.
"""

from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException

from app.calling.blockers import EXITS, Blocker

Claim = Literal["none", "reservation", "live_call"]
Queue = Literal["unasked", "lead", "none", "scanning"]


@dataclass(frozen=True)
class Situation:
    blockers: tuple[Blocker, ...] = ()
    claim: Claim = "none"  # The rep's own active claim.
    step: Literal["reserve", "dial"] = "reserve"
    reservation_ok: bool = False  # For a dial: the reservation is the rep's own, unexpired and still claimed.
    queue: Queue = "unasked"  # For a reservation: what the queue found.


@dataclass(frozen=True)
class Decision:
    go: bool
    code: str | None = None
    # How the refusal ends: it clears by itself within this many seconds, or the rep uses the named control.
    clears_within: int | None = None
    control: str | None = None


# Refusals that are not blockers, each with its way out.
OUTS = {
    "LIVE_OBLIGATION": Decision(False, "LIVE_OBLIGATION", control="Dialer: hang up or save the outcome"),
    "RESERVATION_EXISTS": Decision(False, "RESERVATION_EXISTS", clears_within=180),
    "RESERVATION_INVALID": Decision(False, "RESERVATION_INVALID", control="Dialer: Start calling"),
    "NO_ELIGIBLE_LEADS": Decision(False, "NO_ELIGIBLE_LEADS", control="Leads: import or sync a list"),
    "QUEUE_SCAN_CONTINUES": Decision(False, "QUEUE_SCAN_CONTINUES", clears_within=5),
}


def decide(situation):
    if situation.blockers:
        first = situation.blockers[0]
        way = EXITS[first.code]
        return Decision(False, first.code, way.by_itself_within, way.control)
    if situation.claim == "live_call":
        return OUTS["LIVE_OBLIGATION"]
    if situation.step == "dial":
        return Decision(True) if situation.reservation_ok else OUTS["RESERVATION_INVALID"]
    if situation.claim == "reservation":
        return OUTS["RESERVATION_EXISTS"]
    if situation.queue == "none":
        return OUTS["NO_ELIGIBLE_LEADS"]
    if situation.queue == "scanning":
        return OUTS["QUEUE_SCAN_CONTINUES"]
    return Decision(True)


def require(situation, detail=None):
    """Refuse with the decision's reason, or return when the dial (or reservation) may go ahead."""
    decision = decide(situation)
    if decision.go:
        return
    if situation.blockers:
        raise HTTPException(409, situation.blockers[0].detail())
    raise HTTPException(409, detail if detail is not None else decision.code)
