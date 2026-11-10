"""Read-only carrier reconciliation before any retry of an uncertain effect."""

import asyncio
import base64
import json
import logging
from datetime import timedelta

from sqlalchemy import or_, select

from app import ops
from app.calling import audio_leg
from app.calling.records import CallView
from app.calling.recovery import next_step, pollable
from app.calling.state import TERMINAL, moved
from app.platform.db import MODELS, ActiveClaim, doc, emit, uid, update
from app.platform.leases import owns_effect
from app.service import dt, fail

CALL_COMMAND_KINDS = {"dial", "hangup", "action"}
# Conservative application window inside Telnyx's documented 60-second dedupe.
COMMAND_RETRY_SECONDS = 45
# Default 30-second ring plus webhook delivery retries: after this, absent evidence needs a person.
DIAL_EVIDENCE_SECONDS = 120
UNACKNOWLEDGED = {"DIALING", "RECONCILING", "ENDING"}


# Stopping an existing leg is safe to repeat; starting another effect needs the dedupe window.
STOP_OPERATIONS = {"hangup", "transcription_stop"}


def safe_stop(operation):
    return operation in STOP_OPERATIONS


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


def _claim_reconciliation(service, p, command_id, *, provider_id=None, expected_version=None):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        command = ops.required(service, s, "command_outbox", command_id, p)
        s.refresh(command, with_for_update=True)
        if command.data.get("kind") not in CALL_COMMAND_KINDS:
            fail(409, "Use this operation's own recovery workflow; it is not a call command")
        if expected_version is not None and command.version != expected_version:
            fail(409, "Recovery item changed; refresh before continuing")
        lease = dt(command.data.get("lease_until"))
        if lease and lease > service.clock():
            return None
        if command.data["status"] in {"completed", "cancelled", "failed"}:
            return None
        call = ops.required(
            service,
            s,
            "followup_tasks" if command.data.get("inbound") else "call_attempts",
            command.data["call_id"],
            p,
        )
        known = command.data.get("target") or command.data.get("provider_id") or call.data.get("provider_id")
        if provider_id and known and provider_id != known:
            fail(409, "Cannot replace an established carrier reference")
        owner = uid()
        update(
            command,
            status="reconciling",
            lease_owner=owner,
            lease_until=(service.clock() + timedelta(seconds=30)).isoformat(),
        )
        return doc(command), doc(call), known or provider_id, owner, bool(known)


def _complete_reconciliation(service, p, claimed, receipt, error=None):
    command_data, call_data, target, owner, previously_known = claimed
    with service.db.transaction() as s:
        command = ops.required(service, s, "command_outbox", command_data["id"], p)
        s.refresh(command, with_for_update=True)
        if command.data["status"] != "reconciling" or not owns_effect(command.data, owner, service.clock()):
            return doc(command)
        call = ops.required(
            service,
            s,
            "followup_tasks" if command.data.get("inbound") else "call_attempts",
            call_data["id"],
            p,
        )
        verified = call_receipt(receipt, target, simulation=service.settings.simulation)
        if verified and not previously_known and not service.settings.simulation:
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
            last_reconciled_at=service.clock().isoformat(),
            next_attempt_at=(service.clock() + timedelta(seconds=60)).isoformat(),
        )
        if not verified:
            update(
                command,
                **changes,
                status="review_required",
                error=error,
                recovery_reason="No matching carrier status receipt. Await a signed event or supply the original call reference for verification.",
            )
            if not target and not command.data.get("inbound"):
                if call.data["state"] != "DIAL_UNCONFIRMED" and ops.dial_evidence_expired(service, call):
                    # Refreshes the call under its row lock; a concurrent pass may win the transition.
                    ops.dial_unconfirmed_in(service, s, p, call)
                if call.data["state"] == "DIAL_UNCONFIRMED":
                    # This command holds its own lease, so dial_unconfirmed_in skips it; settle it here
                    # whichever pass (this command's or its sibling dial's) moved the call.
                    update(command, status="unconfirmed", recovery_reason=call.data.get("recovery_reason"))
            return doc(command)
        identity, alive = verified
        kind, action = command.data["kind"], command.data.get("action")
        if kind == "dial":
            ops.bind_dial_receipt(service, s, p, call, identity)
            update(command, **changes, status="completed", recovery_reason=None)
            update(
                call,
                **moved(
                    call.data,
                    "call.ringing" if service.settings.simulation else "call.initiated",
                    service.clock(),
                ),
            )
            if alive:
                lead = ops.required(service, s, "leads", call.data["lead_id"], p)
                if ops.suppressed(service, s, p, lead.data["phone"]):
                    update(call, **moved(call.data, "hangup.requested", service.clock()))
                    ops.control_intent(
                        service, s, p, call.id, "hangup", {}, suffix="recovered-suppressed-call"
                    )
        elif not alive:
            update(
                command,
                **changes,
                status="completed" if kind == "hangup" or action == "hangup" else "cancelled",
                recovery_reason="Carrier confirms the target leg ended; no control replay",
            )
        else:
            sent = dt(command.data.get("sent_at"))
            retryable = within_command_window(sent, service.clock())
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
                ops.inbound_event(
                    service, p["tenant_id"], {"call_control_id": identity}, "call.hangup", session=s
                )
            elif identity == call.data.get("provider_id"):
                ops.finish_terminal_in(service, s, p, call, receipt)
            else:
                audio_leg.on_event(s, p["tenant_id"], {"call_control_id": identity}, "call.hangup")
        if not command.data.get("inbound"):
            emit(s, p["tenant_id"], "call.updated", call, service.clock())
        return doc(command)


