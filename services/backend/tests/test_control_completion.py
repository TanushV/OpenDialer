"""A05/A06/A09: response ordering, owner fences and bounded control recovery."""

import asyncio
import base64
import json
from datetime import timedelta

import httpx
import pytest
from app.db import ActiveClaim, doc, get, put, update
from app.providers.simulated import UncertainCarrierResult
from app.providers.telnyx import TelnyxCarrier
from conftest import start
from sqlalchemy import select


def principal(w):
    return {k: w[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


def command_for(service, p, call_id, kind="dial"):
    with service.db.transaction(False) as s:
        return next(
            doc(r)
            for r in service.scoped(s, "command_outbox", p)
            if r.data.get("call_id") == call_id and r.data.get("kind") == kind
        )


@pytest.mark.parametrize(
    "event,state",
    [
        ("call.answered", "ANSWERED_UNCLASSIFIED"),
        ("human.confirmed", "HUMAN"),
        ("call.hangup", "ENDED"),
    ],
)
@pytest.mark.parametrize("response", ["success", "timeout"])
def test_dial_response_cannot_regress_newer_provider_event(
    workspace, app, carrier, monkeypatch, event, state, response
):
    service, p = app.state.service, principal(workspace)
    original = carrier.dial

    async def event_before_response(payload):
        receipt = await original(payload)
        service.event(p, "before-dial-response", event, payload["command_id"], receipt)
        if response == "timeout":
            raise UncertainCarrierResult()
        return receipt

    monkeypatch.setattr(carrier, "dial", event_before_response)
    call = start(workspace)
    assert call["state"] == state
    assert call["provider_id"], "Even a terminal signed event binds the original dial"
    assert command_for(service, p, call["id"])["status"] == "completed"


@pytest.mark.parametrize("event,state", [("call.answered", "answered"), ("call.hangup", "ended")])
@pytest.mark.parametrize("response", ["success", "timeout"])
@pytest.mark.asyncio
async def test_agent_submission_cannot_regress_newer_event(
    workspace, app, carrier, monkeypatch, event, state, response
):
    service, p = app.state.service, principal(workspace)
    service.settings.simulation = False
    service.vault.write(p["tenant_id"], {"sip_username": "test-rep", "telephony_credential_id": "test-cred"})

    async def verified(_):
        return True

    async def event_before_response(payload):
        receipt = {"call_control_id": "agent-ordering", "client_state": payload["client_state"]}
        assert service.agent_event(p["tenant_id"], receipt, event)
        if response == "timeout":
            raise UncertainCarrierResult()
        return receipt

    monkeypatch.setattr(carrier, "verify_incoming_only_credential", verified, raising=False)
    monkeypatch.setattr(carrier, "dial", event_before_response)
    try:
        await service.create_agent(p)
    except Exception:
        if response != "timeout":
            raise
    with service.db.transaction(False) as s:
        legs = [r for r in service.scoped(s, "call_legs", p) if r.data.get("kind") == "agent"]
        assert len(legs) == 1 and legs[0].data["state"] == state


@pytest.mark.parametrize("provider_id", [None, "different-agent"])
def test_agent_client_state_cannot_replace_known_leg_identity(workspace, app, provider_id):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        leg = put(
            s,
            "call_legs",
            p["tenant_id"],
            p["seller_id"],
            {"kind": "agent", "rep_id": p["user_id"], "provider_id": "known-agent", "state": "answered"},
        )
        identity = leg.id
    payload = {
        "call_control_id": provider_id,
        "client_state": base64.b64encode(json.dumps({"agent_leg_id": identity}).encode()).decode(),
    }
    assert not service.agent_event(p["tenant_id"], payload, "call.hangup")
    with service.db.transaction(False) as s:
        assert get(s, "call_legs", identity).data["provider_id"] == "known-agent"
        assert get(s, "call_legs", identity).data["state"] == "answered"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{}, {"data": {}}, {"data": {"call_control_id": None}}, {"data": {"call_control_id": 3}}]
)
async def test_malformed_successful_dial_response_is_uncertain_not_rejected(payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        with pytest.raises(UncertainCarrierResult):
            await TelnyxCarrier("test", "test", http=http).dial({"command_id": "original"})


@pytest.mark.asyncio
async def test_hangup_is_claimed_once_while_provider_request_is_running(workspace, app, carrier, monkeypatch):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    started, release = asyncio.Event(), asyncio.Event()
    submissions = []

    async def delayed(provider_id, command_id):
        submissions.append(command_id)
        started.set()
        await release.wait()
        return {"result": "ok"}

    monkeypatch.setattr(carrier, "hangup", delayed)
    first = asyncio.create_task(service.hangup(p, call["id"]))
    await started.wait()
    try:
        await asyncio.wait_for(service.hangup(p, call["id"]), 0.25)
        assert len(submissions) == 1
    finally:
        release.set()
        await first


@pytest.mark.asyncio
async def test_hangup_response_lost_owner_cannot_complete_new_owner_command(
    workspace, app, carrier, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)

    async def ownership_changed(*_):
        with service.db.transaction() as s:
            command = next(
                r for r in service.scoped(s, "command_outbox", p) if r.data.get("kind") == "hangup"
            )
            update(
                command,
                status="sending",
                lease_owner="new-owner",
                lease_until=(service.clock() + timedelta(seconds=30)).isoformat(),
            )
        return {"result": "ok"}

    monkeypatch.setattr(carrier, "hangup", ownership_changed)
    await service.hangup(p, call["id"])
    command = command_for(service, p, call["id"], "hangup")
    assert command["status"] == "sending" and command["lease_owner"] == "new-owner"
    with service.db.transaction(False) as s:
        assert s.scalar(select(ActiveClaim).where(ActiveClaim.call_id == call["id"])) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["answer", "bridge", "transfer", "gather", "speak", "transcription_start"])
