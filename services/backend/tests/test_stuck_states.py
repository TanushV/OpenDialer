"""Every stuck obligation reaches a terminal or operator-review state without blocking the rep."""

import asyncio
from datetime import timedelta

import pytest
from app.db import ActiveClaim, doc, get, put, update
from app.providers.simulated import CarrierNotSubmitted, SimulatedCarrier, UncertainCarrierResult
from conftest import ready, reserve, start
from fastapi import HTTPException
from sqlalchemy import select
from test_context_reads import count_decrypted
from test_control_completion import command_for, principal
from test_inbound_completion import dispatch, incoming, previous_call, saved, target_payload, webhook


def claims(service, call_id):
    with service.db.transaction(False) as s:
        return [
            (c.rep_id, c.endpoint_id)
            for c in s.scalars(select(ActiveClaim).where(ActiveClaim.call_id == call_id))
        ]


def call_data(service, call_id):
    with service.db.transaction(False) as s:
        return doc(get(s, "call_attempts", call_id))


@pytest.mark.parametrize("failure", ["configuration", "connect"])
def test_dial_that_never_left_the_process_fails_and_releases_the_rep(
    workspace, app, carrier, monkeypatch, failure
):
    service, p = app.state.service, principal(workspace)
    if failure == "configuration":

        def unconfigured(_):
            raise HTTPException(503, "TELNYX_NOT_CONFIGURED")

        monkeypatch.setattr(service, "carrier", unconfigured)
    else:

        async def refused(payload):
            raise CarrierNotSubmitted("connection refused")

        monkeypatch.setattr(carrier, "dial", refused)
    call = start(workspace)
    saved_call = call_data(service, call["id"])
    assert saved_call["state"] == "ENDED" and saved_call["end_reason"] == "carrier_not_submitted"
    assert command_for(service, p, call["id"])["status"] == "failed"
    assert claims(service, call["id"]) == []
    with service.db.transaction(False) as s:
        lead = get(s, "leads", call["lead_id"]).data
    assert lead["attempt_count"] == 0, "A dial that never reached the carrier is not an attempt"
    assert reserve(workspace).status_code == 200


def unacknowledged(workspace, app, carrier, monkeypatch):
    async def lost(payload):
        raise UncertainCarrierResult("response lost before any carrier reference")

    monkeypatch.setattr(carrier, "dial", lost)
    call = start(workspace)
    monkeypatch.setattr(carrier, "dial", SimulatedCarrier.dial.__get__(carrier))
    return call


