"""Placing and ending a call (R4.5): durable dial and hang-up records, the claim on each carrier command, and read-only
reconcile. A dial or hang-up is recorded before the carrier is asked, so a crash at any step never dials twice and never
loses the call (`tests/test_crash_steps.py`).
"""

import asyncio
import base64
import json
import logging
from datetime import timedelta

import httpx
from sqlalchemy import select

from app import ops
from app.calling.carrier_errors import PAUSE_SECONDS as CHANNEL_PAUSE
from app.calling.carrier_errors import is_channel_limit
from app.calling.decide import Situation, require
from app.calling.records import (
    CallView,
    CommandView,
    LeadAttempts,
    LegView,
    PolicyLimits,
    ReservationView,
)
from app.calling.recovery import next_step
from app.calling.state import TERMINAL, moved
from app.compliance.policy import authorize, retry_seconds
from app.numbers import pool
from app.platform.config import TEST_LINES
from app.platform.db import (
    MODELS,
    SCOPE_KEY,
    ActiveClaim,
    doc,
    emit,
    get,
    put,
    scoped_where,
    uid,
    update,
    workspace,
)
from app.platform.errors import body_of, dt, fail
from app.platform.leases import owns_effect
from app.providers.errors import CarrierNotSubmitted


def _replay(service, p):
    """The replay carrier answers and ends calls itself; its calls are labeled synthetic."""
    return ops.carrier(service, workspace(p)).replay


def _held_reservation(service, s, p, reservation_id, now):
    """The rep's live, claimed reservation, or a refusal."""
    reservation = ops.required(service, s, "reservations", reservation_id, p)
    live = (
        reservation.data["rep_id"] == p["user_id"]
        and reservation.data["status"] == "reserved"
        and dt(reservation.data["expires_at"]) > now
    )
    require(Situation(step="dial", reservation_ok=live), "Reservation expired or not owned by this rep")
    claim = s.scalar(select(ActiveClaim).where(ActiveClaim.reservation_id == reservation.id))
    require(Situation(step="dial", reservation_ok=claim is not None), "Reservation claim is no longer valid")
    return reservation, claim, ReservationView.model_validate(reservation.data)


def _dial_payload(service, scope, call_id, phone, caller, agent):
    return {
        "to": phone,
        "from": caller,
        "link_to": agent,
        "bridge_on_answer": True,
        "answering_machine_detection": "premium",
        "timeout_secs": service.configs.read(scope).get("outbound_ring_seconds", 40),
        "prevent_double_bridge": True,
        "time_limit_secs": service.settings.max_call_seconds,
        "command_id": call_id,
        "client_state": base64.b64encode(
            json.dumps({"call_id": call_id, SCOPE_KEY: scope}).encode()
        ).decode(),
    }