async def reconcile_command(service, p, command_id, *, provider_id=None, expected_version=None):
    claimed = await asyncio.to_thread(
        ops._claim_reconciliation,
        service,
        p,
        command_id,
        provider_id=provider_id,
        expected_version=expected_version,
    )
    if not claimed:
        return {"status": "unchanged"}
    receipt, error = None, None
    try:
        receipt = await ops.carrier(service, p["tenant_id"]).reconcile(claimed[0]["call_id"], claimed[2])
    except Exception as exc:
        logging.getLogger(__name__).warning("reconcile_command handled a failure: %s", type(exc).__name__)
        error = type(exc).__name__
    return await asyncio.to_thread(ops._complete_reconciliation, service, p, claimed, receipt, error)


def _recovery_snapshot(service, p):
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
                    or_(commands.op_due_at.is_(None), commands.op_due_at <= service.clock()),
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
            calls.op_due_at <= service.clock(),
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
                    or_(commands.op_due_at.is_(None), commands.op_due_at <= service.clock()),
                )
                .order_by(commands.op_due_at.asc().nullsfirst(), commands.created_at)
                .limit(25)
            )
        )

    # Read first: claim expiry and the orphan-check stamp are the only writes,
    # so an idle seller's tick never takes the writer.
    with service.db.transaction(False) as s:
        if (
            s.scalar(due_calls(select(calls.id)).limit(1)) is None
            and s.scalar(ops._expired_claims(service).limit(1)) is None
        ):
            return [doc(c) for c in pending(s)], [], [doc(c) for c in configurations(s)]
    with service.db.transaction() as s:
        ops._expire_claims(service, s)
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
            update(call, orphan_checked_at=service.clock().isoformat())
        return (
            [doc(c) for c in commands_due],
            [doc(c) for c in active],
            [doc(c) for c in configurations(s)],
        )


def dial_evidence_expired(service, call):
    sent = dt(call.data.get("submitted_at"))
    return bool(
        sent
        and not call.data.get("provider_id")
        and call.data["state"] in UNACKNOWLEDGED
        and (service.clock() - sent).total_seconds() >= DIAL_EVIDENCE_SECONDS
    )


