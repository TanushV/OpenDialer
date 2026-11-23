from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Outreach calls Monday to Friday, 9:00 to 17:00 in the prospect's own time zone, never on a US federal holiday
# (work package 9.3 C2, owner decisions D4 and D6). Settings may narrow the window, never widen it. A due requested
# callback is exempt. Strictest state rules found: Texas from 9 a.m.; Pennsylvania 9 a.m. to 7 p.m. with no Sundays
# or holidays (Act 47 of 2026); Alabama, Louisiana, Mississippi and possibly Florida until 8 p.m.
OUTREACH_START_FLOOR = 9
OUTREACH_END_CAP = 17
# Every zone a US number can be in. With no usable data, a lead is called only when all of them are open.
ALL_US_ZONES = (
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "America/Adak",
    "Pacific/Honolulu",
)


def _observed(day):
    return (
        day - timedelta(days=1)
        if day.weekday() == 5
        else day + timedelta(days=1)
        if day.weekday() == 6
        else day
    )


def _nth(year, month, weekday, n):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last(year, month, weekday):
    end = date(year, month + 1, 1) - timedelta(days=1)
    return end - timedelta(days=(end.weekday() - weekday) % 7)


@lru_cache(maxsize=16)
def us_federal_holidays(year):
    """The eleven US federal holidays, on the days they are observed."""
    return frozenset(
        {
            _observed(date(year, 1, 1)),
            _nth(year, 1, 0, 3),  # Martin Luther King Jr. Day
            _nth(year, 2, 0, 3),  # Washington's Birthday
            _last(year, 5, 0),  # Memorial Day
            _observed(date(year, 6, 19)),  # Juneteenth
            _observed(date(year, 7, 4)),
            _nth(year, 9, 0, 1),  # Labor Day
            _nth(year, 10, 0, 2),  # Columbus Day
            _observed(date(year, 11, 11)),  # Veterans Day
            _nth(year, 11, 3, 4),  # Thanksgiving
            _observed(date(year, 12, 25)),
        }
    )


def outreach_hours(policy):
    """The window a saved policy really calls in: older workspaces may hold 8:00 or a later end."""
    start, end = (
        policy.get("local_start_hour", OUTREACH_START_FLOOR),
        policy.get("local_end_hour", OUTREACH_END_CAP),
    )
    return max(OUTREACH_START_FLOOR, start), min(OUTREACH_END_CAP, end)


def outreach_open(local, start_hour, end_hour):
    """Whether a local time is inside the outreach window."""
    start, end = outreach_hours({"local_start_hour": start_hour, "local_end_hour": end_hour})
    return (
        local.weekday() < 5
        and local.date() not in us_federal_holidays(local.year)
        and start <= local.hour < end
    )


# Ordinary retries of an unanswered lead wait at least a day; requested callbacks and the
# final-stage second attempt are separate actions with their own timing.
ORDINARY_RETRY_SECONDS = 86400


def retry_seconds(policy):
    """A campaign may wait longer than a day between ordinary attempts, never less."""
    return max(ORDINARY_RETRY_SECONDS, int(policy.get("min_retry_seconds") or 0))


@dataclass
class CallContext:
    now: datetime
    continuing_call: bool = False
    authorization_reconciled: bool = True
    outreach_hold: bool = False
    script_ready: bool = True
    use_master: bool = False
    callback_owner_matches: bool = True
    endpoint_valid: bool = False
    ownership: str = "unknown"
    timezone: str | None = None
    timezone_confidence: str = "unknown"
    # Plausible zones from the supplied state and the number's area code when no zone was given.
    # The calling window must hold in every one of them.
    timezones: tuple = ()
    # National do-not-call screening is optional and off unless the campaign turns it on.
    registry_required: bool = False
    registry_decision: dict | None = None
    state_registry_required: bool = False
    state_registry_current: bool = False
    state_registry_listed: bool = False
    internal_suppressed: bool = True
    consent_required: bool = False
    consent_valid: bool = False
    consent_revoked: bool = False
    rep_ready: bool = False
    audio_ready: bool = False
    number_healthy: bool = False
    number_authorized: bool = False
    attempt_count: int = 0
    max_attempts: int = 4
    next_attempt_at: datetime | None = None
    last_attempt_at: datetime | None = None
    min_retry_seconds: int = ORDINARY_RETRY_SECONDS
    # A confirmed requested callback that is due, or the offered final-stage second attempt.
    callback_due: bool = False
    pair_open: bool = False
    budget_available: bool = False
    route_allowed: bool = False
    local_start_hour: int = 8
    local_end_hour: int = 17
    permission: str = "live"
    permitted_permissions: set[str] | None = None
    # The rep's own test phone during a test call: no prospect is called, so calling hours, time zones,
    # attempt limits, retry spacing and do-not-call registries do not apply. An opt-out still does.
    own_test_line: bool = False


