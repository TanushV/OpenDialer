"""Provider receipt, retry and completion share one identity.

Carrier speech events are never processed: live speech comes only from the app's own audio tracks (call insights
plan C1). A stray `call.transcription` receipt is closed at once without its text.
"""

import asyncio
import base64
import json
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import ops
from app.calling import audio_leg
from app.platform.db import MODELS, Job, get, put, uid, update
from app.privacy import RAW_TTL_SECONDS, event_metadata
from app.service import dt, fail


def _carries_our_state(tenant_id, payload):
    """True when a carrier event carries client state this app attached to a leg it placed."""
    try:
        context = json.loads(base64.b64decode(payload.get("client_state") or ""))
    except (ValueError, TypeError):
        return False
    return isinstance(context, dict) and context.get("tenant_id") == tenant_id


async def accept_webhook(service, tenant_id, event, body_hash, *, verified=False, received_at=None):
    # Receipt I/O is synchronous; it must never occupy the control event-loop thread.
    try:
        return await asyncio.to_thread(
            ops._receive_webhook, service, tenant_id, event, body_hash, verified, received_at
        )
    except IntegrityError:
        # A concurrent identical receipt may win the unique insert. Reload and compare its hash.
        return await asyncio.to_thread(
            ops._receive_webhook, service, tenant_id, event, body_hash, verified, received_at
        )


def _receive_webhook(service, tenant_id, event, body_hash, verified=False, received_at=None):
    received_at = min(received_at or service.clock(), service.clock())
    if (
        not isinstance(event, dict)
        or not all(isinstance(event.get(k), str) and event[k] for k in ("id", "event_type"))
        or not isinstance(event.get("payload"), dict)
        or not event["payload"]
    ):
        fail(422, "Invalid provider event")
    with service.db.transaction() as s:
        receipt = s.scalar(
            select(MODELS["webhook_inbox"]).where(
                MODELS["webhook_inbox"].tenant_id == tenant_id,
                MODELS["webhook_inbox"].external_key == event["id"],
            )
        )
        if receipt and receipt.data.get("hash") != body_hash:
            fail(409, "Provider event ID collision")
        if receipt and receipt.data.get("status") in {"processed", "expired_raw", "ignored"}:
            return {
                "received": True,
                "duplicate": True,
                "status": receipt.data["status"],
                "receipt_id": receipt.id,
            }
        if not receipt:
            receipt = put(
                s,
                "webhook_inbox",
                tenant_id,
                "",
                {
                    "event_id": event["id"],
                    "event_type": event["event_type"],
                    "payload": event_metadata(event["payload"]),
                    "hash": body_hash,
                    "signature_verified": verified,
                    "status": "received",
                    "received_at": received_at.isoformat(),
                    "occurred_at": event.get("occurred_at"),
                    "callback_call_id": event.get("callback_call_id") if verified else None,
                    "relay_callback": bool(verified and event.get("relay_callback") is True),
                    "raw_expires_at": (received_at + timedelta(seconds=RAW_TTL_SECONDS)).isoformat(),
                },
                key=event["id"],
                now=service.clock(),
            )
        receipt_id = receipt.id
        if event["event_type"] == "call.transcription":
            # Carrier transcription was removed (plan C1): close the receipt, never keep or apply its text.
            update(
                receipt,
                status="processed",
                ignored_reason="carrier_transcription_removed",
                processed_at=service.clock().isoformat(),
            )
            return {"received": True, "receipt_id": receipt_id, "status": "processed"}
        service.jobs.enqueue_in(
            s,
            "webhook",
            {"receipt_id": receipt_id},
            "webhook:" + receipt_id,
            tenant_id=tenant_id,
            priority=200,
        )
    return {"received": True, "receipt_id": receipt_id, "status": receipt.data["status"]}


async def process_webhook(service, tenant_id, receipt_id):
    def callback_root():
        with service.db.transaction(False) as s:
            row = get(s, "webhook_inbox", receipt_id, tenant_id)
            if row and row.data["event_type"] == "call.initiated" and row.data.get("relay_callback"):
                if row.data["status"] not in {"processed", "expired_raw", "ignored"}:
                    return row.data["payload"]["call_control_id"]
        return None

    shared_pool = service.managed.pool and service.managed.pool.shared_connection_id
    root = await asyncio.to_thread(callback_root) if shared_pool else None
    claim = await service.managed.claim_callback(tenant_id, root) if root else None
    result = await asyncio.to_thread(ops._process_webhook, service, tenant_id, receipt_id, claim)
    if saved := result.pop("save_outcome", None):
        await asyncio.to_thread(ops.auto_disposition, service, *saved)
    return result


