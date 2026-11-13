"""The rep's audio leg (R4.4): one fresh leg per call while the WebRTC client stays registered.

Telnyx has no Dial option that parks the rep's leg, so the leg ends with each call and the screen asks for a new one.
The credential behind it is checked with Telnyx once per app session, not on every leg, so a new leg costs one dial.
"""

import asyncio
import base64
import json
import logging
import re
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select

from app import ops
from app.calling.blockers import blocked, refuse
from app.calling.records import AgentLeg, NumberView, UserView, presence_of
from app.numbers.pool import request
from app.platform.db import (
    MODELS,
    SCOPE_KEY,
    doc,
    scope_of,
    scoped_get,
    scoped_put,
    scoped_where,
    uid,
    update,
    workspace,
)
from app.platform.leases import owns_effect

ACTIVE = ("submitting", "uncertain", "ringing", "answered")
SIP_NAME = re.compile(r"[A-Za-z0-9_.+-]{1,128}")
LEASE = timedelta(seconds=30)

log = logging.getLogger(__name__)


def find(s, p, *, provider_id=None, leg_id=None, rep_id=None, active=False):
    """The rep's agent leg by carrier reference, local id, or the one still active."""
    model = MODELS["call_legs"]
    query = select(model).where(*scoped_where(model, p))
    if rep_id:
        query = query.where(model.op_owner_id == rep_id)
    if provider_id:
        query = query.where(model.op_provider_id == provider_id)
    elif leg_id:
        query = query.where(model.id == leg_id)
    elif active and rep_id:
        query = query.where(model.op_status.in_(ACTIVE))
    else:
        raise ValueError("Agent lookup requires a provider, local identity or active rep")
    candidates = list(s.scalars(query.limit(2)))
    if len(candidates) > 1:
        raise HTTPException(409, "Multiple agent identities need review before another setup")
    return next((row for row in candidates if row.data["kind"] == "agent"), None)


def identity(data, config, *, managed=False):
    """A managed profile's current SIP identity wins over an older workspace record."""
    sip, credential = config.get("sip_username"), config.get("telephony_credential_id")
    if managed and sip and credential:
        return sip, credential
    user = UserView.model_validate(data)
    return user.sip_username or sip, user.telephony_credential_id or credential


def setup(ctx, p, pool=None, *, managed=False):
    """(SIP name, credential, caller number) for the rep's leg, or the blocker that is missing."""
    conf = ctx.configs.read(workspace(p))
    with ctx.db.transaction(False) as s:
        user = scoped_get(s, p, "users", p["user_id"])
        sip, credential = identity(user.data, conf, managed=managed)
        if not sip or not SIP_NAME.fullmatch(sip) or not credential:
            refuse([blocked("AUDIO_SETUP_MISSING")])
        if pool is not None:
            # An internal SIP leg never spends a PSTN attempt, even at the outbound cap.
            if phone := next((n.phone for n in pool.numbers if n.outbound_enabled), None):
                return sip, credential, phone
            refuse([blocked("CALLER_NUMBER_PAUSED" if pool.numbers else "NO_CALLER_NUMBER", shared=True)])
        model = MODELS["number_inventory"]
        rows = s.scalars(select(model).where(*scoped_where(model, p)))
        numbers = [
            n
            for n in (NumberView.model_validate(row.data) for row in rows)
            if not n.shared_pool and n.lifecycle not in {"retired_callback_only", "paused_for_outbound"}
        ]
        routed = [n for n in numbers if n.identity_configured and n.provider_present is not False]
        if not routed:
            refuse([blocked("CALLER_NUMBER_ROUTE_REQUIRED" if numbers else "NO_CALLER_NUMBER")])
        ready = [n for n in routed if n.lifecycle in {"healthy", "limited_real_use"}]
        return sip, credential, (ready or routed)[0].phone


async def verify_once(ctx, carrier, credential, sip):
    """Check the credential with Telnyx the first time this session uses it."""
    from app.providers.telnyx import CarrierConfigurationError

    verified = ctx.memo.setdefault("verified_credentials", set())
    if (credential, sip) in verified:
        return
    try:
        await carrier.verify_incoming_only_credential(credential, sip_username=sip)
    except CarrierConfigurationError as exc:
        raise HTTPException(503, {"code": "TELEPHONY_CREDENTIAL_MISCONFIGURED", "message": str(exc)}) from exc
    verified.add((credential, sip))