def dial_unconfirmed_in(service, s, p, call):
    """Telnyx cannot be asked about a dial by command ID. Without a reference or a signed
    event, only the operator can settle it: free the rep, keep the prospect held."""
    s.refresh(call, with_for_update=True)
    if not ops.dial_evidence_expired(service, call):
        return
    reason = (
        "No carrier reference or signed event arrived for this dial. Check the carrier account: "
        "attach the call reference if it was placed, or confirm it was not placed."
    )
    commands = MODELS["command_outbox"]
    for key in (call.id, call.id + ":hangup"):
        command = ops._by_key(service, s, p, "command_outbox", key)
        if command is not None:
            # A sibling command another worker is reconciling is skipped, not overwritten; that
            # worker sees DIAL_UNCONFIRMED after this commits and settles its own command.
            command = s.scalar(
                select(commands)
                .where(commands.id == command.id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)  # Lock, then read its current version.
            )
        lease = dt(command.data.get("lease_until")) if command else None
        if (
            command
            and command.data.get("status") in {"uncertain", "reconciling", "review_required", "sending"}
            and not (lease and lease > service.clock())
        ):
            update(command, status="unconfirmed", lease_owner=None, lease_until=None, recovery_reason=reason)
    update(
        call,
        **moved(call.data, "dial.unconfirmed", service.clock()),
        review_required=True,
        recovery_reason=reason,
    )
    for claim in s.scalars(select(ActiveClaim).where(ActiveClaim.call_id == call.id)):
        claim.rep_id = call.id  # Unique per call; the endpoint and lead stay claimed.
    emit(s, p["tenant_id"], "call.updated", call, service.clock(), {"state": "DIAL_UNCONFIRMED"})


def finish_terminal_in(service, s, p, call, receipt):
    """Carrier evidence says the prospect leg is gone; an expired status read needs review."""
    if not receipt.get("status_expired"):
        return ops.finish_call(service, s, p, call, "provider_terminal_reconciliation")
    ops.finish_call(service, s, p, call, "carrier_status_expired")
    update(
        call,
        review_required=True,
        recovery_reason="Carrier status is kept only ten minutes after a call ends; "
        "confirm the end time and duration from the carrier statement",
    )


async def _recover_call(service, p, call):
    """Carry out `calling.recovery.next_step` for one due call (R4.6, R4.8, G3)."""
    step = next_step(call, service.clock())
    if step is None:
        return
    if pollable(CallView.model_validate(call)):
        alive = await ops._poll_call(service, p, call)
        if alive is False or step == "poll":
            return  # Ended by the status read, or alive and inside its limit.
        if step == "call.ended" and alive:
            step = "hangup.requested"  # Still live after the hang-up was sent: send it again.
    if step == "hangup.requested":
        await ops.hangup(service, p, call["id"], recovery_expected=call)
    elif step == "dial.unconfirmed":
        await asyncio.to_thread(ops._settle_unacknowledged, service, p, call)
    elif step == "call.ended":
        await asyncio.to_thread(ops._end_unsettled, service, p, call)


async def _poll_call(service, p, call):
    """True or False from a carrier status read; None when the carrier cannot answer. A dead leg is finished."""
    receipt = None
    try:
        receipt = await ops.carrier(service, p["tenant_id"]).reconcile(call["id"], call["provider_id"])
    except Exception as exc:
        logging.getLogger(__name__).warning("Call status unavailable: %s", type(exc).__name__)
    verified = call_receipt(receipt, call["provider_id"], simulation=service.settings.simulation)
    if not verified:
        return None
    if not verified[1]:
        await asyncio.to_thread(ops._finish_orphan, service, p, call, receipt)
    return verified[1]


def _finish_orphan(service, p, snapshot, receipt):
    with service.db.transaction() as s:
        call = ops.required(service, s, "call_attempts", snapshot["id"], p)
        s.refresh(call, with_for_update=True)
        if (
            call.version != snapshot["version"]
            or call.data["state"] in TERMINAL
            or call.data.get("provider_id") != snapshot.get("provider_id")
        ):
            return
        ops.finish_terminal_in(service, s, p, call, receipt)