def _process_webhook(service, tenant_id, receipt_id, callback_claim=None):
    owner = uid()
    with service.db.transaction() as s:
        query = select(MODELS["webhook_inbox"]).where(
            MODELS["webhook_inbox"].id == receipt_id, MODELS["webhook_inbox"].tenant_id == tenant_id
        )
        receipt = s.scalar(query)
        if not receipt:
            fail(404, "Receipt not found")
        if receipt.data["status"] in {"processed", "expired_raw", "ignored"}:
            return {"received": True, "duplicate": True, "status": receipt.data["status"]}
        if receipt.data.get("lease_until") and dt(receipt.data["lease_until"]) > service.clock():
            return {"received": True, "duplicate": True, "status": "processing"}
        update(
            receipt,
            status="processing",
            lease_owner=owner,
            lease_until=(service.clock() + timedelta(seconds=30)).isoformat(),
        )
        data = dict(receipt.data)
        if callback_claim:
            data["callback_claim"] = callback_claim
    payload, status = data["payload"], "processed"
    raw_writes = []
    try:
        result = {"received": True, "duplicate": False}
        with service.db.transaction() as s:
            receipt = s.scalar(query)
            if receipt.data.get("lease_owner") != owner:
                return {"received": True, "status": "processing"}
            if data["event_type"] == "call.transcription":
                # Saved by a version that still had carrier transcription: nothing to apply.
                update(receipt, ignored_reason="carrier_transcription_removed")
            else:
                result = ops._apply_webhook_in(service, s, tenant_id, data, payload, raw_writes)
                if result.get("unmatched"):
                    status = "unmatched"
                elif result.get("ignored") == "not_ours":
                    status = "ignored"
            ops._finish_webhook(service, tenant_id, receipt_id, owner, status, session=s)
    except Exception as exc:
        ops._finish_webhook(service, tenant_id, receipt_id, owner, "retry", type(exc).__name__)
        raise
    ops._publish_transcripts(service, raw_writes)
    return {**result, "received": True, "status": status}


def _finish_webhook(service, tenant_id, receipt_id, owner, status, error=None, *, session=None):
    with service.db.transaction(session=session) as s:
        receipt = get(s, "webhook_inbox", receipt_id, tenant_id)
        if receipt and receipt.data.get("lease_owner") == owner:
            update(
                receipt,
                status=status,
                lease_owner=None,
                lease_until=None,
                error=error,
                processed_at=service.clock().isoformat() if status == "processed" else None,
            )


def _webhook_call(service, s, tenant_id, payload):
    provider_id = payload.get("call_control_id")
    if not isinstance(provider_id, str) or not provider_id:
        return None
    model = MODELS["call_attempts"]
    matches = list(
        s.scalars(
            select(model).where(model.tenant_id == tenant_id, model.op_provider_id == provider_id).limit(2)
        )
    )
    if len(matches) > 1:
        fail(409, "Call provider identity is ambiguous; review original call records")
    call = next(iter(matches), None)
    if not call and payload.get("client_state"):
        try:
            context = json.loads(base64.b64decode(payload["client_state"]))
            if (
                isinstance(context, dict)
                and context.get("call_id")
                and context.get("tenant_id", tenant_id) == tenant_id
            ):
                call = get(s, "call_attempts", context["call_id"], tenant_id)
                if call and call.data.get("provider_id") not in {None, provider_id}:
                    return None
        except (ValueError, TypeError):
            pass
    return call


def _apply_webhook_in(service, s, tenant_id, receipt, payload, raw_writes):
    event_type, event_id = receipt["event_type"], receipt["event_id"]
    if audio_leg.on_event(s, tenant_id, payload, event_type):
        return {"received": True, "agent_event": True}
    if ops.inbound_event(service, tenant_id, payload, event_type, session=s, receipt=receipt):
        return {"received": True, "inbound_event": True}
    if event_type == "call.initiated" and payload.get("direction") == "incoming":
        return ops._receive_inbound(service, tenant_id, payload, event_id, session=s, receipt=receipt)
    call = ops._webhook_call(service, s, tenant_id, payload)
    if not call:
        if not _carries_our_state(tenant_id, payload):
            # Not a leg this app placed, such as the rep's own SIP leg: nothing to retry.
            logging.getLogger(__name__).info("Ignored %s for a leg OpenDialer did not place", event_type)
            return {"received": True, "ignored": "not_ours"}
        # Ours, but the call row may not be written yet: retry.
        return {"received": True, "unmatched": True}
    p = {
        "tenant_id": call.tenant_id,
        "seller_id": call.seller_id,
        "user_id": call.data["rep_id"],
    }
    call_id = call.id
    normalized_type = (
        event_type + ":" + payload["result"]
        if "machine" in event_type and payload.get("result")
        else event_type
    )
    event_payload = {
        **payload,
        "occurred_at": receipt.get("occurred_at") or receipt["received_at"],
        "timing_source": "provider" if receipt.get("occurred_at") else "receipt",
    }
    result = ops.event(service, p, event_id, normalized_type, call_id, event_payload, session=s)
    if event_type == "call.hangup":
        # The call row is locked by the event write above, as the finalize enqueue requires.
        ops.enqueue_aftercall(service, s, p, call)
    return result


