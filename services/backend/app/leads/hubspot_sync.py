"""What one HubSpot list sync changes (D17, D25, section 9B). Pure: the caller reads HubSpot and writes the store.

HubSpot picks who is called and the app picks the order. A seat syncs only the members its "Calls for" user owns, so
two seats never share a contact. A sync can add a suppression and never remove one. A member that left the list stops
being dialed and keeps its history. A contact another dialer reached inside the spacing window waits (the
last-contacted guard, checked in L18).
"""

from datetime import datetime, timedelta

PROPERTIES = (
    "firstname",
    "lastname",
    "company",
    "phone",
    "mobilephone",
    "jobtitle",
    "website",
    "state",
    "city",
    "hs_linkedin_url",
    "notes_last_contacted",
    "opendialer_opt_out",
    "opendialer_opt_out_reason",
    "phone_status",
)
SPACING = timedelta(days=3)


def _stamp(value):
    if not value:
        return None
    try:
        if str(value).isdigit():
            return datetime.fromtimestamp(int(value) / 1000).astimezone()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def row(contact):
    """The intake row for one contact: identity first, then every other property under its own name."""
    props = {k: v for k, v in (contact.get("properties") or {}).items() if v not in (None, "")}
    first, last = props.get("firstname", ""), props.get("lastname", "")
    website = str(props.get("website", "")).removeprefix("https://").removeprefix("http://").split("/")[0]
    out = {
        "name": " ".join(filter(None, [first, last])) or "Unknown",
        "first name": first,
        "last name": last,
        "company": props.get("company", ""),
        "phone": props.get("phone") or props.get("mobilephone") or "",
        "domain": website.removeprefix("www."),
        "state": props.get("state", ""),
        "city": props.get("city", ""),
        "title": props.get("jobtitle", ""),
        "linkedin url": props.get("hs_linkedin_url", ""),
    }
    skip = set(PROPERTIES) | {"hs_object_id", "createdate", "lastmodifieddate"}
    out |= {k: str(v) for k, v in props.items() if k not in skip}
    return {k: v for k, v in out.items() if v}


def plan(contacts, members, existing, *, now, last_called=None):
    """Changes for one sync: {"add", "update", "drop", "opt_outs", "recent"}.

    `existing` maps a contact id to that lead's stored row. `last_called` maps a contact id to when this app last
    called it, so the guard ignores our own calls.
    """
    last_called = last_called or {}
    members = set(members)
    out = {"add": [], "update": [], "drop": [], "opt_outs": [], "recent": {}}
    for contact in contacts:
        cid = str(contact["id"])
        if cid not in members:
            continue
        props = contact.get("properties") or {}
        data = row(contact)
        if str(props.get("opendialer_opt_out") or "").lower() == "true":
            out["opt_outs"].append(
                {
                    "contact_id": cid,
                    "phone": data.get("phone", ""),
                    "reason": props.get("opendialer_opt_out_reason") or "opt_out",
                }
            )
        reached = _stamp(props.get("notes_last_contacted"))
        ours = last_called.get(cid)
        if reached and now - reached < SPACING and (ours is None or reached > ours + timedelta(minutes=5)):
            out["recent"][cid] = (reached + SPACING).isoformat()
        if cid not in existing:
            out["add"].append((cid, data))
        elif existing[cid] != data:
            out["update"].append((cid, data))
    out["drop"] = [cid for cid in existing if cid not in members]
    return out
