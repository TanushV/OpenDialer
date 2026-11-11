"""Finish a local queue claim with the shared relay's caller ID."""

import asyncio

from fastapi import HTTPException
from sqlalchemy import select

from app import ops
from app.calling.decide import Situation, require
from app.compliance.policy import authorize
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
        lead = ops.required(service, s, "leads", saved["lead_id"], p)
        number = ops._by_key(service, s, p, "number_inventory", selected) if selected else None
        if number is not None and claim.endpoint_id == lead.data["endpoint_id"]:
            binding = ops._by_key(service, s, p, "lead_number_bindings", lead.id)
            if binding is None:
                put(
                    s,
                    "lead_number_bindings",
                    scope,
                    p["seller_id"],
                    {"lead_id": lead.id, "number_id": number.id},
                    key=lead.id,
                    now=service.clock(),
                )
            elif doc(binding)["number_id"] != number.id:
                update(binding, number_id=number.id)
            if accepted:
                decision = authorize(ops.context(service, s, {**p, **saved["dial_scope"]}, lead, number))
                if decision.allowed:
                    decision_row = ops.required(
                        service, s, "policy_decisions", saved["policy_decision_id"], p
                    )
                    allocation = doc(decision_row)["allocation"]
                    if allocation["number_id"] != number.id:
                        update(
                            decision_row,
                            allocation={**allocation, "number_id": number.id, "reason": "existing_binding"},
                        )
                    update(reservation, number_id=number.id, pool_ref=reservation.id)
                    claim.number_id = number.id
                    provisional["number"] = doc(number)
                    emit(s, scope, "reservation.created", reservation, service.clock())
                    return True
        update(reservation, status="cancelled")
        s.delete(claim)
        return False


async def reserve(service, p, campaign_id, names, callback):
    managed = service.managed
    provisional = await asyncio.to_thread(ops.reserve, service, p, campaign_id, names, callback)
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
    require(Situation(queue="none"), {"code": "NO_ELIGIBLE_LEADS", "message": message})
