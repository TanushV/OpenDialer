"""Read HubSpot activity only for the CSV contact reaching the dialing queue."""

import asyncio
from datetime import timedelta

from sqlalchemy import select

from app import ops
from app.compliance.policy import authorize
from app.leads.hubspot_sync import _stamp
from app.platform.db import MODELS, ActiveClaim, doc, emit, update, workspace
from app.platform.errors import dt, fail

FRESH = timedelta(minutes=5)
INCOMPLETE = {"QUEUED", "RINGING", "CONNECTING", "IN_PROGRESS", "FAILED", "CANCELED", "CANCELLED"}


async def read_history(service, p, lead):
    if p.get("test_only") or lead.get("source") != "csv":
        return None
    now = service.clock()
    seen = lead.get("hubspot_call_history") or {}
    if seen.get("phone") == lead["phone"] and (at := dt(seen.get("checked_at"))) and now - at < FRESH:
        return None
    result = {"phone": lead["phone"], "checked_at": now.isoformat()}
    try:
        async with asyncio.timeout(2.5):
            receipt = await ops.csv_outbound_calls(service, p, lead)
        if not receipt:
            return None
        if receipt["status"] != "checked":
            return {**result, "status": receipt["status"]}
        calls = {}
        for call in receipt["calls"]:
            properties = call.get("properties") or {}
            status = str(properties.get("hs_call_status") or "").upper()
            if properties.get("hs_call_direction") != "OUTBOUND" or status in INCOMPLETE:
                continue
            at = _stamp(properties.get("hs_timestamp"))
            if at and at.tzinfo and at <= now + timedelta(minutes=5):
                calls[call["id"]] = (at, status, properties.get("opendialer_call_id"))
    except (TimeoutError, RuntimeError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return {**result, "status": "unavailable"}
    latest = max(calls.values(), key=lambda call: call[0], default=None)
    return {
        **result,
        "status": "checked",
        "attempts": len(calls),
        "last_at": latest[0].isoformat() if latest else None,
        "last_status": latest[1] or None if latest else None,
        "call_ids": [call[2] for call in calls.values()],
    }


def apply_history(service, p, provisional, result):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        reservation = ops.required(service, s, "reservations", provisional["reservation_id"], p)
        claim = s.scalar(select(ActiveClaim).where(ActiveClaim.reservation_id == reservation.id))
        if not claim or claim.call_id or reservation.data["status"] != "reserved":
            fail(409, "Calling was paused while checking HubSpot.")
        lead = ops.required(service, s, "leads", claim.lead_id, p)
        value = doc(lead)
        number = ops.required(service, s, "number_inventory", claim.number_id, p)
        if value.get("phone") != result["phone"] or value.get("source") != "csv":
            update(reservation, status="cancelled")
            s.delete(claim)
            return False
        ids = [identity for identity in result.pop("call_ids", []) if identity]
        if result["status"] == "checked":
            model = MODELS["call_attempts"]
            recorded = {
                row.id
                for row in ops.lead_records(service, s, p, "call_attempts", lead.id, model.id.in_(ids))
                if (doc(row).get("started_at") or "") >= (value.get("imported_at") or "")
            }
            result["prior_attempts"] = result["attempts"] - len(recorded)
        else:
            # Keep a prior verified attempt count through a temporary provider error.
            previous = prior_history(value)
            if result["status"] == "unavailable" and previous.get("phone") == result["phone"]:
                result = {**previous, **result}
        update(lead, hubspot_call_history=result)
        emit(s, workspace(p), "lead.updated", lead, service.clock())
        provisional["lead"] = doc(lead)
        if authorize(ops.context(service, s, {**p, **reservation.data["dial_scope"]}, lead, number)).allowed:
            return True
        update(reservation, status="cancelled")
        s.delete(claim)
        return False


def prior_history(lead):
    history = lead.get("hubspot_call_history") or {}
    return history if lead.get("source") == "csv" and history.get("phone") == lead.get("phone") else {}
