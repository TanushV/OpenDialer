"""Native HubSpot call records (D17, section 9B): one outcome map and one `log_call`.

The record is written when the outcome is saved; the AI summary is added later by an update, so an AI failure never
loses the call. Before retrying an uncertain write the app searches for its own `opendialer_call_id`, because HubSpot
documents no duplicate guard. The transcript is never sent.
"""

from dataclasses import dataclass

# HubSpot's default call outcome labels; the dispositions API supplies their ids at setup.
NO_ANSWER, BUSY, LEFT_VOICEMAIL, WRONG_NUMBER, CONNECTED = (
    "No answer",
    "Busy",
    "Left voicemail",
    "Wrong number",
    "Connected",
)
OUTCOMES = {
    "no_answer": NO_ANSWER,
    "voicemail_skipped": NO_ANSWER,
    "busy": BUSY,
    "voicemail": LEFT_VOICEMAIL,
    "wrong_party": WRONG_NUMBER,
    "invalid_endpoint": WRONG_NUMBER,
}
# A person answered: Connected, unless the account has a custom outcome with the same name (for example Meeting Booked).
HUMAN = {
    "meeting_booked": "Meeting booked",
    "callback_requested": "Callback requested",
    "not_interested": "Not interested",
    "gatekeeper": "Gatekeeper",
    "opt_out": "Do not call",
}


@dataclass(frozen=True)
class CallRecord:
    call_id: str
    contact_id: str
    outcome: str
    started_at: str
    duration_ms: int
    from_number: str
    to_number: str
    note: str = ""
    owner_id: str | None = None


def outcome_label(outcome):
    return OUTCOMES.get(outcome) or HUMAN.get(outcome) or CONNECTED


def map_outcomes(outcomes, dispositions):
    """{outcome: disposition id} for every app outcome, and the outcomes with no matching disposition (shown at setup)."""
    by_label = {d["label"].casefold(): d["id"] for d in dispositions}
    mapped, missing = {}, []
    for outcome in outcomes:
        label = outcome_label(outcome)
        found = by_label.get(label.casefold()) or (
            by_label.get(CONNECTED.casefold()) if outcome in HUMAN or outcome not in OUTCOMES else None
        )
        if found:
            mapped[outcome] = found
        else:
            missing.append(outcome)
    return mapped, missing


def properties(record, disposition_id):
    return {
        "hs_timestamp": record.started_at,
        "hs_call_direction": "OUTBOUND",
        "hs_call_status": "COMPLETED",
        "hs_call_disposition": disposition_id,
        "hs_call_duration": str(max(0, record.duration_ms)),
        "hs_call_from_number": record.from_number,
        "hs_call_to_number": record.to_number,
        "hs_call_title": "OpenDialer call",
        "hs_call_body": record.note[:5000],
        "opendialer_call_id": record.call_id,
        **({"hubspot_owner_id": record.owner_id} if record.owner_id else {}),
    }


async def log_call(hubspot, record, dispositions_map, *, uncertain=False):
    """Write one call record; a retry after an uncertain write finds the first one instead. Returns its HubSpot id."""
    if uncertain:
        existing = await hubspot.find_call(record.call_id)
        if existing:
            return existing
    return await hubspot.create_call(properties(record, dispositions_map[record.outcome]), record.contact_id)


async def add_summary(hubspot, hubspot_call_id, summary):
    """The AI summary arrives later and is added to the record; the transcript never is."""
    await hubspot.update_call(hubspot_call_id, {"hs_call_body": summary[:5000]})