@dataclass
class Decision:
    allowed: bool
    reasons: list[str]


def calling_zones(c: CallContext):
    """The supplied zone, else the zones from the state or area code, else every US zone (never "unknown")."""
    if c.timezone and c.timezone_confidence in {"confirmed", "high"}:
        return (c.timezone,)
    return tuple(c.timezones or ()) or ALL_US_ZONES


def authorize(c: CallContext) -> Decision:
    reasons = []

    def require(condition, reason):
        if not condition:
            reasons.append(reason)

    require(c.authorization_reconciled, "RESTORE_RECONCILIATION_REQUIRED")
    require(not c.outreach_hold, "OUTREACH_HOLD")
    require(c.script_ready, "MASTER_SCRIPT_REQUIRED" if c.use_master else "SCRIPT_NOT_READY")
    require(c.callback_owner_matches, "CALLBACK_ASSIGNED_TO_ANOTHER_REP")
    require(c.endpoint_valid, "INVALID_ENDPOINT")
    require(c.ownership != "wrong_party", "WRONG_PARTY")
    require(not c.internal_suppressed, "INTERNAL_SUPPRESSION")
    if not c.own_test_line:
        if c.registry_required:
            # Only a clearance computed for this exact endpoint permits a dial; an account-level
            # "available" or endpoint-free decision is never a clearance.
            national = c.registry_decision or {}
            if national.get("decision") != "clear":
                codes = national.get("reason_codes") or ["REGISTRY_NOT_CLEARED_FOR_ENDPOINT"]
                reasons.extend(r for r in codes if r not in reasons)
            elif not national.get("endpoint"):
                reasons.append("REGISTRY_NOT_CLEARED_FOR_ENDPOINT")
        require(not c.state_registry_required or c.state_registry_current, "STATE_REGISTRY_MISSING")
        require(not c.state_registry_required or not c.state_registry_listed, "STATE_REGISTRY_LISTED")
        require(not c.consent_required or c.consent_valid, "CONSENT_MISSING")
        require(not c.consent_revoked, "CONSENT_REVOKED")
        # A live conversation may finish, and a due requested callback is exempt (owner decision D4).
        for zone in () if c.continuing_call or c.callback_due else calling_zones(c):
            try:
                local = c.now.astimezone(ZoneInfo(zone))
            except (ZoneInfoNotFoundError, ValueError):
                reasons.append("TIMEZONE_UNCERTAIN")
                break
            if not outreach_open(local, c.local_start_hour, c.local_end_hour):
                reasons.append("OUTSIDE_CALLING_WINDOW")
                break
    require(c.rep_ready, "REP_NOT_READY")
    require(c.audio_ready, "AUDIO_NOT_READY")
    require(c.number_healthy, "NUMBER_UNHEALTHY")
    require(c.number_authorized, "NUMBER_NOT_AUTHORIZED")
    if not c.own_test_line:
        # A requested callback is a new promise outside the closed cold sequence.
        require(c.callback_due or c.attempt_count < c.max_attempts, "ATTEMPT_LIMIT")
        require(c.pair_open or c.next_attempt_at is None or c.now >= c.next_attempt_at, "NOT_DUE")
        require(
            c.callback_due
            or c.pair_open
            or c.last_attempt_at is None
            or (c.now - c.last_attempt_at).total_seconds() >= c.min_retry_seconds,
            "CADENCE_LIMIT",
        )
    require(c.budget_available, "BUDGET_EXHAUSTED")
    require(c.route_allowed, "ROUTE_NOT_ALLOWED")
    require(c.permission in (c.permitted_permissions or set()), "PERMISSION_NOT_GRANTED")
    return Decision(not reasons, reasons)