def _prepare_call(service, p, data):
    now = service.clock()
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        previous = ops._by_key(service, s, p, "call_attempts", data["idempotency_key"])
        if previous:
            if (
                previous.data["rep_id"] != p["user_id"]
                or previous.data["reservation_id"] != data["reservation_id"]
            ):
                fail(409, "Idempotency key belongs to another intent")
            return doc(previous), False
        reservation, claim, held = _held_reservation(service, s, p, data["reservation_id"], now)
        if service.managed.pool is not None and not held.pool_ref:
            require(Situation(step="dial", reservation_ok=False), "Reserve the shared number before dialing.")
        require(
            Situation(
                blockers=tuple(ops.dial_blockers_in(service, s, p, rep=True)),
                step="dial",
                reservation_ok=True,
            )
        )
        agent = ops.required(service, s, "users", p["user_id"], p).data["presence"]["agent_call_control_id"]
        lead = ops.required(service, s, "leads", reservation.data["lead_id"], p)
        ops.refuse_outside_test_line(service, p, lead)
        number = ops.required(service, s, "number_inventory", reservation.data["number_id"], p)
        service.managed.require_number(workspace(p), number.data)
        scoped_p = {**p, **held.dial_scope}
        decision = authorize(ops.context(service, s, scoped_p, lead, number))
        ops.record_decision(service, s, p, lead, decision)
        if not decision.allowed:
            fail(403, {"code": "CALL_DENIED", "reasons": decision.reasons})
        call = put(
            s,
            "call_attempts",
            workspace(p),
            p["seller_id"],
            {
                "rep_id": p["user_id"],
                "lead_id": lead.id,
                "endpoint_id": lead.data["endpoint_id"],
                "number_id": number.id,
                "from_number": number.data["phone"],
                "reservation_id": reservation.id,
                **({"pool_ref": held.pool_ref} if held.pool_ref else {}),
                "dial_scope": held.dial_scope,
                "treatment_variant_id": held.treatment_variant_id,
                "feature_snapshot_id": held.feature_snapshot_id,
                "decision_id": held.policy_decision_id,
                "coaching_comparison": held.coaching_comparison,
                "script_comparison": held.script_comparison,
                "script_snapshot": ops.selected_script(
                    service, s, p, lead, assignment=held.script_comparison
                )[0],
                "state": "AUTHORIZED",
                "state_at": now.isoformat(),
                "started_at": now.isoformat(),
                "max_call_seconds": service.settings.max_call_seconds,
                "provider_id": None,
                "outcome": None,
                "transcript_version": 0,
                "synthetic": _replay(service, p),
                "is_test": lead.data["phone"] in TEST_LINES or ops.in_test_session(service, p),
                "transcription_source": "direct" if service.managed.enabled(workspace(p)) else "webhook",
            },
            key=data["idempotency_key"],
            now=now,
        )
        claim.call_id = call.id
        update(reservation, status="active", call_id=call.id)
        # The persistent agent leg carries the rep's own speech stream for this call.
        update(call, agent_provider_id=agent)
        payload = _dial_payload(
            service, workspace(p), call.id, lead.data["phone"], number.data["phone"], agent
        )
        put(
            s,
            "command_outbox",
            workspace(p),
            p["seller_id"],
            {
                "kind": "dial",
                "call_id": call.id,
                "rep_id": p["user_id"],
                "payload": payload,
                "status": "pending",
                "attempts": 0,
            },
            key=call.id,
            now=now,
        )
        put(
            s,
            "usage_ledger",
            workspace(p),
            p["seller_id"],
            {
                "provider": "telnyx",
                "call_id": call.id,
                "kind": "call_authorization",
                "at": now.isoformat(),
                "reserved_usd": 0.05,
                "cost_usd": None,
                "billing_verified": False,
                "synthetic": _replay(service, p),
            },
            key="call:" + call.id,
            now=now,
        )
        emit(s, workspace(p), "call.updated", call, now, {"state": call.data["state"]})
        return doc(call), True


def _call_snapshot(service, p, call_id):
    with service.db.transaction(False) as s:
        return doc(ops.required(service, s, "call_attempts", call_id, p))


async def start_call(service, p, data):
    call, created = await asyncio.to_thread(ops._prepare_call, service, p, data)
    if not created:
        return call
    await ops.dispatch_call(service, p, call["id"])
    return await asyncio.to_thread(ops._call_snapshot, service, p, call["id"])


