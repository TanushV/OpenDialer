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
    if hold and not ((context.own_test_line and hold == "DNC_NOT_CHECKED") or (by_hand and hold in BY_HAND)):
        reasons.append(hold)
    if own_claim:
        reasons.append("LIVE_OBLIGATION" if own_claim.call_id else "RESERVATION_EXISTS")
    if endpoint_claimed:
        reasons.append("ENDPOINT_CLAIMED")
    return Decision(not reasons, reasons)


def rank_candidates(candidates, now):
    """D31 (StatML archived): urgent work first (a due callback, a requested contact, a lapsed coverage promise),
    then leads never called, then the one waiting longest since its last try. A stronger checked trigger breaks
    ties among first calls (R9.9). No estimate takes part."""
    never = datetime.min.replace(tzinfo=now.tzinfo)

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
        tried = int((c.get("attempt_count") or 0) > 0)
        last = parsed(c.get("last_attempt_at")) or never
        strength = STRENGTH_ORDER.get(((c.get("trigger") or {}).get("strength")), 3)
        return (priority, tried, last, strength, coverage or now, str(c.get("id", "")))

    return sorted(candidates, key=key)