def _claim(ctx, p, leg_id, owner):
    now = ctx.clock()
    with ctx.db.transaction() as s:
        prior = find(s, p, rep_id=p["user_id"], active=True)
        if prior:
            if AgentLeg.model_validate(prior.data).provider_id:
                return doc(prior), False
            raise HTTPException(
                409, "An agent leg is awaiting provider reconciliation; do not create a second leg"
            )
        record = AgentLeg(
            kind="agent",
            rep_id=p["user_id"],
            state="submitting",
            command_id=leg_id,
            lease_owner=owner,
            lease_until=(now + LEASE).isoformat(),
        )
        leg = scoped_put(s, p, "call_legs", record.model_dump(), object_id=leg_id, now=now)
    return doc(leg), True


def _locked(s, p, leg_id):
    row = scoped_get(s, p, "call_legs", leg_id)
    s.refresh(row, with_for_update=True)
    return row, AgentLeg.model_validate(row.data)


def _failed(ctx, p, leg_id, owner):
    with ctx.db.transaction() as s:
        row, leg = _locked(s, p, leg_id)
        if leg.state == "submitting" and owns_effect(row.data, owner, ctx.clock()):
            update(row, state="uncertain", lease_owner=None, lease_until=None)
        return leg.provider_id


def _completed(ctx, p, leg_id, owner, provider_id):
    with ctx.db.transaction() as s:
        row, leg = _locked(s, p, leg_id)
        if leg.provider_id not in {None, provider_id}:
            raise HTTPException(409, "Carrier returned a conflicting agent leg reference")
        # Signed events can establish or end the leg before the HTTP response.
        if leg.provider_id == provider_id and not leg.lease_owner:
            return
        if leg.state != "submitting" or not owns_effect(row.data, owner, ctx.clock()):
            raise HTTPException(409, "Agent reconciliation owns the result; refresh its current status")
        update(
            row,
            provider_id=provider_id,
            lease_owner=None,
            lease_until=None,
            state="ringing" if leg.state in {"submitting", "uncertain"} else leg.state,
        )


def _instant_leg(ctx, p):
    provider_id = "agent-" + uid()
    with ctx.db.transaction() as s:
        record = AgentLeg(kind="agent", rep_id=p["user_id"], provider_id=provider_id, state="answered")
        scoped_put(s, p, "call_legs", record.model_dump(), key=provider_id, now=ctx.clock())
    return provider_id


