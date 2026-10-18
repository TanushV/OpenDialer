"""Provider receipt, retry and completion share one identity. Raw speech is ephemeral."""

import base64
import json
from datetime import timedelta

from sqlalchemy import select

from app.db import MODELS, doc, get, put, rows, uid, update
from app.privacy import RAW_TTL_SECONDS, event_metadata
from app.service import dt, fail
from app.transcripts import normalize_transcription


class WebhookIngress:
    async def accept_webhook(self, tenant_id, event, body_hash):
        if not isinstance(event, dict) or not all(event.get(k) for k in ("id", "event_type", "payload")):
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
                        "status": "received",
                        "received_at": self.clock().isoformat(),
                        "occurred_at": event.get("occurred_at"),
                        "raw_expires_at": (self.clock() + timedelta(seconds=RAW_TTL_SECONDS)).isoformat(),
                    },
                    key=event["id"],
                    now=self.clock(),
                )
            receipt_id, expires = receipt.id, dt(receipt.data["raw_expires_at"])
        if event["event_type"] == "call.transcription":
            self.raw.put("webhook:" + receipt_id, event["payload"], expires_at=expires)
        self.jobs.enqueue(
            "webhook", {"receipt_id": receipt_id}, "webhook:" + receipt_id, tenant_id=tenant_id, priority=200
        )
        return await self.process_webhook(tenant_id, receipt_id)

    async def process_webhook(self, tenant_id, receipt_id):
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
        try:
            if data["event_type"] == "call.transcription":
                payload = self.raw.get("webhook:" + receipt_id)
                if payload is None:
                    status = "expired_raw"
            result = {"received": True, "duplicate": False}
            if status != "expired_raw":
                result = await self._apply_webhook(tenant_id, data, payload)
                if result.get("unmatched"):
                    status = "unmatched"
        except Exception as exc:
            self._finish_webhook(tenant_id, receipt_id, owner, "retry", type(exc).__name__)
            raise
        self._finish_webhook(tenant_id, receipt_id, owner, status)
        if status in {"processed", "expired_raw"}:
            self.raw.discard("webhook:" + receipt_id)
        return {**result, "received": True, "status": status}

    def _finish_webhook(self, tenant_id, receipt_id, owner, status, error=None):
        with self.db.transaction() as s:
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

    async def _apply_webhook(self, tenant_id, receipt, payload):
        event_type, event_id = receipt["event_type"], receipt["event_id"]
        if self.agent_event(tenant_id, payload, event_type):
            return {"received": True, "agent_event": True}
        if self.inbound_event(tenant_id, payload, event_type):
            return {"received": True, "inbound_event": True}
        if event_type == "call.initiated" and payload.get("direction") == "incoming":
            return await self.inbound_callback(tenant_id, payload, event_id)
        with self.db.transaction(False) as s:
            call = next(
                (
                    c
                    for c in rows(s, "call_attempts", tenant_id)
                    if c.data.get("provider_id") == payload.get("call_control_id")
                ),
                None,
            )
            if not call and payload.get("client_state"):
                try:
                    context = json.loads(base64.b64decode(payload["client_state"]))
                    if isinstance(context, dict) and context.get("call_id"):
                        call = get(s, "call_attempts", context["call_id"], tenant_id)
                except (ValueError, TypeError):
                    pass
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
        result = self.event(p, event_id, normalized_type, call_id, event_payload)
        # Reducer deduplication does not imply transcript processing completed.
        if event_type == "call.transcription":
            for segment in normalize_transcription(payload, call_id, event_id):
                segment.pop("content_hash", None)
                segment.pop("call_id", None)
                await self.transcript(
                    p, call_id, segment, defer_coach=True, received_at=dt(receipt["received_at"])
                )
        if event_type == "call.hangup":
            self.jobs.enqueue(
                "postcall",
                {"call_id": call_id, "seller_id": p["seller_id"], "user_id": p["user_id"]},
                "postcall-webhook:" + event_id,
                tenant_id=tenant_id,
            )
        return result

    def queue_unfinished_webhooks(self):
        with self.db.transaction(False) as s:
            unfinished = [
                {**doc(r), "tenant_id": r.tenant_id}
                for r in rows(s, "webhook_inbox")
                if r.data.get("status") in {"received", "retry", "processing", "unmatched"}
            ]
        for receipt in unfinished:
            self.jobs.enqueue(
                "webhook",
                {"receipt_id": receipt["id"]},
                "webhook-recovery:" + receipt["id"] + ":" + str(int(self.clock().timestamp()) // 30),
                tenant_id=receipt["tenant_id"],
                priority=200,
                max_attempts=3,
            )