def queue_unfinished_webhooks(service):
    inbox = MODELS["webhook_inbox"]

    def oldest_unfinished(query):
        return (
            query.where(inbox.op_status.in_(["received", "retry", "processing", "unmatched"]))
            .order_by(inbox.created_at)
            .limit(200)
        )

    # Read first: receipts whose job is already queued or running need no write.
    with service.db.transaction(False) as s:
        receipts = {
            (tenant, "webhook:" + identity)
            for tenant, identity in s.execute(oldest_unfinished(select(inbox.tenant_id, inbox.id)))
        }
        if not receipts:
            return
        active = {
            (tenant, key)
            for tenant, key in s.execute(
                select(Job.tenant_id, Job.key).where(
                    Job.tenant_id.in_({tenant for tenant, _ in receipts}),
                    Job.key.in_([key for _, key in receipts]),
                    Job.status.in_(["queued", "running"]),
                )
            )
        }
        if receipts <= active:
            return
    with service.db.transaction() as s:
        unfinished = list(s.scalars(oldest_unfinished(select(inbox))))
        for receipt in unfinished:
            queued = service.jobs.enqueue_in(
                s,
                "webhook",
                {"receipt_id": receipt.id},
                "webhook:" + receipt.id,
                tenant_id=receipt.tenant_id,
                priority=200,
                max_attempts=3,
            )
            job = s.get(Job, queued["id"])
            if job.status == "dead":
                update(receipt, status="review_required", error=job.error or "Processing attempts exhausted")


def archive_legacy_audio_webhooks(db, now):
    """Settle old unmatched SIP-leg jobs using saved evidence, without replaying provider events."""
    inbox, legs = MODELS["webhook_inbox"], MODELS["call_legs"]
    provider = inbox.data["payload"]["call_control_id"].as_string()
    session = inbox.data["payload"]["call_session_id"].as_string()
    with db.transaction() as s:
        jobs = list(
            s.scalars(
                select(Job).where(Job.queue == "webhook", Job.status == "dead", Job.error == "RuntimeError")
            )
        )
        if not jobs:
            return 0
        agent_sessions = set(
            s.execute(
                select(inbox.tenant_id, session)
                .join(legs, (legs.tenant_id == inbox.tenant_id) & (legs.op_provider_id == provider))
                .where(
                    legs.data["kind"].as_string() == "agent",
                    inbox.data["signature_verified"].as_boolean().is_(True),
                    session.is_not(None),
                )
            )
        )
        archived = 0
        for job in jobs:
            receipt = get(s, "webhook_inbox", job.payload.get("receipt_id"), job.tenant_id)
            if not receipt:
                continue
            data = receipt.data
            payload = data.get("payload") or {}
            target = payload.get("call_control_id")
            received = dt(data.get("received_at"))
            if (
                data.get("status") != "review_required"
                or not data.get("signature_verified")
                or data.get("event_type") not in {"call.initiated", "call.answered", "call.hangup"}
                or not received
                or received > now - timedelta(days=7)
                or payload.get("direction") == "incoming"
                or payload.get("client_state")
                or data.get("relay_callback")
                or data.get("callback_call_id")
                or not target
                or (job.tenant_id, payload.get("call_session_id")) not in agent_sessions
            ):
                continue
            matched = False
            for table in ("call_attempts", "call_legs", "followup_tasks"):
                model = MODELS[table]
                if s.scalar(
                    select(model.id)
                    .where(model.tenant_id == job.tenant_id, model.op_provider_id == target)
                    .limit(1)
                ):
                    matched = True
                    break
            if matched:
                continue
            update(
                receipt,
                status="ignored",
                ignored_reason="legacy_rep_audio_companion",
                processed_at=now.isoformat(),
            )
            job.status = "archived"
            archived += 1
        return archived