def _claim_dial(service, p, call_id, owner):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        # The call is already prepared: only what stops calling now can refuse it here.
        require(
            Situation(
                blockers=tuple(ops.dial_blockers_in(service, s, p, rep=False)),
                step="dial",
                reservation_ok=True,
            )
        )
        # Lock call, then lead, as other call writers do: the dial claim updates both.
        call = ops.required(service, s, "call_attempts", call_id, p, lock=True)
        model = MODELS["command_outbox"]
        command = s.scalar(
            select(model).where(
                *scoped_where(model, {SCOPE_KEY: workspace(p)})[:1],
                model.seller_id == p["seller_id"],
                model.external_key == call_id,
                model.op_kind == "dial",
            )
        )
        if not command or CommandView.model_validate(command.data).call_id != call_id:
            fail(409, "Original dial intent is missing; review this call before retrying")
        if command.data["status"] != "pending":
            return
        lead = ops.required(service, s, "leads", call.data["lead_id"], p, lock=True)
        ops.refuse_outside_test_line(service, p, lead)
        number = ops.required(service, s, "number_inventory", call.data["number_id"], p)
        service.managed.require_number(workspace(p), number.data)
        decision = authorize(
            ops.context(service, s, {**p, **CallView.model_validate(call.data).dial_scope}, lead, number)
        )
        submission_decision = ops.record_decision(service, s, p, lead, decision)
        if not decision.allowed:
            update(command, status="cancelled", reasons=decision.reasons)
            ops.finish_call(service, s, p, call, "policy_cancelled")
            return
        policy = ops.required(
            service,
            s,
            "policy_versions",
            ops.required(service, s, "campaigns", lead.data["campaign_id"], p).data["policy_id"],
            p,
        )
        attempts = LeadAttempts.model_validate(lead.data)
        ordinal = (attempts.attempt_count or 0) + 1
        allowance = max(
            attempts.attempt_allowance or 0, PolicyLimits.model_validate(policy.data).max_attempts
        )
        payload = command.data["payload"]
        update(
            command,
            status="sending",
            attempts=1,
            submitted_at=service.clock().isoformat(),
            sent_at=service.clock().isoformat(),
            lease_owner=owner,
            lease_until=(service.clock() + timedelta(seconds=30)).isoformat(),
            # Restored only if the request provably never reached the carrier.
            prior_attempt={
                k: getattr(attempts, k) for k in ("attempt_count", "last_attempt_at", "next_attempt_at")
            },
        )
        update(
            call,
            **moved(call.data, "dial.sent", service.clock()),
            submission_decision_id=submission_decision.id,
            submitted_at=service.clock().isoformat(),
        )
        update(call, attempt_ordinal=ordinal, attempt_allowance=allowance)
        if dt(attempts.pair_until) and service.clock() < dt(attempts.pair_until):
            update(call, pair_of=attempts.pair_of)
        update(lead, pair_until=None, pair_rep_id=None, pair_of=None, pair_opened_at=None)
        update(
            lead,
            attempt_count=ordinal,
            attempt_allowance=allowance,
            last_attempt_at=service.clock().isoformat(),
            next_attempt_at=(service.clock() + timedelta(seconds=retry_seconds(policy.data))).isoformat(),
            # Coverage restarts from this attempt, so tried leads cannot crowd out first calls.
            coverage_due_at=(service.clock() + timedelta(seconds=retry_seconds(policy.data))).isoformat(),
        )
        return command.id, doc(call), lead.data["phone"], payload


def _dial_failed(service, p, call_id, command_id, owner, exc):
    with service.db.transaction() as s:
        command = ops.required(service, s, "command_outbox", command_id, p)
        s.refresh(command, with_for_update=True)
        call = ops.required(service, s, "call_attempts", call_id, p)
        if owns_effect(command.data, owner, service.clock()) and command.data["status"] == "sending":
            rejected = isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500
            limited = rejected and is_channel_limit(exc.response.status_code, body_of(exc.response))
            # Over the account's channel limit nothing reached the prospect: not counted, and calling waits.
            unsent = isinstance(exc, CarrierNotSubmitted) or limited
            if limited:
                until = service.clock() + timedelta(seconds=CHANNEL_PAUSE)
                service.configs.write(workspace(p), {"channel_limited_until": until.isoformat()})
            update(
                command,
                status="failed" if rejected or unsent else "uncertain",
                error=type(exc).__name__,
                lease_owner=None,
                lease_until=None,
            )
            if unsent:
                # Nothing reached the carrier: no contact, so the attempt is not counted.
                lead = ops.required(service, s, "leads", call.data["lead_id"], p)
                if (
                    LeadAttempts.model_validate(lead.data).attempt_count
                    == CallView.model_validate(call.data).attempt_ordinal
                ):
                    update(lead, **CommandView.model_validate(command.data).prior_attempt)
                ops.finish_call(
                    service, s, p, call, "carrier_channel_limit" if limited else "carrier_not_submitted"
                )
            elif rejected:
                ops.finish_call(service, s, p, call, "provider_rejected")
            elif call.data["state"] == "DIALING" and not CallView.model_validate(call.data).provider_id:
                update(call, **moved(call.data, "dial.uncertain", service.clock()))
                emit(s, workspace(p), "call.updated", call, service.clock(), {"state": "RECONCILING"})


def scoped_get(service, s, p, kind, object_id):

    row = get(s, kind, object_id, workspace(p))
    return row if row and row.seller_id == p["seller_id"] else None


def _dial_completed(service, p, call_id, claimed, owner, receipt):
    command_id, call_data, phone, payload = claimed
    cancel = False
    with service.db.transaction() as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        current = ops.required(service, s, "command_outbox", command_id, p)
        s.refresh(current, with_for_update=True)
        if current.data["status"] != "completed":
            if current.data["status"] != "sending" or not owns_effect(current.data, owner, service.clock()):
                return
            ops.bind_dial_receipt(service, s, p, call, receipt["call_control_id"])
        cancel = call.data["state"] not in TERMINAL and (
            call.data["state"] == "ENDING" or ops.suppressed(service, s, p, phone)
        )
        update(
            call,
            **moved(
                call.data,
                "call.ringing" if _replay(service, p) else "call.initiated",
                service.clock(),
            ),
        )
        emit(s, workspace(p), "call.updated", call, service.clock(), {"state": call.data["state"]})
    return cancel


