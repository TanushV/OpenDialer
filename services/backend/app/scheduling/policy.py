from datetime import datetime

from app.compliance.policy import Decision, authorize
from app.leads.record import BY_HAND, number_hold

STRENGTH_ORDER = {"A": 0, "B": 1, "C": 2}


def parsed(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def queue_decision(context, lead, *, own_claim=None, endpoint_claimed=False, by_hand=False, now=None):
    """Shared read-only eligibility for setup and the queue; never a reservation."""
    reasons = authorize(context).reasons
    if lead.get("status") != "eligible":
        reasons.append("LEAD_NOT_ELIGIBLE")
    hold = number_hold(lead, now)
    if hold and not (by_hand and hold in BY_HAND):
        reasons.append(hold)
    if own_claim:
        reasons.append("LIVE_OBLIGATION" if own_claim.call_id else "RESERVATION_EXISTS")
    if endpoint_claimed:
        reasons.append("ENDPOINT_CLAIMED")
    return Decision(not reasons, reasons)


def rank_candidates(candidates, now):
    def key(c):
        callback, coverage = parsed(c.get("callback_due_at")), parsed(c.get("coverage_due_at"))
        pair = parsed(c.get("pair_until"))
        if (callback and callback <= now) or (pair and now < pair):
            priority = 0
        elif c.get("requested_contact"):
            priority = 1
        elif coverage and coverage <= now:
            priority = 2
        else:
            priority = 3
        # Among equally urgent work, a lead never called before goes first (the owner's starting
        # preference); the statistical estimate orders the rest.
        tried = int(c.get("attempt_count", 0) > 0)
        # A stronger checked trigger breaks ties between equal estimates (R9.9); untrained, that is most ties.
        strength = STRENGTH_ORDER.get(((c.get("trigger") or {}).get("strength")), 3)
        return (
            (priority, tried, coverage or now, -c.get("propensity", 0.1))
            if priority < 3
            else (priority, -round(c.get("propensity", 0.1), 6), strength, tried, coverage or now)
        )

    return sorted(candidates, key=key)
