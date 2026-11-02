from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Ordinary retries of an unanswered lead wait at least a day; requested callbacks and the
# final-stage second attempt are separate actions with their own timing.
ORDINARY_RETRY_SECONDS = 86400


def retry_seconds(policy):
    """A campaign may wait longer than a day between ordinary attempts, never less."""
    return max(ORDINARY_RETRY_SECONDS, int(policy.get("min_retry_seconds") or 0))


@dataclass
class CallContext:
    now: datetime
    authorization_reconciled: bool = True
    outreach_hold: bool = False
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
    local_end_hour: int = 21
    permission: str = "live"
    permitted_permissions: set[str] | None = None


@dataclass
class Decision:
    allowed: bool
    reasons: list[str]


def calling_zones(c: CallContext):
    if c.timezone and c.timezone_confidence in {"confirmed", "high"}:
        return (c.timezone,)
    return tuple(c.timezones or ())


def authorize(c: CallContext) -> Decision:
    reasons = []

    def require(condition, reason):
        if not condition:
            reasons.append(reason)

    require(c.authorization_reconciled, "RESTORE_RECONCILIATION_REQUIRED")
    require(not c.outreach_hold, "OUTREACH_HOLD")
    require(c.callback_owner_matches, "CALLBACK_ASSIGNED_TO_ANOTHER_REP")
    require(c.endpoint_valid, "INVALID_ENDPOINT")
    require(c.ownership != "wrong_party", "WRONG_PARTY")
    require(not c.internal_suppressed, "INTERNAL_SUPPRESSION")
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
    zones = calling_zones(c)
    if not zones:
        reasons.append("TIMEZONE_UNCERTAIN")
    for zone in zones:
        try:
            local = c.now.astimezone(ZoneInfo(zone))
        except (ZoneInfoNotFoundError, ValueError):
            reasons.append("TIMEZONE_UNCERTAIN")
            break
        if not c.local_start_hour <= local.hour < c.local_end_hour:
            reasons.append("OUTSIDE_CALLING_WINDOW")
            break
    require(c.rep_ready, "REP_NOT_READY")
    require(c.audio_ready, "AUDIO_NOT_READY")
    require(c.number_healthy, "NUMBER_UNHEALTHY")
    require(c.number_authorized, "NUMBER_NOT_AUTHORIZED")
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
