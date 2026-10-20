from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass
class CallContext:
    now: datetime
    seller_approved: bool = False
    authorization_reconciled: bool = True
    journal_reconciled: bool = True
    policy_published: bool = False
    outreach_hold: bool = False
    callback_owner_matches: bool = True
    endpoint_valid: bool = False
    ownership: str = "unknown"
    timezone: str | None = None
    timezone_confidence: str = "unknown"
    registry_required: bool = True
    registry_checked_at: datetime | None = None
    registry_covered: bool = False
    registry_listed: bool = False
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
    min_retry_seconds: int = 3600
    budget_available: bool = False
    route_allowed: bool = False
    local_start_hour: int = 9
    local_end_hour: int = 20
    permission: str = "live"
    permitted_permissions: set[str] | None = None


@dataclass
class Decision:
    allowed: bool
    reasons: list[str]


def authorize(c: CallContext) -> Decision:
    reasons = []

    def require(condition, reason):
        if not condition:
            reasons.append(reason)

    require(c.seller_approved, "SELLER_NOT_APPROVED")
    require(c.authorization_reconciled, "RESTORE_RECONCILIATION_REQUIRED")
    require(c.journal_reconciled, "JOURNAL_RECOVERY_REQUIRED")
    require(c.policy_published, "POLICY_NOT_PUBLISHED")
    require(not c.outreach_hold, "OUTREACH_HOLD")
    require(c.callback_owner_matches, "CALLBACK_ASSIGNED_TO_ANOTHER_REP")
    require(c.endpoint_valid, "INVALID_ENDPOINT")
    require(c.ownership != "wrong_party", "WRONG_PARTY")
    require(not c.internal_suppressed, "INTERNAL_SUPPRESSION")
    if c.registry_required:
        if c.registry_decision is not None:
            # Only a clearance computed for this exact endpoint permits a dial; an account-level
            # "available" or endpoint-free decision is never a clearance.
            national = c.registry_decision
            if national.get("decision") != "clear":
                codes = national.get("reason_codes") or ["REGISTRY_NOT_CLEARED_FOR_ENDPOINT"]
                reasons.extend(r for r in codes if r not in reasons)
            elif not national.get("endpoint"):
                reasons.append("REGISTRY_NOT_CLEARED_FOR_ENDPOINT")
        else:  # Legacy pure-policy callers; runtime always supplies the shared national decision.
            require(c.registry_checked_at is not None, "REGISTRY_MISSING")
            if c.registry_checked_at:
                require(0 <= (c.now - c.registry_checked_at).total_seconds() <= 31 * 86400, "REGISTRY_STALE")
            require(c.registry_covered, "REGISTRY_COVERAGE_MISSING")
            require(not c.registry_listed, "REGISTRY_LISTED")
    require(not c.state_registry_required or c.state_registry_current, "STATE_REGISTRY_MISSING")
    require(not c.state_registry_required or not c.state_registry_listed, "STATE_REGISTRY_LISTED")
    require(not c.consent_required or c.consent_valid, "CONSENT_MISSING")
    require(not c.consent_revoked, "CONSENT_REVOKED")
    if c.timezone and c.timezone_confidence in {"confirmed", "high"}:
        try:
            local = c.now.astimezone(ZoneInfo(c.timezone))
            require(c.local_start_hour <= local.hour < c.local_end_hour, "OUTSIDE_CALLING_WINDOW")
        except (ZoneInfoNotFoundError, ValueError):
            reasons.append("TIMEZONE_UNCERTAIN")
    else:
        reasons.append("TIMEZONE_UNCERTAIN")
    require(c.rep_ready, "REP_NOT_READY")
    require(c.audio_ready, "AUDIO_NOT_READY")
    require(c.number_healthy, "NUMBER_UNHEALTHY")
    require(c.number_authorized, "NUMBER_NOT_AUTHORIZED")
    require(c.attempt_count < c.max_attempts, "ATTEMPT_LIMIT")
    require(c.next_attempt_at is None or c.now >= c.next_attempt_at, "NOT_DUE")
    require(
        c.last_attempt_at is None or (c.now - c.last_attempt_at).total_seconds() >= c.min_retry_seconds,
        "CADENCE_LIMIT",
    )
    require(c.budget_available, "BUDGET_EXHAUSTED")
    require(c.route_allowed, "ROUTE_NOT_ALLOWED")
    require(c.permission in (c.permitted_permissions or set()), "PERMISSION_NOT_GRANTED")
    return Decision(not reasons, reasons)
