import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from app.compliance.phone import normalize_phone
from app.compliance.policy import CallContext, authorize
from app.compliance.registry import RegistryBitmap, RegistryError

NOW = datetime(2026, 10, 14, 18, tzinfo=UTC)


@pytest.fixture
def context():
    return CallContext(
        now=NOW,
        seller_approved=True,
        policy_published=True,
        endpoint_valid=True,
        ownership="confirmed_match",
        timezone="America/Los_Angeles",
        timezone_confidence="confirmed",
        registry_required=True,
        registry_decision={"decision": "clear", "endpoint": "+14155550101"},
        state_registry_required=False,
        state_registry_current=True,
        internal_suppressed=False,
        consent_required=False,
        consent_valid=True,
        consent_revoked=False,
        rep_ready=True,
        audio_ready=True,
        number_healthy=True,
        number_authorized=True,
        attempt_count=0,
        max_attempts=4,
        next_attempt_at=NOW - timedelta(seconds=1),
        last_attempt_at=None,
        min_retry_seconds=3600,
        budget_available=True,
        route_allowed=True,
        local_start_hour=9,
        local_end_hour=20,
        permission="live",
        permitted_permissions={"live"},
    )


def test_valid_context_is_authorized(context):
    assert authorize(context).allowed


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"seller_approved": False}, "SELLER_NOT_APPROVED"),
        ({"policy_published": False}, "POLICY_NOT_PUBLISHED"),
        ({"endpoint_valid": False}, "INVALID_ENDPOINT"),
        ({"ownership": "wrong_party"}, "WRONG_PARTY"),
        ({"internal_suppressed": True}, "INTERNAL_SUPPRESSION"),
        ({"registry_decision": None}, "REGISTRY_NOT_CLEARED_FOR_ENDPOINT"),
        ({"registry_decision": {"decision": "clear"}}, "REGISTRY_NOT_CLEARED_FOR_ENDPOINT"),
        (
            {"registry_decision": {"decision": "listed", "reason_codes": ["REGISTRY_LISTED"]}},
            "REGISTRY_LISTED",
        ),
        ({"state_registry_required": True, "state_registry_current": False}, "STATE_REGISTRY_MISSING"),
        ({"consent_required": True, "consent_valid": False}, "CONSENT_MISSING"),
        ({"consent_revoked": True}, "CONSENT_REVOKED"),
        ({"timezone": None}, "TIMEZONE_UNCERTAIN"),
        ({"timezone_confidence": "unknown"}, "TIMEZONE_UNCERTAIN"),
        ({"timezone": "made/up"}, "TIMEZONE_UNCERTAIN"),
        ({"now": datetime(2026, 10, 14, 4, tzinfo=UTC)}, "OUTSIDE_CALLING_WINDOW"),
        ({"rep_ready": False}, "REP_NOT_READY"),
        ({"audio_ready": False}, "AUDIO_NOT_READY"),
        ({"number_healthy": False}, "NUMBER_UNHEALTHY"),
        ({"number_authorized": False}, "NUMBER_NOT_AUTHORIZED"),
        ({"attempt_count": 4}, "ATTEMPT_LIMIT"),
        ({"next_attempt_at": NOW + timedelta(hours=1)}, "NOT_DUE"),
        ({"last_attempt_at": NOW - timedelta(seconds=60)}, "CADENCE_LIMIT"),
        ({"budget_available": False}, "BUDGET_EXHAUSTED"),
        ({"route_allowed": False}, "ROUTE_NOT_ALLOWED"),
        ({"permission": "prerecorded"}, "PERMISSION_NOT_GRANTED"),
        ({"permission": "recording"}, "PERMISSION_NOT_GRANTED"),
        ({"permission": "transcription"}, "PERMISSION_NOT_GRANTED"),
    ],
)
def test_fail_closed_on_each_independent_rule(context, changes, reason):
    decision = authorize(replace(context, **changes))
    assert not decision.allowed
    assert reason in decision.reasons


def test_revocation_cannot_be_bypassed_by_requested_callback(context):
    assert not authorize(replace(context, consent_revoked=True, registry_required=False)).allowed


@pytest.mark.parametrize(
    "raw,expected,extension",
    [
        ("(415) 555-0101", "+14155550101", None),
        ("+1 212 555 0102 ext 42", "+12125550102", "42"),
        ("001", None, None),
        ("javascript:123", None, None),
    ],
)
def test_normalization_does_not_claim_ownership(raw, expected, extension):
    result = normalize_phone(raw)
    assert result.e164 == expected
    assert result.extension == extension
    assert result.ownership_status == "unknown"
    assert result.assignment_evidence == "unknown"


def test_exact_registry_bitmap_roundtrip_and_checksum():
    numbers = ["+14155550101", "+14155550102", "+12125550103"]
    bitmap = RegistryBitmap.build(numbers)
    assert bitmap.contains("+14155550101")
    assert not bitmap.contains("+14155550109")
    checksum = hashlib.sha256(bitmap.dump()).hexdigest()
    assert RegistryBitmap.load(bitmap.dump(), checksum).contains("+12125550103")
    with pytest.raises(RegistryError):
        RegistryBitmap.load(bitmap.dump(), "wrong-checksum")
