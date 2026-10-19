from conftest import reserve, start
import pytest


@pytest.mark.parametrize("obligation", ["call", "inbound", "submitted_operation", "agent"])
def test_authority_change_refuses_unresolved_obligations(workspace, app, obligation):
    from app.db import put, update, get
    from test_remediation_privacy import principal

    c, h, p = workspace["client"], workspace["headers"], principal(workspace)
    service = app.state.service
    if obligation == "call":
        call = start(workspace)
        identity, table = call["id"], "call_attempts"
    else:
        table, payload = {
            "inbound": ("followup_tasks", {"inbound": True, "inbound_state": "routing", "status": "open"}),
            "submitted_operation": ("command_outbox", {"kind": "number_configure", "status": "uncertain"}),
            "agent": ("call_legs", {"kind": "agent", "state": "answered"}),
        }[obligation]
        with service.db.transaction() as s:
            identity = put(s, table, p["tenant_id"], p["seller_id"], {**payload, "rep_id": p["user_id"]}).id
    check = c.get("/v1/auth/quiescence", headers=h)
    assert check.status_code == 200, check.text
    assert not check.json()["can_disconnect"] and check.json()["reasons"]
    assert c.post("/v1/auth/logout", headers=h).status_code == 409
    assert c.get("/v1/workspace", headers=h).status_code == 200, "Blocked switch retains the session"
    if obligation == "call":
        assert c.post(f"/v1/calls/{identity}/hangup", headers=h).status_code == 200
    else:
        with service.db.transaction() as s:
            update(
                get(s, table, identity),
                **(
                    {"state": "ended"}
                    if table == "call_legs"
                    else {"status": "completed", "inbound_state": "ended"}
                ),
            )
    assert c.get("/v1/auth/quiescence", headers=h).json()["can_disconnect"]
    assert c.post("/v1/auth/logout", headers=h).status_code == 200


@pytest.mark.parametrize(
    "status", ["validating", "in_progress", "finalizing", "cancelling", "awaiting_publication"]
)
def test_pending_external_batch_prevents_authority_switch(workspace, app, status):
    from app.db import put
    from test_remediation_privacy import principal

    p = principal(workspace)
    with app.state.db.transaction() as session:
        put(
            session,
            "command_outbox",
            p["tenant_id"],
            p["seller_id"],
            {
                "kind": "script_batch",
                "status": status,
                "rep_id": p["user_id"],
                "provider_batch_id": "batch-fixture",
            },
        )
    response = workspace["client"].post("/v1/auth/logout", headers=workspace["headers"])
    assert response.status_code == 409


@pytest.mark.parametrize("action", ["logout", "pause"])
def test_quiescent_logout_releases_only_unused_rep_reservation(workspace, app, action):
    from app.db import ActiveClaim, get
    from sqlalchemy import select

    packet = reserve(workspace)
    assert packet.status_code == 200, packet.text
    identity = packet.json()["reservation_id"]
    response = workspace["client"].post(
        "/v1/auth/logout" if action == "logout" else "/v1/rep/presence",
        headers=workspace["headers"],
        **(
            {"json": {"status": "paused", "audio_ready": False, "telephony_ready": False}}
            if action == "pause"
            else {}
        ),
    )
    assert response.status_code == 200
    with app.state.db.transaction(False) as session:
        assert session.scalar(select(ActiveClaim).where(ActiveClaim.reservation_id == identity)) is None
        assert get(session, "reservations", identity).data["status"] == "cancelled"


def test_registry_import_validates_authorization_and_checksum_atomically(workspace):
    c, h = workspace["client"], workspace["headers"]
    body = {
        "kind": "national",
        "npas": ["415"],
        "subscription_reference": "authorized-fixture",
        "refreshed_at": "2026-10-14T17:00:00+00:00",
        "numbers": ["+14155550101"],
    }
    before = len(c.get("/v1/workspace", headers=h).json()["registries"])
    assert c.post("/v1/admin/registry/import", headers=h, json={**body, "checksum": "bad"}).status_code == 422
    assert len(c.get("/v1/workspace", headers=h).json()["registries"]) == before
    r = c.post("/v1/admin/registry/import", headers=h, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["published"] and r.json()["checksum"]
    packet = reserve(workspace)
    assert packet.status_code == 409
    assert "REGISTRY" in packet.text


def test_replacement_endpoint_preserves_old_optout_and_lead_identity(workspace):
    c, h = workspace["client"], workspace["headers"]
    lead = c.get("/v1/workspace", headers=h).json()["leads"][0]
    c.post("/v1/suppressions", headers=h, json={"phone": lead["phone"], "reason": "wrong_party"})
    result = c.patch(
        f"/v1/leads/{lead['id']}/endpoint",
        headers=h,
        json={"phone": "+14155550188", "evidence": "Company-provided new contact"},
    )
    assert result.status_code == 200, result.text
    corrected = c.get(f"/v1/leads/{lead['id']}/packet", headers=h).json()["lead"]
    assert corrected["id"] == lead["id"] and corrected["phone"] == "+14155550188"
    reimport = c.post(
        "/v1/leads/import",
        headers=h,
        json={
            "campaign_id": workspace["campaign_id"],
            "rows": [{"name": "Old endpoint", "phone": lead["phone"]}],
        },
    )
    assert reimport.json()["leads"][0]["suppressed"]


def test_playbook_publication_preserves_versions_and_validates_identity(workspace):
    c, h = workspace["client"], workspace["headers"]
    old = c.get("/v1/workspace", headers=h).json()["playbooks"][0]
    data = {
        "name": "Revised introduction",
        "expected_previous_id": old["id"],
        "expected_previous_version": old["version"],
        "opener": "Hi, this is {rep} from Example Seller.",
        "screening_intro": "{rep} from Example Seller, calling about business needs.",
        "cards": old["cards"],
        "approved": True,
        "reason": "Reviewed for accuracy",
    }
    r = c.post("/v1/admin/playbooks", headers=h, json=data)
    assert r.status_code == 200, r.text
    versions = c.get("/v1/workspace", headers=h).json()["playbooks"]
    assert len(versions) == 2
    # Validate screening identity against the current version. Approved seller wording
    # is not subject to the removed guarantee-keyword ban.
    data.update(expected_previous_id=r.json()["id"], expected_previous_version=r.json()["version"])
    assert (
        c.post(
            "/v1/admin/playbooks",
            headers=h,
            json={**data, "screening_intro": "Calling without seller identity"},
        ).status_code
        == 422
    )


def test_agent_dtmf_and_detached_voicemail_are_separately_controlled(workspace, carrier):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    assert (
        c.post(
            f"/v1/calls/{call['id']}/dtmf",
            headers=h,
            json={"digits": "42#", "idempotency_key": "keypad-compatibility"},
        ).status_code
        == 200
    )
    assert (
        c.post(
            f"/v1/calls/{call['id']}/dtmf",
            headers=h,
            json={"digits": "javascript:bad", "idempotency_key": "keypad-invalid"},
        ).status_code
        == 422
    )
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    assert (
        c.post(
            f"/v1/calls/{call['id']}/dtmf", headers=h, json={"digits": "1", "idempotency_key": "keypad-ended"}
        ).status_code
        == 409
    )


def test_maintenance_tick_ends_expired_live_obligation(workspace, clock):
    import asyncio
    from datetime import timedelta

    call = start(workspace)
    clock.now += timedelta(hours=3)
    service = workspace["client"].app.state.service
    asyncio.run(service.maintenance_tick())
    response = workspace["client"].get(f"/v1/calls/{call['id']}", headers=workspace["headers"])
    assert response.json()["state"] == "ENDED"