async def test_ended_call_cannot_start_old_control(workspace, app, carrier, action):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    service.event(p, "ended-before-control", "call.hangup", call["id"])
    with service.db.transaction() as s:
        command = service.control_intent(s, p, call["id"], action, {})
        identity = command.id
    await service.dispatch_controls(p)
    assert not carrier.actions
    with service.db.transaction(False) as s:
        assert get(s, "command_outbox", identity).data["status"] == "cancelled"


@pytest.mark.asyncio
async def test_missing_control_target_becomes_actionable_review(workspace, app):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    with service.db.transaction() as s:
        update(get(s, "call_attempts", call["id"]), provider_id=None)
        command = service.control_intent(s, p, call["id"], "transcription_stop", {})
        identity = command.id
    await service.dispatch_controls(p)
    with service.db.transaction(False) as s:
        assert get(s, "command_outbox", identity).data["status"] == "review_required"


@pytest.mark.asyncio
async def test_reconciliation_does_not_downgrade_a_human_call(workspace, app, carrier):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    service.event(p, "human-before-reconcile", "human.confirmed", call["id"])
    with service.db.transaction() as s:
        command = get(s, "command_outbox", command_for(service, p, call["id"])["id"])
        update(command, status="uncertain")
    await service.recover(p)
    with service.db.transaction(False) as s:
        assert get(s, "call_attempts", call["id"]).data["state"] == "HUMAN"


@pytest.mark.asyncio
async def test_old_uncertain_dial_without_receipt_requires_review_not_permanent_sending(
    workspace, app, carrier, clock, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    identity = command_for(service, p, call["id"])["id"]
    with service.db.transaction() as s:
        update(get(s, "call_attempts", call["id"]), provider_id=None, state="RECONCILING")
        update(
            get(s, "command_outbox", identity),
            status="sending",
            provider_id=None,
            lease_until=(clock.now - timedelta(seconds=1)).isoformat(),
        )

    async def no_receipt(*_):
        return None

    monkeypatch.setattr(carrier, "reconcile", no_receipt)
    await service.recover(p)
    assert len(carrier.dials) == 1
    with service.db.transaction(False) as s:
        assert get(s, "command_outbox", identity).data["status"] == "review_required"
        assert s.scalar(select(ActiveClaim).where(ActiveClaim.call_id == call["id"])) is not None
