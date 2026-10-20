from datetime import datetime

from app.compliance.policy import Decision, authorize


def parsed(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def queue_decision(context, lead, *, own_claim=None, endpoint_claimed=False):
    """Shared read-only eligibility for setup and the queue; never a reservation."""
    reasons = authorize(context).reasons
    if lead.get("status") != "eligible":
        reasons.append("LEAD_NOT_ELIGIBLE")
    if own_claim:
        reasons.append("LIVE_OBLIGATION" if own_claim.call_id else "RESERVATION_EXISTS")
    if endpoint_claimed:
        reasons.append("ENDPOINT_CLAIMED")
    return Decision(not reasons, reasons)


def rank_candidates(candidates, now):
    def key(c):
        callback, coverage = parsed(c.get("callback_due_at")), parsed(c.get("coverage_due_at"))
        if callback and callback <= now:
            priority = 0
        elif c.get("requested_contact"):
            priority = 1
        elif coverage and coverage <= now:
            priority = 2
        else:
            priority = 3
        return (
            (priority, coverage or now, -c.get("propensity", 0.1))
            if priority < 3
            else (priority, -c.get("propensity", 0.1), coverage or now)
        )

    return sorted(candidates, key=key)
