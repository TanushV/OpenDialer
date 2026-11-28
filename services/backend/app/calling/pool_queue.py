"""Finish a local queue claim with the shared relay's caller ID."""

import asyncio

from fastapi import HTTPException
from sqlalchemy import select

from app import ops
from app.calling import test_calls
from app.calling.decide import Situation, require
from app.compliance.policy import authorize
from app.leads.csv_call_history import apply_history, read_history
from app.numbers.pool import cancel, request
from app.platform.db import ActiveClaim, doc, emit, put, update, workspace
from app.platform.errors import dt


def use_binding(service, p, provisional, selected, *, accepted):
    """Recheck the local claim and policy after the relay selects the actual number."""
    scope = workspace(p)
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        reservation = ops.required(service, s, "reservations", provisional["reservation_id"], p)
        saved = doc(reservation)
        claim = s.scalar(select(ActiveClaim).where(ActiveClaim.reservation_id == reservation.id))
        live = (
            claim is not None
            and claim.call_id is None
            and saved["status"] == "reserved"
            and saved["rep_id"] == p["user_id"]
            and dt(saved["expires_at"]) > service.clock()
        )
        if not live:
            return False
        lead = ops.call_recipient(service, s, p, saved)
        number = ops._by_key(service, s, p, "number_inventory", selected) if selected else None
        if (
            number is not None
            and claim.endpoint_id == lead.data["endpoint_id"]
            and _accept_number(service, s, p, lead, number, saved, accepted)
        ):
            update(reservation, number_id=number.id, pool_ref=reservation.id)
            claim.number_id = number.id
            provisional["number"] = doc(number)
            emit(s, scope, "reservation.created", reservation, service.clock())
            return True
        update(reservation, status="cancelled")
        s.delete(claim)
        return False


def _accept_number(service, s, p, lead, number, saved, accepted):
    if saved.get("test_contact"):
        return accepted and test_calls.authorized(service, s, p, lead, number, saved["dial_scope"]).allowed
    binding = ops._by_key(service, s, p, "lead_number_bindings", lead.id)
    if binding is None:
        put(
            s,
            "lead_number_bindings",
            workspace(p),
            p["seller_id"],
            {"lead_id": lead.id, "number_id": number.id},
            key=lead.id,
            now=service.clock(),
        )
    elif doc(binding)["number_id"] != number.id:
        update(binding, number_id=number.id)
    if (
        not accepted
        or not authorize(ops.context(service, s, {**p, **saved["dial_scope"]}, lead, number)).allowed
    ):
        return False
    decision = ops.required(service, s, "policy_decisions", saved["policy_decision_id"], p)
    allocation = doc(decision)["allocation"]
    if allocation["number_id"] != number.id:
        update(decision, allocation={**allocation, "number_id": number.id, "reason": "existing_binding"})
    return True


def hold_recent(service, p, provisional, called_at):
    """Another rep called this prospect in the last 24 hours: give the lead back until a day after that call."""
    from datetime import timedelta

    until = (dt(called_at) + timedelta(hours=24)).isoformat()
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        reservation = ops.required(service, s, "reservations", provisional["reservation_id"], p)
        claim = s.scalar(select(ActiveClaim).where(ActiveClaim.reservation_id == reservation.id))
        lead = ops.required(service, s, "leads", provisional["lead"]["id"], p)
        if until > (doc(lead).get("next_attempt_at") or ""):
            update(lead, next_attempt_at=until, recently_contacted_until=until, other_rep_called_at=called_at)
        update(reservation, status="cancelled")
        if claim is not None and claim.call_id is None:
            s.delete(claim)


async def reserve(service, p, campaign_id, names, callback):
    from app.numbers.pool import recent_elsewhere

    for _ in range(8):
        provisional = await asyncio.to_thread(ops.reserve, service, p, campaign_id, names, callback)
        if provisional.get("preferred_is_bound") is not None and not provisional["lead"].get("test_contact"):
            # Two reps with the same list never call one person twice in a day (plan P5, step 8).
            called_at = await recent_elsewhere(service.managed, provisional["lead"]["phone"])
            if called_at:
                await asyncio.to_thread(hold_recent, service, p, provisional, called_at)
                continue
        result = await read_history(service, p, provisional["lead"])
        if result is None or await asyncio.to_thread(apply_history, service, p, provisional, result):
            break
    else:
        require(
            Situation(queue="none"),
            {
                "code": "NO_ELIGIBLE_LEADS",
                "message": "Eight contacts in a row were held (HubSpot call history, or another rep called them today). Start calling again to check the next contacts.",
            },
        )
    return await assign(service, p, provisional)


async def assign(service, p, provisional):
    managed = service.managed
    preferred_is_bound = provisional.pop("preferred_is_bound", None)
    if preferred_is_bound is None:
        return provisional
    ref = provisional["reservation_id"]
    result = await request(
        managed,
        "relay_pool_reserve",
        {
            "request_ref": ref,
            "target": provisional["lead"]["phone"],
            "preferred": provisional["number"]["phone"],
            "preferred_is_bound": preferred_is_bound,
        },
    )
    try:
        accepted = await asyncio.to_thread(
            use_binding,
            service,
            p,
            provisional,
            result.phone,
            accepted=result.reason is None,
        )
    except (HTTPException, ValueError):
        await cancel(managed, ref)
        raise
    if accepted:
        return provisional
    await cancel(managed, ref)
    message = {
        "busy": "This prospect is already being called by another rep.",
        "capacity": "The shared caller number is paused or at its outbound limit. Check Numbers or call later.",
        "unavailable": "The shared number pool could not be reached. Try again when call events reconnect.",
    }.get(result.reason, "The shared caller number reservation expired. Start calling again.")
    fix = {"capacity": "numbers", "unavailable": "settings"}.get(result.reason)
    require(
        Situation(queue="none"),
        {"code": "NO_ELIGIBLE_LEADS", "message": message, **({"fix": fix} if fix else {})},
    )