def _settle_unacknowledged(service, p, snapshot):
    """A dial that was never sent ends; one sent without any carrier answer becomes unconfirmed."""
    with service.db.transaction() as s:
        call = ops.required(service, s, "call_attempts", snapshot["id"], p)
        s.refresh(call, with_for_update=True)
        if call.version != snapshot["version"] or call.data.get("provider_id"):
            return
        if call.data.get("submitted_at"):
            ops.dial_unconfirmed_in(service, s, p, call)
            return
        # Nothing was sent: the dial claim sets submitted_at before any request leaves.
        command = ops._by_key(service, s, p, "command_outbox", call.id)
        if command and command.data.get("status") == "pending" and not command.data.get("sent_at"):
            update(command, status="cancelled", reason="DIAL_NOT_SENT")
        ops.finish_call(service, s, p, call, "dial_not_sent")


def _end_unsettled(service, p, snapshot):
    """The state ran out and the carrier cannot settle it: end it here and list it under Needs attention."""
    with service.db.transaction() as s:
        call = ops.required(service, s, "call_attempts", snapshot["id"], p)
        s.refresh(call, with_for_update=True)
        if call.version != snapshot["version"] or call.data["state"] in TERMINAL:
            return
        reason = call.data.get("recovery_reason") or (
            "The carrier never confirmed how this call ended. Check the carrier account for its duration."
        )
        ops.finish_call(service, s, p, call, "recovery_timeout")
        update(call, review_required=True, recovery_reason=reason)


def _expired_agents(service, p):
    """Keep an interrupted setup actionable without placing another carrier dial."""
    model = MODELS["call_legs"]
    due = (
        select(model)
        .where(
            model.tenant_id == p["tenant_id"],
            model.seller_id == p["seller_id"],
            model.op_status.in_(["submitting", "uncertain"]),
            or_(model.op_due_at.is_(None), model.op_due_at <= service.clock()),
        )
        .order_by(model.op_due_at.asc().nullsfirst(), model.id)
        .limit(25)
    )

    def expired(leg):
        expiry = dt(leg.data.get("lease_until"))
        return leg.data.get("kind") == "agent" and not (expiry and expiry > service.clock())

    # Read first: only an expired agent setup needs the writer.
    with service.db.transaction(False) as s:
        if not any(expired(leg) for leg in s.scalars(due)):
            return []
    with service.db.transaction() as s:
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
                next_attempt_at=(service.clock() + timedelta(seconds=60)).isoformat(),
                recovery_reason="Verify the original carrier agent reference; do not create another leg",
            )
            if leg.data.get("provider_id"):
                known.append(doc(leg))
        return known


async def recover(service, p):
    await asyncio.to_thread(service.jobs.recover_expired)
    await ops.reconcile_inbound(service, p)
    commands, calls, configurations = await asyncio.to_thread(ops._recovery_snapshot, service, p)
    limit = asyncio.Semaphore(4)

    async def recover_one(command):
        async with limit:
            if command["status"] == "pending":
                if command["kind"] == "dial":
                    await ops.dispatch_call(service, p, command["call_id"])
                elif command["kind"] == "hangup":
                    await ops.hangup(service, p, command["call_id"])
            else:
                result = await ops.reconcile_command(service, p, command["id"])
                if command["kind"] == "hangup" and result.get("status") == "pending":
                    await ops.hangup(service, p, command["call_id"])

    await asyncio.gather(*(recover_one(command) for command in commands))
    await ops.dispatch_controls(service, p)

    async def check_configuration(command):
        async with limit:
            try:
                await ops.reconcile_number_configuration(
                    service, p, command["id"], expected_version=command["version"]
                )
            except Exception as exc:
                logging.getLogger(__name__).warning("Number reconciliation deferred: %s", type(exc).__name__)

    await asyncio.gather(*(check_configuration(command) for command in configurations))
    for leg in await asyncio.to_thread(ops._expired_agents, service, p):
        try:
            await ops.reconcile_agent(service, p, leg["id"], leg["version"])
        except Exception as exc:
            logging.getLogger(__name__).warning("Agent reconciliation deferred: %s", type(exc).__name__)
    for call in calls:
        await ops._recover_call(service, p, call)
    return {"reconciled": len(commands), "examined_calls": len(calls)}
