"""Read-only carrier reconciliation before any retry of an uncertain effect."""

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select

from app.db import MODELS, doc, emit, uid, update
from app.service import dt, fail
from app.telephony.reducer import TERMINAL, owns_effect, reduce_state

CALL_COMMAND_KINDS = {"dial", "hangup", "action"}
# Conservative application window inside Telnyx's documented 60-second dedupe.
COMMAND_RETRY_SECONDS = 45


@dataclass(frozen=True)
class OperationPolicy:
    handler: str
    repeat: str
    uncertain_action: str
    proof: str


OPERATION_POLICIES = {
    "dial": OperationPolicy(
        "reconcile_command",
        "original_command_window",
        "verify_original_call",
        "matching_leg_or_terminal_receipt",
    ),
    "agent": OperationPolicy(
        "reconcile_agent",
        "original_command_window",
        "verify_original_agent",
        "matching_agent_or_terminal_receipt",
    ),
    **{
        name: OperationPolicy(
            "reconcile_command",
            "original_command_window",
            "verify_original_operation",
            "matching_operation_event_or_typed_acknowledgement",
        )
        for name in (
            "answer",
            "bridge",
            "transfer",
            "transcription_start",
            "playback_start",
            "gather",
            "speak",
            "send_dtmf",
        )
    },
    **{
        name: OperationPolicy(
            "reconcile_command", "safe_stop", "stop_verified_original_leg", "stopped_or_terminal_receipt"
        )
        for name in ("hangup", "transcription_stop", "playback_stop", "record_stop")
    },
    "number_configure": OperationPolicy(
        "reconcile_number_configuration",
        "verified_idempotent_configuration",
        "read_original_number_route",
        "exact_identity_connection_and_recording_state",
    ),
    "number_purchase": OperationPolicy(
        "reconcile_number_order", "never", "read_original_order", "original_partial_or_terminal_items"
    ),
    "dnc_screen": OperationPolicy(
        "compliance.screening.execute",
        "explicit_charge_acknowledgement",
        "review_original_lookup_liability",
        "exact_endpoint_source_and_attempt_response",
    ),
    "deferred_scripts": OperationPolicy(
        "preparation.batch_recovery.claim_recovery",
        "explicit_missing_objects",
        "read_original_batch",
        "original_input_digest_and_source_current_results",
    ),
    "inference": OperationPolicy(
        "run_jobs",
        "current_budget_and_live_sources",
        "retain_original_uncertain_usage",
        "matching_request_usage_receipt",
    ),
    "review": OperationPolicy(
        "run_jobs",
        "current_budget_and_live_sources",
        "retain_original_uncertain_usage",
        "matching_review_fingerprint_and_usage_receipt",
    ),
    "local_task": OperationPolicy(
        "edit_task", "identical_request_digest", "read_committed_edit_receipt", "committed_task_and_version"
    ),
    "local_outcome": OperationPolicy(
        "save_disposition",
        "identical_request_digest",
        "read_committed_edit_receipt",
        "committed_outcome_and_version",
    ),
}


def safe_stop(operation):
    policy = OPERATION_POLICIES.get(operation)
    return bool(policy and policy.repeat == "safe_stop")


