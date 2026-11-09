"""Review opens with what needs the rep, each with the one thing to do (November 9 owner direction).

`issues()` is pure: blockers, leads, calls and follow-ups in; a short list of rows out, most urgent first. Each row
names the problem in plain words, how many it covers, and where to fix it. Nothing that fixes itself is listed.
"""

from datetime import datetime

from app.calling.blockers import SELF_CLEARING
from app.leads.record import HOLDS, number_hold

ENDED = {"finalized", "completed", "ended"}
CLOSED_TASKS = {"completed", "cancelled", "aliased"}
PAGES = {
    "settings": "Open Settings",
    "numbers": "Open Numbers",
    "leads": "Open Leads",
    "dialer": "Open Dialer",
}
# What to do about leads held back from auto-dial, per hold.
HOLD_FIX = {
    "NUMBER_CONFLICT": ("leads", "Check them in Leads; dial by hand if you are sure"),
    "CRM_SYNC_STALE": ("leads", "Sync the HubSpot list in Leads"),
    "DNC_NOT_CHECKED": ("settings", "Add the RealValidito key in Settings"),
    "NEEDS_NUMBER": ("settings", "Add the Treg token in Settings to find numbers"),
}
HOLD_TITLE = {
    "NUMBER_DEAD": (
        "1 lead has a dead number and is skipped",
        "{n} leads have a dead number and are skipped",
    ),
    "NUMBER_CONFLICT": (
        "1 lead has a number that may belong to someone else",
        "{n} leads have a number that may belong to someone else",
    ),
    "NUMBER_BAD": (
        "1 lead has an invalid or wrong number and is skipped",
        "{n} leads have an invalid or wrong number and are skipped",
    ),
    "NO_VALID_NUMBER": ("1 lead has no valid number", "{n} leads have no valid number"),
    "NEEDS_NUMBER": ("1 lead has no number yet", "{n} leads have no number yet"),
    "DNC_NOT_CHECKED": (
        "1 HubSpot lead waits for the do-not-call check",
        "{n} HubSpot leads wait for the do-not-call check",
    ),
    "CRM_SYNC_STALE": (
        "1 HubSpot lead waits because its list has not synced for a day",
        "{n} HubSpot leads wait because their list has not synced for a day",
    ),
}
# Holds the rep can do nothing more about are shown last, as information.
SETTLED = {"NUMBER_DEAD", "NUMBER_BAD", "NO_VALID_NUMBER"}


def _row(code, title, page, action, *, count=None, detail=None, level="fix"):
    return {
        "code": code,
        "title": title,
        "detail": detail,
        "count": count,
        "level": level,
        "fix": {"page": page, "label": action} if page else None,
    }


def _plural(n, one, many):
    return one if n == 1 else many.format(n=n)


def issues(*, blockers, leads, calls, tasks, now):
    rows = []
    for blocker in blockers:
        if blocker["code"] in SELF_CLEARING and not blocker.get("stuck"):
            continue
        rows.append(
            _row(blocker["code"], blocker["message"], blocker["fix"], PAGES.get(blocker["fix"], "Open"))
        )

    unsaved = [c for c in calls if not c.get("outcome") and not c.get("is_test") and _ended(c)]
    if unsaved:
        rows.append(
            _row(
                "CALLS_WITHOUT_OUTCOME",
                _plural(len(unsaved), "1 call has no outcome saved", "{n} calls have no outcome saved"),
                "review",
                "Save their outcomes below",
                count=len(unsaved),
                detail="Learning and follow-ups only use calls with an outcome.",
            )
        )

    overdue = [t for t in tasks if (t.get("status") or "open") not in CLOSED_TASKS and _due(t, now)]
    if overdue:
        rows.append(
            _row(
                "FOLLOWUPS_OVERDUE",
                _plural(len(overdue), "1 follow-up is overdue", "{n} follow-ups are overdue"),
                "review",
                "Call back or reschedule below",
                count=len(overdue),
            )
        )

    held = {}
    for lead in leads:
        if lead.get("archived"):
            continue
        hold = number_hold(lead, now)
        if hold:
            held[hold] = held.get(hold, 0) + 1
    for hold in sorted(held, key=lambda code: (code in SETTLED, list(HOLDS).index(code))):
        n = held[hold]
        page, action = HOLD_FIX.get(hold, (None, None))
        rows.append(
            _row(
                hold,
                _plural(n, *HOLD_TITLE[hold]),
                page,
                action,
                count=n,
                level="info" if hold in SETTLED else "fix",
                detail="Nothing to do: they are not dialed." if hold in SETTLED else None,
            )
        )
    return rows


def _ended(call):
    state = str(call.get("state") or call.get("status") or "").lower()
    return state in ENDED or bool(call.get("ended_at"))


def _due(task, now):
    due = task.get("due_at")
    if not due:
        return False
    try:
        at = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
    except ValueError:
        return False
    return at <= now if at.tzinfo else at <= now.replace(tzinfo=None)