async def create(ctx, p, managed=None):
    """Dial the rep's registered client and return the new leg's carrier reference."""
    try:
        carrier = await asyncio.to_thread(ctx.carrier, workspace(p))
    except HTTPException as exc:
        if exc.detail == "TELNYX_NOT_CONFIGURED":
            refuse([blocked("TELNYX_NOT_CONFIGURED")])
        raise
    if carrier.instant_agent_legs:
        return {"agent_call_control_id": await asyncio.to_thread(_instant_leg, ctx, p)}
    from_profile = bool(managed and managed.enabled(workspace(p)))
    pool = managed.pool if from_profile else None
    if pool is not None:
        managed.require_ready(workspace(p))
        if pool.shared_connection_id and carrier.connection_id != pool.shared_connection_id:
            refuse([blocked("AUDIO_SETUP_MISSING")])
    sip, credential, phone = await asyncio.to_thread(setup, ctx, p, pool, managed=from_profile)
    await verify_once(ctx, carrier, credential, sip)
    leg_id, owner = uid(), uid()
    leg, created = await asyncio.to_thread(_claim, ctx, p, leg_id, owner)
    if not created:
        if await _still_live(ctx, carrier, leg["provider_id"]):
            return {"agent_call_control_id": leg["provider_id"]}
        # A leg from an earlier run of the app, or one the carrier no longer has, belongs to no audio client: reusing
        # it put calls on dead audio (live check, November 10). End it and dial a fresh one.
        await asyncio.to_thread(_retire, ctx, p, leg["id"])
        leg, created = await asyncio.to_thread(_claim, ctx, p, leg_id, owner)
        if not created:
            raise HTTPException(409, "The previous audio leg could not be ended; press Start calling again")
    target = f"sip:{sip}@sip.telnyx.com"
    if pool and pool.shared_connection_id:
        intent = await request(
            managed, "relay_pool_agent", {"agent_ref": leg_id, "number": phone, "destination": target}
        )
        if not intent.submitted:
            await asyncio.to_thread(_retire, ctx, p, leg_id)
            managed.wake.set()
            refuse([blocked(managed.connection_code(workspace(p)) or "AUDIO_SETUP_MISSING")])
    state = json.dumps({"agent_leg_id": leg_id, SCOPE_KEY: workspace(p)})
    try:
        receipt = await carrier.dial(
            {
                "to": target,
                "from": phone,
                **({} if ops.in_test_session(ctx, p) else {"time_limit_secs": ctx.settings.max_call_seconds}),
                "command_id": leg_id,
                "client_state": base64.b64encode(state.encode()).decode(),
            }
        )
    except Exception as exc:
        provider_id = await asyncio.to_thread(_failed, ctx, p, leg_id, owner)
        if provider_id:
            return {"agent_call_control_id": provider_id}
        raise HTTPException(
            503, "Agent submission is uncertain; await its signed receipt or reconcile before retrying"
        ) from exc
    await asyncio.to_thread(_completed, ctx, p, leg_id, owner, receipt["call_control_id"])
    ctx.memo.setdefault("agent_legs", set()).add(receipt["call_control_id"])
    return {"agent_call_control_id": receipt["call_control_id"]}


async def _still_live(ctx, carrier, provider_id):
    """Reusable only if this run of the app made the leg (its audio client is this one) and the carrier has it live."""
    if provider_id not in ctx.memo.get("agent_legs", set()):
        return False
    try:
        status = await carrier.reconcile(None, provider_id)
    except Exception as exc:
        log.warning("Could not read audio leg %s: %s", provider_id, type(exc).__name__)
        return False
    return (
        bool(status) and status.get("is_alive", status.get("state") not in {"hangup", "ended"}) is not False
    )


def _retire(ctx, p, leg_row_id):
    with ctx.db.transaction() as s:
        row = scoped_get(s, p, "call_legs", leg_row_id, lock=True)
        if row and AgentLeg.model_validate(row.data).state != "ended":
            update(row, state="ended", ended_at=ctx.clock().isoformat())


def on_event(s, workspace_id, payload, event_type):
    """Apply a carrier event to the rep's leg. False when the event is not for an agent leg."""
    provider_id = payload.get("call_control_id")
    if not isinstance(provider_id, str) or not provider_id:
        return False
    scope = scope_of(workspace_id)
    row = find(s, scope, provider_id=provider_id)
    if not row:
        try:
            context = json.loads(base64.b64decode(payload.get("client_state", "")))
            if not isinstance(context, dict) or context.get(SCOPE_KEY) != workspace_id:
                return False
            row = find(s, scope, leg_id=context.get("agent_leg_id"))
        except (ValueError, TypeError):
            return False
    if not row:
        return False
    leg = AgentLeg.model_validate(row.data)
    if leg.provider_id not in {None, provider_id}:
        return False
    state = leg.state
    if state != "ended":
        state = {"call.answered": "answered", "call.hangup": "ended"}.get(event_type, state)
    update(row, provider_id=provider_id, lease_owner=None, lease_until=None, state=state)
    if event_type == "call.hangup":
        user = scoped_get(s, scope, "users", leg.rep_id)
        presence = presence_of(user.data) if user else None
        if presence and presence.agent_call_control_id in {None, provider_id}:
            # The leg ends with every call. The rep stays in their chosen state until audio reconnects;
            # only Pause pauses.
            changed = presence.model_copy(update={"audio_ready": False, "telephony_ready": False})
            update(user, presence={**changed.model_dump(exclude_unset=False), "agent_call_control_id": None})
    return True