def within_command_window(sent, now):
    return bool(sent and 0 <= (now - sent).total_seconds() < COMMAND_RETRY_SECONDS)


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
            s.refresh(command, with_for_update=True)
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
            s.refresh(command, with_for_update=True)
            if command.data["status"] != "reconciling" or not owns_effect(command.data, owner, self.clock()):
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
                retryable = within_command_window(sent, self.clock())
                # Stopping an existing leg is safe; starting another effect needs the original dedupe window.
                stop = safe_stop(action if kind == "action" else kind)
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
        commands, calls = MODELS["command_outbox"], MODELS["call_attempts"]

        def pending(s):
            return list(
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

        def due_calls(query):
            return query.where(
                calls.tenant_id == p["tenant_id"],
                calls.seller_id == p["seller_id"],
                calls.op_status.not_in(TERMINAL),
                calls.op_due_at <= self.clock(),
            )

        def configurations(s):
            return list(
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

        # Read first: claim expiry and the orphan-check stamp are the only writes,
        # so an idle seller's tick never takes the writer.
        with self.db.transaction(False) as s:
            if (
                s.scalar(due_calls(select(calls.id)).limit(1)) is None
                and s.scalar(self._expired_claims().limit(1)) is None
            ):
                return [doc(c) for c in pending(s)], [], [doc(c) for c in configurations(s)]
        with self.db.transaction() as s:
            self._expire_claims(s)
            commands_due = pending(s)
            active = list(
                s.scalars(
                    due_calls(select(calls))
                    .order_by(calls.op_due_at, calls.id)
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            )
            for call in active:
                update(call, orphan_checked_at=self.clock().isoformat())
            return (
                [doc(c) for c in commands_due],
                [doc(c) for c in active],
                [doc(c) for c in configurations(s)],
            )

    def _finish_orphan(self, p, snapshot):
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", snapshot["id"], p)
            s.refresh(call, with_for_update=True)
            if (
                call.version != snapshot["version"]
                or call.data["state"] != "ENDING"
                or call.data.get("provider_id") != snapshot.get("provider_id")
            ):
                return
            self.finish_call(s, p, call, "provider_terminal_reconciliation")

    def _expired_agents(self, p):
        """Keep an interrupted setup actionable without placing another carrier dial."""
        model = MODELS["call_legs"]
        due = (
            select(model)
            .where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.op_status.in_(["submitting", "uncertain"]),
                or_(model.op_due_at.is_(None), model.op_due_at <= self.clock()),
            )
            .order_by(model.op_due_at.asc().nullsfirst(), model.id)
            .limit(25)
        )

        def expired(leg):
            expiry = dt(leg.data.get("lease_until"))
            return leg.data.get("kind") == "agent" and not (expiry and expiry > self.clock())

        # Read first: only an expired agent setup needs the writer.
        with self.db.transaction(False) as s:
            if not any(expired(leg) for leg in s.scalars(due)):
                return []
        with self.db.transaction() as s:
            pending = s.scalars(due.with_for_update(skip_locked=True))
            known = []
            for leg in pending:
                if not expired(leg):
                    continue
                update(
                    leg,
                    state="uncertain",
                    lease_owner=None,
                    lease_until=None,
                    next_attempt_at=(self.clock() + timedelta(seconds=60)).isoformat(),
                    recovery_reason="Verify the original carrier agent reference; do not create another leg",
                )
                if leg.data.get("provider_id"):
                    known.append(doc(leg))
            return known

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
        for leg in await asyncio.to_thread(self._expired_agents, p):
            try:
                await self.reconcile_agent(p, leg["id"], leg["version"])
            except Exception as exc:
                logging.getLogger(__name__).warning("Agent reconciliation deferred: %s", type(exc).__name__)
        for call in calls:
            if call["state"] == "ENDING" and call.get("provider_id"):
                receipt = None
                try:
                    receipt = await self.carrier(p["tenant_id"]).reconcile(call["id"], call["provider_id"])
                except Exception as exc:
                    logging.getLogger(__name__).warning("Call status unavailable: %s", type(exc).__name__)
                verified = call_receipt(receipt, call["provider_id"], simulation=self.settings.simulation)
                if verified and not verified[1]:
                    await asyncio.to_thread(self._finish_orphan, p, call)
                    continue
            if call.get("started_at") and (self.clock() - dt(call["started_at"])).total_seconds() >= call.get(
                "max_call_seconds", self.settings.max_call_seconds
            ):
                await self.hangup(p, call["id"], recovery_expected=call)
        return {"reconciled": len(commands), "examined_calls": len(calls)}
