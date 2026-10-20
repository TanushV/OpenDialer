"""Operator actions over existing intents and jobs; never manufacture a replacement dial."""

import asyncio
import base64
import json
import re
from datetime import timedelta

from sqlalchemy import or_, select

from app.db import MODELS, Job, audit, doc, get, uid, update
from app.service import dt, fail
from app.telephony.recovery import CALL_COMMAND_KINDS, call_receipt
from app.telephony.reducer import owns_effect

UNFINISHED = {"pending", "sending", "uncertain", "reconciling", "review_required", "unconfirmed"}


def error_code(value, fallback):
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value) else fallback
    )


class RecoveryActions:
    def _job_visible(self, s, p, job):
        if not job or job.tenant_id != p["tenant_id"]:
            return False
        if job.payload.get("seller_id"):
            return job.payload["seller_id"] == p["seller_id"]
        if job.queue != "webhook":
            return False
        receipt = get(s, "webhook_inbox", job.payload.get("receipt_id"), p["tenant_id"])
        if not receipt:
            return False
        call = self._webhook_call(s, p["tenant_id"], receipt.data["payload"])
        if call:
            return call.seller_id == p["seller_id"]
        # Unmatched events are visible only when this seller owns the whole tenant scope.
        sellers = self.scoped(s, "sellers", p)
        model = MODELS["sellers"]
        return (
            len(sellers) == 1
            and len(list(s.scalars(select(model.id).where(model.tenant_id == p["tenant_id"]).limit(2)))) == 1
        )

    def _job_retry_reason(self, s, p, job):
        if job.queue not in {"webhook", "research", "scripts", "ml", "postcall", "live_coach"}:
            return "Use carrier command reconciliation; this queue cannot be blindly retried"
        if job.queue in {"postcall", "live_coach"}:
            call = self.required(s, "call_attempts", job.payload.get("call_id"), p)
            if not self.transcript_rows(s, p, call.id):
                return "Raw speech expired or is unavailable; retain the rundown and review your notes"
        if job.queue == "webhook":
            receipt = get(s, "webhook_inbox", job.payload.get("receipt_id"), p["tenant_id"])
            if receipt.data["status"] in {"processed", "expired_raw"}:
                return "This receipt has already completed or its raw content expired"
            if (
                receipt.data["event_type"] == "call.transcription"
                and self.raw.get("webhook:" + receipt.id) is None
            ):
                return "Raw speech expired or is unavailable; it cannot be reconstructed"
        return None

    def recovery_items(self, p):
        with self.db.transaction(False) as s:
            commands, legs = MODELS["command_outbox"], MODELS["call_legs"]
            pending = list(
                s.scalars(
                    select(commands)
                    .where(
                        commands.tenant_id == p["tenant_id"],
                        commands.seller_id == p["seller_id"],
                        commands.op_status.in_(UNFINISHED),
                        commands.op_kind.in_(CALL_COMMAND_KINDS),
                    )
                    .order_by(commands.created_at)
                    .limit(100)
                )
            )
            operations = list(
                s.scalars(
                    select(commands)
                    .where(
                        commands.tenant_id == p["tenant_id"],
                        commands.seller_id == p["seller_id"],
                        commands.op_status.in_(UNFINISHED),
                        or_(commands.op_kind.not_in(CALL_COMMAND_KINDS), commands.op_kind.is_(None)),
                    )
                    .order_by(commands.created_at)
                    .limit(100)
                )
            )
            agents = [
                r
                for r in s.scalars(
                    select(legs)
                    .where(
                        legs.tenant_id == p["tenant_id"],
                        legs.seller_id == p["seller_id"],
                        legs.op_status.in_(["uncertain", "submitting"]),
                    )
                    .limit(100)
                )
                if r.data.get("kind") == "agent"
            ]
            failed = list(
                s.scalars(
                    select(Job)
                    .where(Job.tenant_id == p["tenant_id"], Job.status.in_(["dead", "reconcile"]))
                    .order_by(Job.available_at)
                    .limit(200)
                )
            )
            items = []
            for command in pending:
                data = command.data
                call = get(
                    s,
                    "followup_tasks" if data.get("inbound") else "call_attempts",
                    data["call_id"],
                    p["tenant_id"],
                )
                target = (
                    data.get("target")
                    or data.get("provider_id")
                    or (call.data.get("provider_id") if call and call.seller_id == p["seller_id"] else None)
                )
                items.append(
                    {
                        "id": command.id,
                        "version": command.version,
                        "kind": data["kind"],
                        "action": data.get("action"),
                        "call_id": data["call_id"],
                        "status": data["status"],
                        "provider_id": target,
                        "reason": data.get("recovery_reason")
                        or error_code(data.get("error"), "Awaiting processing or operator review"),
                        "can_cancel": data["status"] == "pending"
                        and not data.get("sent_at")
                        and not data.get("submitted_at"),
                        "can_end": bool(target),
                        "can_confirm_not_placed": data["status"] == "unconfirmed" and not target,
                    }
                )
            jobs = []
            for job in failed:
                if self._job_visible(s, p, job):
                    reason = self._job_retry_reason(s, p, job)
                    jobs.append(
                        {
                            "id": job.id,
                            "queue": job.queue,
                            "status": job.status,
                            "attempts": job.attempts,
                            "reason": reason or error_code(job.error, "Processing attempts exhausted"),
                            "can_retry": reason is None,
                        }
                    )
            return {
                "commands": items,
                "operations": [
                    {
                        "id": r.id,
                        "version": r.version,
                        "kind": r.data.get("kind", "unknown"),
                        "status": r.data["status"],
                        "provider_id": r.data.get("provider_id"),
                        "number_id": r.data.get("number_id"),
                        "connection_id": r.data.get("connection_id"),
                        "can_reconcile": r.data.get("kind") == "number_configure"
                        and r.data["status"] in {"sending", "reconciling", "uncertain", "review_required"},
                        "reason": r.data.get("recovery_reason")
                        or {
                            "number_configure": "Verify the original number route; callback testing is separate.",
                            "script_batch": "Review the original deferred preparation request; do not submit another batch.",
                            "purchase": "Use prior order recovery in Numbers to verify the original carrier order.",
                        }.get(
                            r.data.get("kind"),
                            "Unsupported effect requires operator review; it cannot be retried as a call.",
                        ),
                    }
                    for r in operations
                ],
                "agents": [
                    {
                        "id": r.id,
                        "version": r.version,
                        "status": r.data["state"],
                        "provider_id": r.data.get("provider_id"),
                        "reason": r.data.get("recovery_reason") or "Agent setup has no confirmed result",
                    }
                    for r in agents
                ],
                "jobs": jobs,
                "limited": len(pending) == 100
                or len(operations) == 100
                or len(agents) == 100
                or len(failed) == 200,
            }

    def _confirm_not_placed(self, s, p, call, reason):
        """Operator attestation, from the carrier account, that an unconfirmed dial never rang."""
        if call.data.get("provider_id") or call.data["state"] != "DIAL_UNCONFIRMED":
            fail(409, "Only an unconfirmed dial without a carrier reference can be confirmed as not placed")
        for key in (call.id, call.id + ":hangup"):
            command = self._by_key(s, p, "command_outbox", key)
            if command and command.data.get("status") in UNFINISHED:
                update(
                    command, status="cancelled", recovery_reason="Operator confirmed not placed: " + reason
                )
        update(call, not_placed_reason=reason, not_placed_by=p["user_id"])
        self.finish_call(s, p, call, "operator_confirmed_not_placed")

    def _recovery_action(self, p, identity, expected_version, action, reason=None):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            command = self.required(s, "command_outbox", identity, p)
            if command.version != expected_version:
                fail(409, "Recovery item changed; refresh before continuing")
            data = command.data
            if data.get("kind") not in CALL_COMMAND_KINDS:
                fail(409, "Use this operation's own recovery workflow; it is not a call command")
            if data["status"] not in UNFINISHED:
                fail(409, "This command is already resolved")
            if data.get("lease_until") and dt(data["lease_until"]) > self.clock():
                fail(409, "A worker still owns this command; await its result")
            call = self.required(
                s, "followup_tasks" if data.get("inbound") else "call_attempts", data["call_id"], p
            )
            if action == "confirm_not_placed":
                if data["status"] != "unconfirmed" or data.get("inbound"):
                    fail(409, "Carrier evidence may still arrive; wait for the dial to become unconfirmed")
                self._confirm_not_placed(s, p, call, reason)
            elif action == "cancel_unsent":
                if data["status"] != "pending" or data.get("sent_at") or data.get("submitted_at"):
                    fail(409, "A possibly submitted effect cannot be canceled as unsent")
                update(command, status="cancelled", recovery_reason="Operator canceled unsent intent")
                if data["kind"] == "dial":
                    self.finish_call(s, p, call, "unsent_intent_cancelled")
            else:
                target = data.get("target") or data.get("provider_id") or call.data.get("provider_id")
                if not target:
                    fail(409, "Verify the original carrier reference before requesting a stop")
                if data.get("inbound"):
                    self._inbound_command(
                        s, p, call, "hangup", target=target, suffix="recovery-stop:" + identity
                    )
                else:
                    self.control_intent(
                        s, p, call.id, "hangup", {}, target=target, suffix="recovery-stop:" + identity
                    )
                    if target == call.data.get("provider_id") and call.data["state"] not in {
                        "ENDED",
                        "FINALIZED",
                    }:
                        update(call, state="ENDING")
                update(command, recovery_reason="Stop requested; awaiting terminal carrier evidence")
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "recovery." + action,
                identity,
                self.clock(),
                {"reason": reason} if reason else None,
            )
            return doc(command)

    async def recovery_action(self, p, identity, expected_version, action, provider_id=None, reason=None):
        def kind():
            with self.db.transaction(False) as s:
                return self.required(s, "command_outbox", identity, p).data.get("kind")

        if await asyncio.to_thread(kind) == "number_configure":
            if action != "reconcile":
                fail(409, "Number configuration supports verification only, not call controls")
            return await self.reconcile_number_configuration(
                p, identity, expected_version=expected_version, provider_id=provider_id
            )
        if action == "reconcile":
            return await self.reconcile_command(
                p, identity, provider_id=provider_id, expected_version=expected_version
            )
        result = await asyncio.to_thread(self._recovery_action, p, identity, expected_version, action, reason)
        if action == "end_call":
            await self.dispatch_controls(p)
        return result

    def retry_job(self, p, identity, expected_attempts):
        with self.db.transaction() as s:
            query = select(Job).where(Job.id == identity)
            job = s.scalar(query.with_for_update() if not self.db.sqlite else query)
            if not self._job_visible(s, p, job):
                fail(404, "Job not found")
            if job.status not in {"dead", "reconcile"} or job.attempts != expected_attempts:
                fail(409, "Job changed; refresh before retrying")
            reason = self._job_retry_reason(s, p, job)
            if reason:
                fail(409, reason)
            if job.queue == "webhook":
                receipt = get(s, "webhook_inbox", job.payload["receipt_id"], p["tenant_id"])
                update(receipt, status="retry", lease_owner=None, lease_until=None)
            else:
                owner = self.required(s, "users", job.payload.get("user_id"), p)
                if owner.data.get("disabled"):
                    fail(409, "Original job owner is disabled; assign fresh work to an active rep")
            job.status, job.max_attempts, job.available_at = "queued", job.attempts + 1, self.clock()
            job.lease_owner, job.lease_expires_at = None, None
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "recovery.job_retry",
                identity,
                self.clock(),
                {"prior_attempts": expected_attempts},
            )
            return {"id": identity, "status": job.status, "attempts": job.attempts}

    async def reconcile_agent(self, p, identity, expected_version, provider_id=None):
        def claim():
            with self.db.transaction() as s:
                self.db.authorize_stream(s, p)
                leg = self.required(s, "call_legs", identity, p)
                s.refresh(leg, with_for_update=True)
                if leg.version != expected_version or leg.data.get("kind") != "agent":
                    fail(409, "Agent setup changed; refresh before continuing")
                if leg.data.get("lease_until") and dt(leg.data["lease_until"]) > self.clock():
                    fail(409, "An agent request is still running")
                target = leg.data.get("provider_id") or provider_id
                if not target or provider_id and target != provider_id:
                    fail(409, "Use the original carrier agent reference")
                owner = uid()
                known = bool(leg.data.get("provider_id"))
                update(leg, lease_owner=owner, lease_until=(self.clock() + timedelta(seconds=30)).isoformat())
                return target, owner, known

        target, owner, known = await asyncio.to_thread(claim)
        receipt = None
        try:
            receipt = await self.carrier(p["tenant_id"]).reconcile(identity, target)
        except Exception:
            receipt = None
        verified = call_receipt(receipt, target, simulation=self.settings.simulation)
        if verified and not known:
            try:
                context = json.loads(base64.b64decode(receipt.get("client_state", "")))
            except (ValueError, TypeError):
                context = None
            if (
                not isinstance(context, dict)
                or context.get("agent_leg_id") != identity
                or context.get("tenant_id") != p["tenant_id"]
            ):
                verified = None

        def finish():
            with self.db.transaction() as s:
                leg = self.required(s, "call_legs", identity, p)
                s.refresh(leg, with_for_update=True)
                if not owns_effect(leg.data, owner, self.clock()):
                    return {"status": "unchanged"}
                update(
                    leg,
                    lease_owner=None,
                    lease_until=None,
                    recovery_reason=None
                    if verified
                    else "No matching carrier agent receipt; do not create another leg",
                )
                if verified:
                    update(leg, provider_id=target)
                    if not verified[1]:
                        self.agent_event(
                            p["tenant_id"], {"call_control_id": target}, "call.hangup", session=s
                        )
                    elif leg.data["state"] in {"submitting", "uncertain"}:
                        update(leg, state="ringing")
                audit(
                    s,
                    p["tenant_id"],
                    p["seller_id"],
                    p["user_id"],
                    "recovery.agent_reconciled",
                    identity,
                    self.clock(),
                )
                return {"id": identity, "status": leg.data["state"]}

        return await asyncio.to_thread(finish)
