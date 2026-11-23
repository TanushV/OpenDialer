"""Today's carrier traffic shape, as Telnyx measures it (work package 6.17). Information only: never a gate.

Telnyx counts connected calls of 6 seconds or less ("short calls") over connected calls, per UTC day, and charges or
may block above 15%. It also charges $0.005 per abandoned call once more than 20% of outbound calls end on our side
before answer (including calls to disconnected numbers). Both are read from this workspace's own call records.
"""

from datetime import UTC, datetime

SHORT_SECONDS = 6
SHORT_LIMIT = 0.15
ABANDONED_LIMIT = 0.20


def _at(value):
    return datetime.fromisoformat(value) if value else None


def carrier_rates(calls, now):
    """Counts and shares for the current UTC day from call records (dicts)."""
    day = now.astimezone(UTC).date()
    placed = [c for c in calls if c.get("provider_id") and (_at(c.get("started_at")) or now).date() == day]
    connected = [c for c in placed if c.get("answered_at")]
    short = [c for c in connected if (c.get("connected_seconds") or 0) <= SHORT_SECONDS and c.get("ended_at")]
    abandoned = [
        c
        for c in placed
        if not c.get("answered_at") and c.get("ended_at") and c.get("provider_hangup_source") == "caller"
    ]

    def share(part, whole):
        return round(len(part) / len(whole), 3) if whole else 0.0

    return {
        "day": day.isoformat(),
        "connected": len(connected),
        "short": len(short),
        "short_share": share(short, connected),
        "short_limit": SHORT_LIMIT,
        "outbound": len(placed),
        "abandoned": len(abandoned),
        "abandoned_share": share(abandoned, placed),
        "abandoned_limit": ABANDONED_LIMIT,
    }