def test_unacknowledged_dial_moves_to_review_and_frees_only_the_rep(
    workspace, app, carrier, clock, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    call = unacknowledged(workspace, app, carrier, monkeypatch)
    assert call_data(service, call["id"])["state"] == "RECONCILING"
    asyncio.run(service.recover(p))
    assert call_data(service, call["id"])["state"] == "RECONCILING", "Evidence may still arrive"
    clock.now += timedelta(seconds=121)
    asyncio.run(service.recover(p))
    saved_call = call_data(service, call["id"])
    assert saved_call["state"] == "DIAL_UNCONFIRMED"
    assert command_for(service, p, call["id"])["status"] == "unconfirmed"
    ((holder, endpoint),) = claims(service, call["id"])
    assert holder != workspace["user_id"] and endpoint == call["endpoint_id"]
    next_packet = reserve(workspace)
    assert next_packet.status_code == 200, next_packet.text
    assert next_packet.json()["lead"]["id"] != call["lead_id"], "The unresolved prospect stays held"
    version = saved_call["version"]
    clock.now += timedelta(minutes=5)
    asyncio.run(service.recover(p))
    assert call_data(service, call["id"])["version"] == version, "Out of the automatic recovery loop"
    assert len(carrier.dials) == 0


def test_rep_hangup_of_an_unacknowledged_dial_also_leaves_the_loop(
    workspace, app, carrier, clock, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    call = unacknowledged(workspace, app, carrier, monkeypatch)
    workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    assert call_data(service, call["id"])["state"] == "ENDING"
    clock.now += timedelta(seconds=121)
    asyncio.run(service.recover(p))
    assert call_data(service, call["id"])["state"] == "DIAL_UNCONFIRMED"
    hangup = command_for(service, p, call["id"], "hangup")
    assert hangup["status"] == "unconfirmed"
    version = call_data(service, call["id"])["version"]
    clock.now += timedelta(minutes=5)
    asyncio.run(service.recover(p))
    assert call_data(service, call["id"])["version"] == version


def recovery_item(workspace, call_id, kind="dial"):
    items = workspace["client"].get("/v1/admin/recovery", headers=workspace["headers"]).json()["commands"]
    return next(i for i in items if i["call_id"] == call_id and i["kind"] == kind)


def test_operator_confirms_not_placed_with_a_reason_and_releases_the_prospect(
    workspace, app, carrier, clock, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    call = unacknowledged(workspace, app, carrier, monkeypatch)
    clock.now += timedelta(seconds=121)
    asyncio.run(service.recover(p))
    item = recovery_item(workspace, call["id"])
    assert item["can_confirm_not_placed"] is True
    url = "/v1/admin/recovery/commands/" + item["id"]
    body = {"expected_version": item["version"], "action": "confirm_not_placed"}
    assert workspace["client"].post(url, headers=workspace["headers"], json=body).status_code == 422
    result = workspace["client"].post(
        url,
        headers=workspace["headers"],
        json={**body, "reason": "Carrier portal shows no call at that time"},
    )
    assert result.status_code == 200, result.text
    saved_call = call_data(service, call["id"])
    assert saved_call["state"] == "ENDED" and saved_call["end_reason"] == "operator_confirmed_not_placed"
    assert claims(service, call["id"]) == []
    assert command_for(service, p, call["id"])["status"] == "cancelled"
    with service.db.transaction(False) as s:
        audit = [
            r.data
            for r in service.scoped(s, "audit_events", p)
            if r.data.get("action") == "recovery.confirm_not_placed"
        ]
    assert audit and audit[0]["detail"]["reason"] == "Carrier portal shows no call at that time"


def test_confirm_not_placed_is_refused_while_evidence_can_still_arrive(workspace, app, carrier, monkeypatch):
    call = unacknowledged(workspace, app, carrier, monkeypatch)
    item = recovery_item(workspace, call["id"])
    assert item["can_confirm_not_placed"] is False
    result = workspace["client"].post(
        "/v1/admin/recovery/commands/" + item["id"],
        headers=workspace["headers"],
        json={"expected_version": item["version"], "action": "confirm_not_placed", "reason": "guess"},
    )
    assert result.status_code == 409


def test_late_signed_event_reattaches_an_unconfirmed_dial(workspace, app, carrier, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    call = unacknowledged(workspace, app, carrier, monkeypatch)
    clock.now += timedelta(seconds=121)
    asyncio.run(service.recover(p))
    service.event(p, "late-initiated", "call.initiated", call["id"], {"call_control_id": "late-leg"})
    assert call_data(service, call["id"])["provider_id"] == "late-leg"
    assert command_for(service, p, call["id"])["status"] == "completed"


def test_caller_hangup_ends_a_transfer_that_never_reported_its_leg(workspace, carrier):
    _, phone = previous_call(workspace)
    task = incoming(workspace, phone=phone)
    dispatch(workspace)
    assert saved(workspace, "followup_tasks", task["id"])["routing_attempts"][0]["state"] == "sending"
    webhook(workspace, "call.hangup", {"call_control_id": "inbound-1"}, "caller-left")
    current = saved(workspace, "followup_tasks", task["id"])
    assert current["inbound_state"] == "ended", current
    assert claims(workspace["client"].app.state.service, task["id"]) == []
    assert reserve(workspace).status_code == 200
    # A transfer leg that reports after the caller left is stopped, never connected.
    webhook(workspace, "call.initiated", target_payload(carrier, "late-transfer-leg"), "late-leg")
    dispatch(workspace)
    assert any(h["provider_id"] == "late-transfer-leg" for h in carrier.hangups)


def test_routing_deadline_releases_the_rep_when_no_leg_or_caller_receipt_arrives(workspace, carrier, clock):
    _, phone = previous_call(workspace)
    task = incoming(workspace, phone=phone)
    dispatch(workspace)
    clock.now += timedelta(minutes=2)
    dispatch(workspace)
    current = saved(workspace, "followup_tasks", task["id"])
    assert current["routing_attempts"][0]["state"] == "orphaned"
    assert claims(workspace["client"].app.state.service, task["id"]) == []
    assert reserve(workspace).status_code == 200


def test_expired_inbound_claim_cannot_block_the_rep_forever(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        s.add(
            ActiveClaim(
                tenant_id=p["tenant_id"],
                seller_id=p["seller_id"],
                endpoint_id="inbound-task",
                lead_id="inbound-task",
                rep_id=p["user_id"],
                number_id="number",
                reservation_id="inbound-task",
                call_id="inbound-task",
                expires_at=clock.now + timedelta(seconds=10),
            )
        )
    clock.now += timedelta(minutes=5)
    asyncio.run(service.recover(p))
    assert claims(service, "inbound-task") == []


def test_inbound_webhook_reads_indexed_identities_not_the_seller_history(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    _, phone = previous_call(workspace)
    with service.db.transaction() as s:
        for i in range(150):
            put(
                s,
                "leads",
                p["tenant_id"],
                p["seller_id"],
                {"phone": f"+1212555{i:04d}", "endpoint_id": f"e{i}"},
            )
            put(
                s,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {"lead_id": f"l{i}", "started_at": clock.now.isoformat(), "state": "FINALIZED"},
            )
            put(
                s,
                "followup_tasks",
                p["tenant_id"],
                p["seller_id"],
                {"status": "completed", "provider_id": f"t{i}"},
            )
    decoded = count_decrypted(app, monkeypatch)
    task = incoming(workspace, phone=phone)
    assert task["route"] == "responsible_rep", {
        k: task.get(k) for k in ("lead_id", "previous_call_id", "responsible_rep_id", "ambiguous")
    }
    assert len(decoded) < 30, len(decoded)


def test_claim_time_denial_retries_selection_instead_of_failing(workspace, app, monkeypatch):
    import app.scheduling.queue as queue
    from app.compliance.policy import Decision

    original, denials = queue.authorize, []

    def deny_once(context):
        if not denials:
            denials.append(1)
            return Decision(False, ["OUTSIDE_CALLING_WINDOW"])
        return original(context)

    monkeypatch.setattr(queue, "authorize", deny_once)
    response = reserve(workspace)
    assert response.status_code == 200, response.text
    assert denials == [1]


def test_persistent_claim_denial_is_a_bounded_retryable_conflict(workspace, app, monkeypatch):
    import app.scheduling.queue as queue
    from app.compliance.policy import Decision

    monkeypatch.setattr(queue, "authorize", lambda context: Decision(False, ["OUTSIDE_CALLING_WINDOW"]))
    response = reserve(workspace)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["retryable"] is True
    assert "OUTSIDE_CALLING_WINDOW" in response.json()["detail"]["reasons"]


def test_start_after_the_agent_leg_dropped_is_a_clean_conflict(workspace, app):
    packet = reserve(workspace).json()
    service = app.state.service
    service.agent_event(workspace["tenant_id"], {"call_control_id": "agent-test-id"}, "call.hangup")
    with service.db.transaction() as s:
        user = get(s, "users", workspace["user_id"])
        update(user, presence={**user.data["presence"], "agent_call_control_id": None})
    response = workspace["client"].post(
        "/v1/calls/start",
        headers=workspace["headers"],
        json={"reservation_id": packet["reservation_id"], "idempotency_key": "after-drop"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "REP_AUDIO_NOT_READY"


def test_live_start_rechecks_that_the_server_verified_agent_leg_is_still_answered(
    workspace, app, carrier, monkeypatch
):
    service = app.state.service
    ready(workspace, agent_call_control_id="agent-test-id")
    packet = reserve(workspace).json()
    service.settings.simulation = False
    with service.db.transaction() as s:
        put(
            s,
            "call_legs",
            workspace["tenant_id"],
            workspace["seller_id"],
            {
                "kind": "agent",
                "rep_id": workspace["user_id"],
                "provider_id": "agent-test-id",
                "state": "ended",
            },
            key="agent-test-id",
        )
    response = workspace["client"].post(
        "/v1/calls/start",
        headers=workspace["headers"],
        json={"reservation_id": packet["reservation_id"], "idempotency_key": "ended-leg"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "REP_AUDIO_NOT_READY"
    assert not carrier.dials


@pytest.mark.asyncio
async def test_simulator_cannot_reconcile_a_dial_by_command_id():
    carrier = SimulatedCarrier()
    receipt = await carrier.dial({"command_id": "command-1", "to": "+14155550101"})
    assert await carrier.reconcile("command-1") is None
    assert await carrier.reconcile("command-1", None) is None
    assert (await carrier.reconcile("anything", receipt["call_control_id"]))["call_control_id"] == receipt[
        "call_control_id"
    ]
