"""Fields every lead carries about where it came from and whether its number may be dialed (R3.11, R9.16, R9.22).

CSV leads are taken as they are: no finder, Trestle or do-not-call check runs on them (D19, D23). A mapped live-status
column still applies to both sources, because it is free: a `dead` number is never dialed.
"""

from datetime import timedelta

# Why a lead's number is held from calling, with the words Leads and the Dialer show.
HOLDS = {
    "NUMBER_DEAD": "The list marks this number dead, so it is not dialed.",
    "NUMBER_CONFLICT": "The number check says this number belongs to someone else. Dial it by hand if you are sure.",
    "NUMBER_BAD": "This number is invalid or was marked wrong, so it is not dialed.",
    "NO_VALID_NUMBER": "No valid number was found for this lead.",
    "NEEDS_NUMBER": "This lead has no number yet.",
    "DNC_NOT_CHECKED": "This number needs a do-not-call check before you can dial it.",
    "CRM_SYNC_STALE": "This HubSpot list has not synced for a day, so its leads wait. Sync the list on Leads.",
    "OUTSIDE_US": (
        "This number is outside the 50 US states and DC, where the app's calling hours and recording rules do not"
        " apply. Correct the number or remove the lead."
    ),
}
# Section 5: a HubSpot list not synced for a day stops dialing its leads, so a HubSpot opt-out is not missed for long.
STALE_SYNC = timedelta(hours=24)
# Holds a rep may override by dialing the lead by hand.
BY_HAND = {"NUMBER_CONFLICT"}
STATUS_HOLDS = {
    "conflict": "NUMBER_CONFLICT",
    "bad": "NUMBER_BAD",
    "no_valid_number": "NO_VALID_NUMBER",
    "needs_number": "NEEDS_NUMBER",
}
NUMBER_LABELS = {
    "not_checked": "Not checked (CSV)",
    "confirmed": "Confirmed",
    "unconfirmed": "Unconfirmed",
    "conflict": "Conflict",
    "bad": "Bad",
    "no_valid_number": "No valid number",
    "needs_number": "Needs a number",
    "checking": "Checking",
}


UNVERIFIED = "from the list, unverified"


def trigger_label(trigger):
    """How Leads and the Dialer name a trigger's standing (section 9)."""
    if not trigger or trigger.get("type") in (None, "none"):
        return "No trigger found"
    standing = UNVERIFIED if trigger.get("evidence") == "row_only" else "checked on the page"
    if trigger.get("imported") and trigger.get("evidence") != "row_only":
        # Checked by the run that wrote an imported script (one-generator plan), not again on this computer.
        standing = "checked when the script was written"
    return f"{trigger['strength']} · {standing}"


def csv_fields():
    return {
        "source": "csv",
        "external_id": None,
        "signals": {"aligned": True, "items": []},
        "number_status": "not_checked",
    }


def number_hold(lead, now=None):
    """The code that holds this lead's number from calling, or None when it may be dialed."""
    if now is not None and lead.get("source") == "crm":
        from datetime import datetime

        synced = lead.get("crm_synced_at")
        if not synced or now - datetime.fromisoformat(synced) >= STALE_SYNC:
            return "CRM_SYNC_STALE"
    if lead.get("live_status") == "dead":
        return "NUMBER_DEAD"
    from app.optimization.geography import us_area

    if lead.get("phone") and us_area(lead["phone"]) is None:
        # Call insights plan C2: hours, holidays and recording rules are modeled for the US only.
        return "OUTSIDE_US"
    hold = STATUS_HOLDS.get(lead.get("number_status"))
    if hold:
        return hold
    if lead.get("source") == "crm":
        check = lead.get("dnc") or {}
        if check.get("status") != "checked" or check.get("phone") != lead.get("phone"):
            return "DNC_NOT_CHECKED"
    return None


def number_view(lead, now=None):
    """What Leads and the Dialer show about the number and the trigger: labels and, when held, why."""
    hold = number_hold(lead, now)
    return {
        "trigger_label": trigger_label(lead.get("trigger")),
        "number_status": lead.get("number_status") or "not_checked",
        "number_label": NUMBER_LABELS.get(lead.get("number_status") or "not_checked", "Not checked"),
        "number_hold": hold,
        "number_hold_reason": HOLDS.get(hold),
        "dial_by_hand": hold in BY_HAND,
    }
