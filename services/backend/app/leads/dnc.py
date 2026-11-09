"""The do-not-call decision for CRM leads (D23, section 9A). Pure: the caller stores the result and the suppression.

A result is good for 30 days (the federal rule wants a fresh scrub within 31). CSV leads are never checked. A CRM lead
whose check is missing, stale or failed is held back from auto-dial by `number_hold` until it passes.
"""

from datetime import timedelta

FRESH = timedelta(days=30)


def due(lead, now):
    """Whether a CRM lead needs a (re)check before it may be auto-dialed."""
    if lead.get("source") != "crm" or not lead.get("phone"):
        return False
    state = lead.get("dnc") or {}
    if state.get("status") != "checked" or state.get("phone") != lead["phone"]:
        return True
    from datetime import datetime

    return now - datetime.fromisoformat(state["checked_at"]) >= FRESH - timedelta(days=1)


def outcome(lead, answer, now):
    """Lead changes and the suppression (or None) for one number's answer."""
    base = {
        "status": "checked",
        "phone": lead["phone"],
        "checked_at": now.isoformat(),
        "provider": "realvalidito",
    }
    if answer["invalid"]:
        return {"dnc": {**base, "lists": []}, "number_status": "bad"}, None
    changes = {"dnc": {**base, "lists": answer["lists"]}}
    if answer["lists"]:
        return changes, {"phone": lead["phone"], "reason": "do_not_call", "lists": answer["lists"]}
    return changes, None


def failed(lead, reason, now):
    """A failed or impossible check leaves the lead "DNC not checked" with the reason; it never clears it."""
    previous = lead.get("dnc") or {}
    return {"dnc": {**previous, "status": "not_checked", "reason": reason, "attempted_at": now.isoformat()}}


LIST_NAMES = {"federal": "the federal do-not-call list", "tcpa_litigator": "a known TCPA litigator list"}


def describe(lists):
    """Plain words for the lists a number is on, for the lead and HubSpot."""
    names = [LIST_NAMES.get(item) or f"the {item.split(':', 1)[1]} state do-not-call list" for item in lists]
    return ", ".join(names)
