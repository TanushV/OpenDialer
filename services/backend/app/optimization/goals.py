"""One goal reconciliation calculation for previews and committed changes."""

import hashlib
import json

from app.optimization.learning import conversion_at, derive_labels, outcome_at


def review_token(lead, campaign):
    # Background script work changes the row version, but not the relationship
    # being reviewed. All other changes, including continuity revisions, count.
    data = {
        key: value
        for key, value in lead.data.items()
        if key not in {"preparation", "preparation_lease", "preparation_generation"}
    }
    return hashlib.sha256(
        json.dumps(
            [lead.id, data, campaign.data.get("optimization_epoch"), campaign.data.get("optimization", {})],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def goal_state(lead, goal, calls, events, *, now, suppressed=False, changed=False):
    label = "qualified" if goal == "qualified_conversation" else goal
    achieved = any(derive_labels((outcome_at(c, now) or {}).get("outcome"))[label] for c in calls)
    achieved |= any(
        derive_labels(value["kind"])[label] and not value["retracted"]
        for event in events
        if (value := conversion_at(event, now))
    )
    status = lead.get("status", "eligible")
    hold = lead.get("outreach_hold")
    if achieved:
        status = "converted" if status == "eligible" else status
    elif status == "converted":
        status = "needs_review" if suppressed else "eligible"
        if changed and not hold and not lead.get("callback_due_at"):
            hold = "goal_changed"
    return {"status": status, "conversion_goal_achieved": goal if achieved else None, "outreach_hold": hold}


def goal_plan(service, s, p, campaign, goal):
    calls, events = {}, {}
    for kind, grouped in (("call_attempts", calls), ("conversion_events", events)):
        for record in service.scoped(s, kind, p):
            grouped.setdefault(record.data["lead_id"], []).append({"id": record.id, **record.data})
    blocked = {r.data["phone"] for r in service.scoped(s, "suppressions", p) if r.data.get("active", True)}
    before = service.optimization_config(s, p, campaign.id)["conversion_goal"]
    changes = []
    for lead in service.scoped(s, "leads", p):
        if lead.data["campaign_id"] != campaign.id:
            continue
        changes.append(
            (
                lead,
                goal_state(
                    lead.data,
                    goal,
                    calls.get(lead.id, []),
                    events.get(lead.id, []),
                    now=service.clock(),
                    suppressed=lead.data["phone"] in blocked,
                    changed=goal != before,
                )
                if goal != before
                else {
                    key: lead.data.get(key) for key in ("status", "conversion_goal_achieved", "outreach_hold")
                },
            )
        )
    summary = {
        "campaign_version": campaign.version,
        "from_goal": before,
        "to_goal": goal,
        "completed_before": sum(bool(lead.data.get("conversion_goal_achieved")) for lead, _ in changes),
        "completed_after": sum(bool(value["conversion_goal_achieved"]) for _, value in changes),
        "requires_next_action": sum(value["outreach_hold"] == "goal_changed" for _, value in changes),
        "preserved_holds": sum(bool(lead.data.get("outreach_hold")) for lead, _ in changes),
        "preserved_callbacks": sum(bool(lead.data.get("callback_due_at")) for lead, _ in changes),
    }
    return summary, changes