async def dispatch_call(service, p, call_id):
    owner = uid()
    claimed = await asyncio.to_thread(ops._claim_dial, service, p, call_id, owner)
    if not claimed:
        return
    pool_ref = claimed[1].get("pool_ref")
    try:
        try:
            provider = await asyncio.to_thread(ops.carrier, service, workspace(p))
        except Exception as exc:
            # Configuration fails before any request exists; nothing can be live.
            raise CarrierNotSubmitted(type(exc).__name__) from exc
        if pool_ref:
            snapshot = service.managed.pool
            if (
                snapshot
                and snapshot.shared_connection_id
                and getattr(provider, "connection_id", snapshot.shared_connection_id)
                != snapshot.shared_connection_id
            ):
                raise CarrierNotSubmitted("This calling profile does not match the shared application.")
            admission = await pool.request(
                service.managed,
                "relay_pool_submit",
                {
                    "request_ref": pool_ref,
                    "local_call": call_id,
                    "number": claimed[3]["from"],
                },
            )
            if not admission.submitted:
                raise CarrierNotSubmitted("The shared outbound number is no longer reserved.")
        receipt = await provider.dial(claimed[3])
    except Exception as exc:
        logging.getLogger(__name__).warning("Dial for %s failed: %s", call_id, type(exc).__name__)
        if pool_ref and (
            isinstance(exc, CarrierNotSubmitted)
            or isinstance(exc, httpx.HTTPStatusError)
            and 400 <= exc.response.status_code < 500
        ):
            await pool.cancel(service.managed, pool_ref, unsent=True)
        await asyncio.to_thread(ops._dial_failed, service, p, call_id, claimed[0], owner, exc)
        return
    cancel = await asyncio.to_thread(ops._dial_completed, service, p, call_id, claimed, owner, receipt)
    if cancel:
        await ops.hangup(service, p, call_id)


def bind_dial_receipt(service, s, p, call, provider_id):
    if CallView.model_validate(call.data).provider_id not in {None, provider_id}:
        fail(409, "Provider call reference conflicts with the established leg")
    update(call, provider_id=provider_id)
    command = ops._by_key(service, s, p, "command_outbox", call.id)
    if (
        command
        and CommandView.model_validate(command.data).call_id == call.id
        and CommandView.model_validate(command.data).kind == "dial"
        and CommandView.model_validate(command.data).status
        in {"sending", "uncertain", "reconciling", "review_required", "unconfirmed"}
    ):
        update(command, status="completed", provider_id=provider_id, lease_owner=None, lease_until=None)
    stop = ops._by_key(service, s, p, "command_outbox", call.id + ":hangup")
    if stop and CommandView.model_validate(stop.data).status == "unconfirmed":
        # A requested end that lacked a carrier reference can now stop the actual leg.
        update(stop, status="pending", provider_id=provider_id, recovery_reason=None)
    prior = ops._by_key(service, s, p, "call_legs", provider_id)
    if not prior:
        put(
            s,
            "call_legs",
            workspace(p),
            p["seller_id"],
            {
                "call_id": call.id,
                "provider_id": provider_id,
                "kind": "prospect",
                "state": call.data["state"],
            },
            key=provider_id,
            now=service.clock(),
        )


def finish_call(service, s, p, call, reason=None):
    service.tips.end(call.id)
    if call.data["state"] != "FINALIZED":
        ended = dt(CallView.model_validate(call.data).ended_at) or service.clock()
        update(
            call,
            **moved(call.data, "call.ended", service.clock()),
            ended_at=ended.isoformat(),
            end_reason=reason or "hangup",
        )
        answered = dt(CallView.model_validate(call.data).answered_at)
        if answered and answered <= ended:
            update(call, connected_seconds=(ended - answered).total_seconds())
    legs = MODELS["call_legs"]
    if CallView.model_validate(call.data).provider_id:
        for leg in s.scalars(
            select(legs).where(
                *scoped_where(legs, {SCOPE_KEY: workspace(p)})[:1],
                legs.seller_id == p["seller_id"],
                legs.op_provider_id == call.data["provider_id"],
            )
        ):
            seen = LegView.model_validate(leg.data)
            if seen.kind == "prospect" and seen.call_id == call.id:
                update(leg, state="ENDED", ended_at=CallView.model_validate(call.data).ended_at)
    for claim in list(s.scalars(select(ActiveClaim).where(ActiveClaim.call_id == call.id))):
        s.delete(claim)

    ops.settle_call_estimate(service, s, p, call)
    emit(s, workspace(p), "call.updated", call, service.clock(), {"state": call.data["state"]})


