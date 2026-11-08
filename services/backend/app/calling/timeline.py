"""One call's timeline, from what is already stored: its own times, carrier events and sent commands (R4.12, G6)."""

from datetime import UTC, datetime

from sqlalchemy import func, select

from app.platform.db import MODELS, doc, scoped_where

MILESTONES = (
    ("started_at", "Call prepared"),
    ("submitted_at", "Dial sent"),
    ("answered_at", "Answered"),
    ("human_answered_at", "Person on the line"),
    ("ended_at", "Call ended"),
    ("outcome_available_at", "Outcome saved"),
)
HANGUP_SOURCES = {"callee": "they hung up", "caller": "you hung up", "unknown": "hang-up source unknown"}


def build(call, events, commands):
    """Entries sorted by time; each has `at`, `kind` (call, carrier or command), `label` and `detail`."""
    entries = [
        {"at": call[key], "kind": "call", "label": label, "detail": ""}
        for key, label in MILESTONES
        if call.get(key)
    ]
    for event in events:
        payload = event.get("payload") or {}
        detail = ""
        if event.get("event_type") == "call.hangup":
            source = HANGUP_SOURCES.get(payload.get("hangup_source"), "")
            detail = ", ".join(part for part in (source, payload.get("hangup_cause") or "") if part)
        entries.append(
            {
                "at": event.get("received_at") or event["created_at"],
                "kind": "carrier",
                "label": event.get("event_type"),
                "detail": detail,
            }
        )
    for command in commands:
        label = " ".join(
            part for part in (command.get("kind"), command.get("action")) if part and part != "action"
        )
        reason = command.get("recovery_reason") or command.get("reason") or command.get("error") or ""
        entries.append(
            {
                "at": command.get("sent_at") or command.get("submitted_at") or command["created_at"],
                "kind": "command",
                "label": label or "command",
                "detail": ", ".join(part for part in (command.get("status"), reason) if part),
            }
        )
    if call.get("recovery_reason"):
        entries.append(
            {
                "at": call.get("ended_at") or call["created_at"],
                "kind": "call",
                "label": "Needs review",
                "detail": call["recovery_reason"],
            }
        )
    for entry in entries:
        entry["at"] = moment(entry["at"]).isoformat()
    return sorted(entries, key=lambda entry: entry["at"])


def moment(value):
    parsed = (
        value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def for_call(s, scope, call):
    """The timeline of a stored call."""

    def related(kind):
        model = MODELS[kind]
        rows = s.scalars(
            select(model).where(
                *scoped_where(model, scope), func.json_extract(model.data, "$.call_id") == call.id
            )
        )
        return [{**doc(row), "created_at": row.created_at} for row in rows]

    return build(
        {**doc(call), "created_at": call.created_at}, related("provider_events"), related("command_outbox")
    )
