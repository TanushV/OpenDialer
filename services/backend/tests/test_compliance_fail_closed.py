"""Compliance inputs that are malformed, invalid or unrecorded block the call; nothing fails open."""

from datetime import timedelta

import pytest
from app.compliance.operating import screening_capability
from app.compliance.policy import CallContext, authorize
from app.compliance.registry import RegistryBitmap, RegistryError, registry_contains
from app.db import get
from conftest import reserve
from fastapi import HTTPException
from test_consent_journal import decision, evidence, submit
from test_control_completion import principal

PHONE = "+14155550101"


def test_consent_revocation_with_an_unverifiable_journal_persists_the_outbound_hold(
    workspace, app, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    original = service.vault.append_journal

    def unverifiable(name, record):
        if name == "consent":
            raise HTTPException(409, "Journal cannot be verified; recover its protected copy before resuming")
        return original(name, record)

    monkeypatch.setattr(service.vault, "append_journal", unverifiable)
    body = evidence(workspace, app)
    response = submit(workspace, body)
    assert response.status_code == 503, response.text
    # The hold must survive a restart, not only this process's memory.
    service.__dict__.pop("_journal_faults", None)
    with service.db.transaction(False) as s:
        assert get(s, "sellers", p["seller_id"]).data["journal_recovery_required"] is True
    assert "JOURNAL_RECOVERY_REQUIRED" in decision(service, p, body["endpoint_id"]).reasons


def snapshot(clock, *sources):
    return {
        "now": clock.now,
        "seller_id": "seller",
        "max_age_days": 31,
        "national": [
            {
                "subscription_reference": "own-registry-subscription",
                "npas": ["415"],
                "refreshed_at": (clock.now - timedelta(days=1)).isoformat(),
                "listed": False,
                **source,
            }
            for source in sources
        ],
    }


def test_invalid_covering_national_source_blocks_even_beside_a_clear_one(clock):
    result = screening_capability(snapshot(clock, {"source_invalid": True}, {}), PHONE)
    assert result["decision"] != "clear"
    assert "REGISTRY_SOURCE_INVALID" in result["reason_codes"]
    assert not authorize(CallContext(now=clock.now, registry_decision=result)).allowed


def test_invalid_source_for_another_area_code_does_not_block(clock):
    result = screening_capability(snapshot(clock, {"source_invalid": True, "npas": ["212"]}, {}), PHONE)
    assert result["decision"] == "clear"


class Snapshot:
    def __init__(self, listed):
        self.id, self.version, self.data = "registry", 1, {"listed_numbers": listed, "checksum": "x"}


@pytest.mark.parametrize("phone", ["+1415555010", "+14155550101x", "4155550101", "+1415555O101", ""])
def test_malformed_endpoint_is_not_reported_as_unlisted(phone):
    class Session:
        info = {}

    with pytest.raises(RegistryError):
        registry_contains(Session(), Snapshot([PHONE]), phone)
    with pytest.raises(RegistryError):
        RegistryBitmap.build([PHONE]).contains(phone)


@pytest.mark.parametrize(
    "national",
    [
        {"decision": "available", "reason_codes": [], "endpoint": None},
        {"decision": "pending", "reason_codes": [], "endpoint": PHONE},
        {"decision": "clear", "reason_codes": [], "endpoint": None},
        {"decision": "clear", "reason_codes": []},
    ],
)
def test_authorization_requires_a_clear_decision_for_this_endpoint(clock, national):
    context = CallContext(now=clock.now, registry_decision=national)
    assert "REGISTRY_NOT_CLEARED_FOR_ENDPOINT" in authorize(context).reasons


def test_endpoint_clear_decision_still_authorizes(clock):
    result = screening_capability(snapshot(clock, {}), PHONE)
    assert result["decision"] == "clear" and result["endpoint"] == PHONE
    assert (
        "REGISTRY_NOT_CLEARED_FOR_ENDPOINT"
        not in authorize(CallContext(now=clock.now, registry_decision=result)).reasons
    )


def test_preflight_states_when_state_lists_are_not_required(workspace):
    result = workspace["client"].get("/v1/admin/preflight", headers=workspace["headers"]).json()
    assert result["screening"]["state_registry_required"] is False
    assert any("State do-not-call lists are not required" in w for w in result["warnings"])


def hold(workspace, app, reason="temporary_hold"):
    service, p = app.state.service, principal(workspace)
    service.suppress(p, PHONE, reason, event_id="interim-transcript-optout")
    return service, p


def release(workspace, **body):
    return workspace["client"].post(
        "/v1/admin/suppressions/release-hold",
        headers=workspace["headers"],
        json={"phone": PHONE, "reason": "Final transcript showed no opt-out request", **body},
    )


def test_temporary_hold_keeps_blocking_until_a_reviewed_release(workspace, app, clock):
    service, p = hold(workspace, app)
    clock.now += timedelta(days=60)
    with service.db.transaction(False) as s:
        assert service.suppressed(s, p, PHONE), "A hold never lapses on its own"
    clock.now -= timedelta(days=60)  # Keep the operator session valid for the review below.
    assert release(workspace, reason="").status_code == 422
    assert release(workspace, reason="   ").status_code == 422
    response = release(workspace)
    assert response.status_code == 200, response.text
    with service.db.transaction(False) as s:
        assert not service.suppressed(s, p, PHONE)
        audit = [
            r.data
            for r in service.scoped(s, "audit_events", p)
            if r.data.get("action") == "suppression.hold_released"
        ]
    assert audit[0]["detail"]["reason"] == "Final transcript showed no opt-out request"
    # Journal replay at restart must not silently reinstate the reviewed release.
    from app.compliance.suppressions import replay

    replay(service)
    with service.db.transaction(False) as s:
        assert not service.suppressed(s, p, PHONE)
    assert reserve(workspace).status_code == 200


@pytest.mark.parametrize("reason", ["opt_out", "wrong_party"])
def test_permanent_denials_cannot_be_released_as_holds(workspace, app, reason):
    service, p = hold(workspace, app, reason)
    assert release(workspace).status_code == 409
    with service.db.transaction(False) as s:
        assert service.suppressed(s, p, PHONE)


def test_new_hold_after_a_release_blocks_again(workspace, app):
    service, p = hold(workspace, app)
    assert release(workspace).status_code == 200
    service.suppress(p, PHONE, "temporary_hold", event_id="second-interim-optout")
    with service.db.transaction(False) as s:
        assert service.suppressed(s, p, PHONE)
