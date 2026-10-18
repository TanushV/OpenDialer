"""Read-only carrier reconciliation before any retry of an uncertain effect."""

import asyncio
import base64
import json
import logging
from datetime import timedelta

from sqlalchemy import or_, select

from app.db import MODELS, doc, emit, uid, update
from app.service import dt, fail
from app.telephony.reducer import TERMINAL, reduce_state

CALL_COMMAND_KINDS = {"dial", "hangup", "action"}


def call_receipt(receipt, provider_id, *, simulation=False):
    if not isinstance(receipt, dict):
        return None
    identity = receipt.get("call_control_id")
    if not isinstance(identity, str) or not identity or provider_id and identity != provider_id:
        return None
    alive = receipt.get("is_alive")
    if type(alive) is not bool:
        if not simulation or "is_alive" in receipt:
            return None
        state = receipt.get("state")
        if state not in {"ringing", "answered", "hangup", "ended", "completed"}:
            return None
        alive = state in {"ringing", "answered"}
    return identity, alive


class CommandRecovery:
    def _claim_reconciliation(self, p, command_id, *, provider_id=None, expected_version=None):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            command = self.required(s, "command_outbox", command_id, p)
            if command.data.get("kind") not in CALL_COMMAND_KINDS:
                fail(409, "Use this operation's own recovery workflow; it is not a call command")
            if expected_version is not None and command.version != expected_version:
                fail(409, "Recovery item changed; refresh before continuing")
            lease = dt(command.data.get("lease_until"))
            if lease and lease > self.clock():
                return None
            if command.data["status"] in {"completed", "cancelled", "failed"}:
                return None
            call = self.required(
                s,
                "followup_tasks" if command.data.get("inbound") else "call_attempts",
                command.data["call_id"],
                p,
            )
            known = (
                command.data.get("target") or command.data.get("provider_id") or call.data.get("provider_id")
            )
            if provider_id and known and provider_id != known:
                fail(409, "Cannot replace an established carrier reference")
            owner = uid()
            update(
                command,
                status="reconciling",
                lease_owner=owner,
                lease_until=(self.clock() + timedelta(seconds=30)).isoformat(),
            )
            return doc(command), doc(call), known or provider_id, owner, bool(known)

    def _complete_reconciliation(self, p, claimed, receipt, error=None):
        command_data, call_data, target, owner, previously_known = claimed
        with self.db.transaction() as s:
            command = self.required(s, "command_outbox", command_data["id"], p)
            if command.data.get("lease_owner") != owner:
                return doc(command)
            call = self.required(
                s, "followup_tasks" if command.data.get("inbound") else "call_attempts", call_data["id"], p
            )
            verified = call_receipt(receipt, target, simulation=self.settings.simulation)
            if verified and not previously_known and not self.settings.simulation:
                try:
                    context = json.loads(base64.b64decode(receipt.get("client_state", "")))
                except (ValueError, TypeError):
                    context = None
                if (
                    not isinstance(context, dict)
                    or context.get("call_id") != call.id
                    or context.get("tenant_id") != p["tenant_id"]
                ):
                    verified = None
            changes = dict(
                lease_owner=None,
                lease_until=None,
                last_reconciled_at=self.clock().isoformat(),
                next_attempt_at=(self.clock() + timedelta(seconds=60)).isoformat(),
            )
            if not verified:
                update(
                    command,
                    **changes,
                    status="review_required",
                    error=error,
                    recovery_reason="No matching carrier status receipt. Await a signed event or supply the original call reference for verification.",
                )
                return doc(command)
            identity, alive = verified
            kind, action = command.data["kind"], command.data.get("action")
            if kind == "dial":
                self.bind_dial_receipt(s, p, call, identity)
                update(command, **changes, status="completed", recovery_reason=None)
                update(
                    call,
                    state=reduce_state(
                        call.data["state"], "call.ringing" if self.settings.simulation else "call.initiated"
                    ),
                )
                if alive:
                    lead = self.required(s, "leads", call.data["lead_id"], p)
                    if self.suppressed(s, p, lead.data["phone"]):
                        update(call, state="ENDING")
                        self.control_intent(s, p, call.id, "hangup", {}, suffix="recovered-suppressed-call")
            elif not alive:
                update(
                    command,
                    **changes,
                    status="completed" if kind == "hangup" or action == "hangup" else "cancelled",
                    recovery_reason="Carrier confirms the target leg ended; no control replay",
                )
            else:
                sent = dt(command.data.get("sent_at"))
                retryable = bool(sent and 0 <= (self.clock() - sent).total_seconds() < 45)
                # Stopping an existing leg is safe; starting another effect needs the original dedupe window.
                stop = kind == "hangup" or action in {
                    "hangup",
                    "transcription_stop",
                    "playback_stop",
                    "record_stop",
                }
                update(
                    command,
                    **changes,
                    status="pending" if retryable or stop else "review_required",
                    recovery_reason="same_command_within_dedupe_window"
                    if retryable
                    else "Stopping the verified existing leg"
                    if stop
                    else "Call is alive; this does not prove the earlier control executed. Do not replay it outside the deduplication window.",
                )
            if not alive:
                if command.data.get("inbound"):
                    self.inbound_event(
                        p["tenant_id"], {"call_control_id": identity}, "call.hangup", session=s
                    )
                elif identity == call.data.get("provider_id"):
                    self.finish_call(s, p, call, "provider_terminal_reconciliation")
                else:
                    self.agent_event(p["tenant_id"], {"call_control_id": identity}, "call.hangup", session=s)
            if not command.data.get("inbound"):
                emit(s, p["tenant_id"], "call.updated", call, self.clock())
            return doc(command)

    async def reconcile_command(self, p, command_id, *, provider_id=None, expected_version=None):
        claimed = await asyncio.to_thread(
            self._claim_reconciliation,
            p,
            command_id,
            provider_id=provider_id,
            expected_version=expected_version,
        )
        if not claimed:
            return {"status": "unchanged"}
        receipt, error = None, None
        try:
            receipt = await self.carrier(p["tenant_id"]).reconcile(claimed[0]["call_id"], claimed[2])
        except Exception as exc:
            error = type(exc).__name__
        return await asyncio.to_thread(self._complete_reconciliation, p, claimed, receipt, error)

    def _recovery_snapshot(self, p):
        with self.db.transaction() as s:
            self._expire_claims(s)
            commands = MODELS["command_outbox"]
            pending = list(
                s.scalars(
                    select(commands)
                    .where(
                        commands.tenant_id == p["tenant_id"],
                        commands.seller_id == p["seller_id"],
                        commands.op_kind.in_(CALL_COMMAND_KINDS),
                        commands.op_status.in_(
                            ["pending", "sending", "uncertain", "reconciling", "review_required"]
                        ),
                        or_(commands.op_due_at.is_(None), commands.op_due_at <= self.clock()),
                    )
                    .order_by(commands.op_due_at.asc().nullsfirst(), commands.created_at)
                    .limit(100)
                )
            )
            calls = MODELS["call_attempts"]
            active = list(
                s.scalars(
                    select(calls)
                    .where(
                        calls.tenant_id == p["tenant_id"],
                        calls.seller_id == p["seller_id"],
                        calls.op_status.not_in(TERMINAL),
                    )
                    .order_by(calls.created_at)
                    .limit(100)
                )
            )
            configurations = list(
                s.scalars(
                    select(commands)
                    .where(
                        commands.tenant_id == p["tenant_id"],
                        commands.seller_id == p["seller_id"],
                        commands.op_kind == "number_configure",
                        commands.op_status.in_(["sending", "uncertain", "reconciling", "review_required"]),
                        or_(commands.op_due_at.is_(None), commands.op_due_at <= self.clock()),
                    )
                    .order_by(commands.op_due_at.asc().nullsfirst(), commands.created_at)
                    .limit(25)
                )
            )
            return [doc(c) for c in pending], [doc(c) for c in active], [doc(c) for c in configurations]

    async def recover(self, p):
        await asyncio.to_thread(self.jobs.recover_expired)
        await self.reconcile_inbound(p)
        commands, calls, configurations = await asyncio.to_thread(self._recovery_snapshot, p)
        limit = asyncio.Semaphore(4)

        async def recover_one(command):
            async with limit:
                if command["status"] == "pending":
                    if command["kind"] == "dial":
                        await self.dispatch_call(p, command["call_id"])
                    elif command["kind"] == "hangup":
                        await self.hangup(p, command["call_id"])
                else:
                    result = await self.reconcile_command(p, command["id"])
                    if command["kind"] == "hangup" and result.get("status") == "pending":
                        await self.hangup(p, command["call_id"])

        await asyncio.gather(*(recover_one(command) for command in commands))
        await self.dispatch_controls(p)

        async def check_configuration(command):
            async with limit:
                try:
                    await self.reconcile_number_configuration(
                        p, command["id"], expected_version=command["version"]
                    )
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "Number reconciliation deferred: %s", type(exc).__name__
                    )

        await asyncio.gather(*(check_configuration(command) for command in configurations))
        for call in calls:
            if call["state"] == "ENDING" and call.get("provider_id"):
                try:
                    receipt = await self.carrier(p["tenant_id"]).reconcile(call["id"], call["provider_id"])
                except Exception as exc:
                    logging.getLogger(__name__).warning("Call status unavailable: %s", type(exc).__name__)
                    continue
                verified = call_receipt(receipt, call["provider_id"], simulation=self.settings.simulation)
                if verified and not verified[1]:

                    def finish(identity=call["id"]):
                        with self.db.transaction() as s:
                            self.finish_call(
                                s,
                                p,
                                self.required(s, "call_attempts", identity, p),
                                "provider_terminal_reconciliation",
                            )

                    await asyncio.to_thread(finish)
                    continue
            if (self.clock() - dt(call["started_at"])).total_seconds() > call.get(
                "max_call_seconds", self.settings.max_call_seconds
            ):
                await self.hangup(p, call["id"])
        return {"reconciled": len(commands), "examined_calls": len(calls)}