def _claim_hangup(service, p, call_id, owner, recovery_expected=None):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        call = ops.required(service, s, "call_attempts", call_id, p)
        s.refresh(call, with_for_update=True)
        if call.data["state"] in TERMINAL:
            return doc(call), None
        # A recovery hang-up goes ahead only if the pass's decision still holds for the call as it is now.
        if recovery_expected is not None and (
            CallView.model_validate(call.data).provider_id != recovery_expected.get("provider_id")
            or next_step(call.data, service.clock()) not in {"hangup.requested", "call.ended"}
        ):
            return doc(call), None
        provider_id = CallView.model_validate(call.data).provider_id
        command_id = call.id + ":hangup"
        existing = ops._by_key(service, s, p, "command_outbox", command_id)
        if not existing:
            existing = put(
                s,
                "command_outbox",
                workspace(p),
                p["seller_id"],
                {
                    "kind": "hangup",
                    "call_id": call.id,
                    "rep_id": CallView.model_validate(call.data).rep_id,
                    "provider_id": provider_id,
                    "status": "pending",
                },
                key=command_id,
                now=service.clock(),
            )
        prior = CommandView.model_validate(existing.data)
        if prior.lease_until and dt(prior.lease_until) > service.clock():
            return doc(call), None
        if prior.status == "completed":
            return doc(call), None
        update(
            existing,
            status="sending" if provider_id else "review_required",
            provider_id=provider_id,
            lease_owner=owner if provider_id else None,
            lease_until=(service.clock() + timedelta(seconds=30)).isoformat() if provider_id else None,
            sent_at=prior.sent_at or service.clock().isoformat(),
            recovery_reason=None
            if provider_id
            else "Await the signed dial receipt or attach a verified carrier call reference",
        )
        update(call, **moved(call.data, "hangup.requested", service.clock()))
        ops.stop_agent_transcription_in(service, s, p, call)
        service.tips.end(call.id)
        emit(s, workspace(p), "call.updated", call, service.clock(), {"state": "ENDING"})
        return doc(call), {"id": existing.id, "provider_id": provider_id, "command_id": command_id}


def _hangup_completed(service, p, call_id, command_id, owner, error=None):
    """Record a sent stop's receipt (if any), then queue wrap-up once the call has ended."""
    with service.db.transaction() as s:
        current = ops.required(service, s, "command_outbox", command_id, p) if command_id else None
        if current:
            s.refresh(current, with_for_update=True)
        if (
            current
            and current.data["status"] == "sending"
            and owns_effect(current.data, owner, service.clock())
        ):
            if error is not None:
                update(current, status="uncertain", lease_owner=None, lease_until=None)
            else:
                update(current, status="completed", lease_owner=None, lease_until=None)
                if _replay(service, p):
                    call = ops.required(service, s, "call_attempts", call_id, p, lock=True)
                    ops.finish_call(service, s, p, call, "simulated_hangup_receipt")
        call = ops.required(service, s, "call_attempts", call_id, p)
        if call.data["state"] in TERMINAL:
            s.refresh(call, with_for_update=True)
            ops.enqueue_aftercall(service, s, p, call)
        return doc(call)


async def hangup(service, p, call_id, *, recovery_expected=None):
    owner = uid()
    call, command = await asyncio.to_thread(ops._claim_hangup, service, p, call_id, owner, recovery_expected)
    if not command:
        return call
    error = None
    if command["provider_id"]:
        try:
            provider = await asyncio.to_thread(ops.carrier, service, workspace(p))
            await provider.hangup(command["provider_id"], command["command_id"])
        except Exception as exc:
            error = exc
            logging.getLogger(__name__).warning("Durable hangup pending: %s", type(exc).__name__)
    sent = command["id"] if command["provider_id"] else None
    return await asyncio.to_thread(ops._hangup_completed, service, p, call_id, sent, owner, error)
