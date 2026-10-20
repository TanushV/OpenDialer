"""Provider receipt, retry and completion share one identity. Raw speech is ephemeral."""

import asyncio
import base64
import json
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import MODELS, Job, emit, get, put, uid, update
from app.privacy import RAW_TTL_SECONDS, event_metadata
from app.service import dt, fail
from app.telephony.reducer import TERMINAL
from app.transcripts import normalize_transcription

# Final segments of the rep's last utterance may trail the prospect hangup by a few seconds.
AGENT_SPEECH_GRACE = 10


class WebhookIngress:
    async def accept_webhook(self, tenant_id, event, body_hash, *, verified=False):
        # Receipt I/O is synchronous; it must never occupy the control event-loop thread.
        try:
            return await asyncio.to_thread(self._receive_webhook, tenant_id, event, body_hash, verified)
        except IntegrityError:
            # A concurrent identical receipt may win the unique insert. Reload and compare its hash.
            return await asyncio.to_thread(self._receive_webhook, tenant_id, event, body_hash, verified)

    def _receive_webhook(self, tenant_id, event, body_hash, verified=False):
        if (
            not isinstance(event, dict)
            or not all(isinstance(event.get(k), str) and event[k] for k in ("id", "event_type"))
            or not isinstance(event.get("payload"), dict)
            or not event["payload"]
        ):
            fail(422, "Invalid provider event")
        with self.db.transaction() as s:
            receipt = s.scalar(
                select(MODELS["webhook_inbox"]).where(
                    MODELS["webhook_inbox"].tenant_id == tenant_id,
                    MODELS["webhook_inbox"].external_key == event["id"],
                )
            )
            if receipt and receipt.data.get("hash") != body_hash:
                fail(409, "Provider event ID collision")
            if receipt and receipt.data.get("status") in {"processed", "expired_raw"}:
                return {"received": True, "duplicate": True, "status": receipt.data["status"]}
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
                        "received_at": self.clock().isoformat(),
                        "occurred_at": event.get("occurred_at"),
                        "raw_expires_at": (self.clock() + timedelta(seconds=RAW_TTL_SECONDS)).isoformat(),
                    },
                    key=event["id"],
                    now=self.clock(),
                )
            receipt_id, expires = receipt.id, dt(receipt.data["raw_expires_at"])
            scope = None
            if event["event_type"] == "call.transcription":
                call, _ = self._transcription_call(s, tenant_id, event["payload"])
                if call:
                    p = {"tenant_id": tenant_id, "seller_id": call.seller_id}
                    scope = tenant_id + ":" + call.id
                    if self.speech_revoked(s, p, call):
                        update(
                            receipt,
                            status="processed",
                            ignored_reason="permission_revoked",
                            processed_at=self.clock().isoformat(),
                        )
                        return {"received": True, "receipt_id": receipt_id, "status": "processed"}
            self.jobs.enqueue_in(
                s,
                "webhook",
                {"receipt_id": receipt_id},
                "webhook:" + receipt_id,
                tenant_id=tenant_id,
                priority=200,
            )
            if event["event_type"] == "call.transcription":
                # The worker must see the short-lived payload as soon as its job commits.
                self.raw.put("webhook:" + receipt_id, event["payload"], expires_at=expires, scope=scope)
        return {"received": True, "receipt_id": receipt_id, "status": receipt.data["status"]}

    async def process_webhook(self, tenant_id, receipt_id):
        return await asyncio.to_thread(self._process_webhook, tenant_id, receipt_id)

    def _process_webhook(self, tenant_id, receipt_id):
        owner = uid()
        with self.db.transaction() as s:
            query = select(MODELS["webhook_inbox"]).where(
                MODELS["webhook_inbox"].id == receipt_id, MODELS["webhook_inbox"].tenant_id == tenant_id
            )
            if not self.db.sqlite:
                query = query.with_for_update()
            receipt = s.scalar(query)
            if not receipt:
                fail(404, "Receipt not found")
            if receipt.data["status"] in {"processed", "expired_raw"}:
                return {"received": True, "duplicate": True, "status": receipt.data["status"]}
            if receipt.data.get("lease_until") and dt(receipt.data["lease_until"]) > self.clock():
                return {"received": True, "duplicate": True, "status": "processing"}
            update(
                receipt,
                status="processing",
                lease_owner=owner,
                lease_until=(self.clock() + timedelta(seconds=30)).isoformat(),
            )
            data = dict(receipt.data)
        payload, status = data["payload"], "processed"
        raw_writes = []
        try:
            if data["event_type"] == "call.transcription":
                payload = self.raw.get("webhook:" + receipt_id)
                if payload is None:
                    status = "expired_raw"
            result = {"received": True, "duplicate": False}
            with self.db.transaction() as s:
                receipt = s.scalar(query.with_for_update() if not self.db.sqlite else query)
                if receipt.data.get("lease_owner") != owner:
                    return {"received": True, "status": "processing"}
                if status != "expired_raw":
                    result = self._apply_webhook_in(s, tenant_id, data, payload, raw_writes)
                    if result.get("unmatched"):
                        status = "unmatched"
                else:
                    call, _ = self._transcription_call(s, tenant_id, data["payload"])
                    if call and self.speech_revoked(
                        s, {"tenant_id": tenant_id, "seller_id": call.seller_id}, call
                    ):
                        status = "processed"
                        update(receipt, ignored_reason="permission_revoked")
                    elif call:
                        update(
                            call,
                            raw_expires_at=max(call.data.get("raw_expires_at", ""), data["raw_expires_at"]),
                            unprocessed_speech_events=call.data.get("unprocessed_speech_events", 0) + 1,
                            analysis_incomplete=True,
                        )
                        emit(s, tenant_id, "call.updated", call, self.clock())
                self._finish_webhook(tenant_id, receipt_id, owner, status, session=s)
        except Exception as exc:
            self._finish_webhook(tenant_id, receipt_id, owner, "retry", type(exc).__name__)
            raise
        self._publish_transcripts(raw_writes)
        if status in {"processed", "expired_raw"}:
            self.raw.discard("webhook:" + receipt_id)
        return {**result, "received": True, "status": status}

    def _finish_webhook(self, tenant_id, receipt_id, owner, status, error=None, *, session=None):
        with self.db.transaction(session=session) as s:
            receipt = get(s, "webhook_inbox", receipt_id, tenant_id)
            if receipt and receipt.data.get("lease_owner") == owner:
                update(
                    receipt,
                    status=status,
                    lease_owner=None,
                    lease_until=None,
                    error=error,
                    processed_at=self.clock().isoformat() if status == "processed" else None,
                )

    def _webhook_call(self, s, tenant_id, payload):
        provider_id = payload.get("call_control_id")
        if not isinstance(provider_id, str) or not provider_id:
            return None
        model = MODELS["call_attempts"]
        matches = list(
            s.scalars(
                select(model)
                .where(model.tenant_id == tenant_id, model.op_provider_id == provider_id)
                .limit(2)
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

    def _transcription_call(self, s, tenant_id, payload):
        """Return (call, leg). The persistent agent leg names its call in transcription client state."""
        call = self._webhook_call(s, tenant_id, payload)
        if call:
            return call, "prospect"
        provider_id = payload.get("call_control_id")
        try:
            context = json.loads(base64.b64decode(payload.get("client_state", "")))
        except (ValueError, TypeError):
            context = None
        if (
            isinstance(provider_id, str)
            and provider_id
            and isinstance(context, dict)
            and context.get("tenant_id") == tenant_id
            and isinstance(context.get("transcript_call_id"), str)
        ):
            call = get(s, "call_attempts", context["transcript_call_id"], tenant_id)
            if call and call.data.get("agent_provider_id") == provider_id:
                return call, "agent"
        return None, None

    def _agent_transcript_in(self, s, call, receipt, payload, raw_writes):
        """Rep speech from the agent leg: no reducer event, no prospect-leg identity binding."""
        received = dt(receipt["received_at"])
        ended = dt(call.data.get("ended_at"))
        if (
            call.data["state"] in TERMINAL
            and ended
            and (received - ended).total_seconds() > AGENT_SPEECH_GRACE
        ):
            # The agent leg outlives the call; later speech belongs to no prospect conversation.
            return {"received": True, "ignored": "speech_after_call"}
        p = {"tenant_id": call.tenant_id, "seller_id": call.seller_id, "user_id": call.data["rep_id"]}
        p["role"] = "rep"
        leg = "agent" if call.data.get("transcription_layout") == "leg_inbound" else None
        for segment in normalize_transcription(payload, call.id, receipt["event_id"], leg=leg):
            segment.pop("content_hash", None)
            segment.pop("call_id", None)
            self._transcript_in(s, p, call.id, segment, received, raw_writes)
        return {"received": True, "agent_transcript": True}

    def _apply_webhook_in(self, s, tenant_id, receipt, payload, raw_writes):
        event_type, event_id = receipt["event_type"], receipt["event_id"]
        leg = None
        if event_type == "call.transcription":
            call, leg = self._transcription_call(s, tenant_id, payload)
            if leg == "agent":
                return self._agent_transcript_in(s, call, receipt, payload, raw_writes)
        if self.agent_event(tenant_id, payload, event_type, session=s):
            return {"received": True, "agent_event": True}
        if self.inbound_event(tenant_id, payload, event_type, session=s, receipt=receipt):
            return {"received": True, "inbound_event": True}
        if event_type == "call.initiated" and payload.get("direction") == "incoming":
            return self._receive_inbound(tenant_id, payload, event_id, session=s, receipt=receipt)
        call = self._webhook_call(s, tenant_id, payload)
        if not call:
            return {"received": True, "unmatched": True}
        p = {
            "tenant_id": call.tenant_id,
            "seller_id": call.seller_id,
            "user_id": call.data["rep_id"],
            "role": "rep",
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
        result = self.event(p, event_id, normalized_type, call_id, event_payload, session=s)
        # Reducer deduplication does not imply transcript processing completed.
        if event_type == "call.transcription":
            layout = call.data.get("transcription_layout") == "leg_inbound"
            for segment in normalize_transcription(payload, call_id, event_id, leg=leg if layout else None):
                segment.pop("content_hash", None)
                segment.pop("call_id", None)
                self._transcript_in(s, p, call_id, segment, dt(receipt["received_at"]), raw_writes)
        if event_type == "call.hangup":
            # The call row is locked by the event write above, as the finalize enqueue requires.
            self.enqueue_finalize(s, p, call, "webhook")
        return result

    def queue_unfinished_webhooks(self):
        inbox = MODELS["webhook_inbox"]

        def oldest_unfinished(query):
            return (
                query.where(inbox.op_status.in_(["received", "retry", "processing", "unmatched"]))
                .order_by(inbox.created_at)
                .limit(200)
            )

        # Read first: receipts whose job is already queued or running need no write.
        with self.db.transaction(False) as s:
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
        with self.db.transaction() as s:
            unfinished = list(s.scalars(oldest_unfinished(select(inbox))))
            for receipt in unfinished:
                queued = self.jobs.enqueue_in(
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
                    update(
                        receipt, status="review_required", error=job.error or "Processing attempts exhausted"
                    )
                elif job.status == "completed":
                    # Older releases could finish a job before its event matched a call.
                    job.status = "queued" if job.attempts < job.max_attempts else "dead"
                    job.available_at = self.clock()
