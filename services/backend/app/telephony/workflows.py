import asyncio
import base64
import json
from datetime import timedelta

from sqlalchemy import select

from app import ops
from app import service as base
from app.calling import dial, events
from app.calling.state import TERMINAL
from app.compliance.policy import authorize
from app.platform.db import MODELS, audit, doc, emit, get, put, uid, update
from app.platform.leases import owns_effect
from app.service import dt, fail
from app.telephony.recovery import safe_stop, within_command_window

KEYPAD_STATES = {"DIALING", "RINGING", "ANSWERED_UNCLASSIFIED", "HUMAN", "SCREENING"}
TRANSCRIPTION_ENGINE = "Soniox"


# How long the final-stage second attempt stays offered after the unanswered one ends.
SECOND_ATTEMPT_WINDOW = timedelta(minutes=5)


def _keypad_intent(service, p, call_id, digits, request_id):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        call = ops.required(service, s, "call_attempts", call_id, p)
        prior = ops._by_key(service, s, p, "command_outbox", f"{call_id}:dtmf:{request_id}")
        if prior:
            if prior.data["payload"]["digits"] != digits:
                fail(409, "Keypad request identity already belongs to different digits")
            return prior.id
        if call.data["state"] not in KEYPAD_STATES:
            fail(409, "Call is no longer active for this rep")
        if not call.data.get("provider_id"):
            fail(409, "Wait for the original carrier call reference")
        command = ops.control_intent(
            service,
            s,
            p,
            call_id,
            "send_dtmf",
            {"digits": digits},
            target=call.data["provider_id"],
            suffix=f"dtmf:{request_id}",
        )
        update(
            command,
            requested_by=p["user_id"],
            expires_at=(service.clock() + timedelta(seconds=10)).isoformat(),
        )
        return command.id


def keypad_status(service, p, call_id, request_id):
    with service.db.transaction(False) as s:
        ops.required(service, s, "call_attempts", call_id, p)
        command = ops._by_key(service, s, p, "command_outbox", f"{call_id}:dtmf:{request_id}")
        if not command or command.data.get("action") != "send_dtmf":
            fail(404, "Keypad request not received")
        return {
            "command_id": command.id,
            "status": command.data["status"],
            "sent": command.data["status"] == "completed",
            "reason": command.data.get("recovery_reason") or command.data.get("reason"),
        }


async def send_keypad(service, p, call_id, digits, request_id):
    command_id = await asyncio.to_thread(ops._keypad_intent, service, p, call_id, digits, request_id)
    await ops.dispatch_controls(service, p, command_id=command_id)
    return await asyncio.to_thread(ops.keypad_status, service, p, call_id, request_id)


def stay_on_call(service, p, call_id):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        call = ops.required(service, s, "call_attempts", call_id, p, lock=True)
        if call.data["rep_id"] != p["user_id"]:
            fail(404, "Object not found")
        if call.data["state"] in TERMINAL | {"ENDING"}:
            fail(409, "The call has already ended")
        command = ops._by_key(service, s, p, "command_outbox", f"{call_id}:machine-hangup")
        if command:
            s.refresh(command, with_for_update=True)
            if command.data["status"] not in {"pending", "cancelled"} or command.data.get("sent_at"):
                fail(409, "The voicemail hang-up has already been sent")
            update(command, status="cancelled", reason="REP_STAY")
        update(call, stay_on_voicemail=True)
        emit(s, p["tenant_id"], "call.updated", call, service.clock())
        return doc(call)


def control_intent(service, s, p, call_id, action, payload, *, target=None, suffix=None):
    key = f"{call_id}:{suffix or action}"
    prior = ops._by_key(service, s, p, "command_outbox", key)
    if prior:
        return prior
    call = get(s, "call_attempts", call_id, p["tenant_id"])
    return put(
        s,
        "command_outbox",
        p["tenant_id"],
        p["seller_id"],
        {
            "kind": "action",
            "call_id": call_id,
            "rep_id": call.data.get("rep_id") if call else None,
            "action": action,
            "target": target,
            "payload": {**payload, "command_id": key},
            "status": "pending",
        },
        key=key,
        now=service.clock(),
    )


def record_carrier_block(service, s, p, call, number, code):
    now = service.clock()
    update(call, sip_block_code=code, carrier_outcome="temporary_failure")
    s.refresh(number, with_for_update=True)
    previous = dt(number.data.get("last_sip_block_at"))
    count = (
        number.data.get("recent_sip_blocks", 0) + 1 if previous and now - previous < timedelta(days=1) else 1
    )
    update(number, last_sip_block_at=now.isoformat(), recent_sip_blocks=count)
    if count >= 2 and number.data["lifecycle"] in {"healthy", "limited_real_use"}:
        update(
            number,
            lifecycle="paused_for_outbound",
            reason="Repeated SIP 603 or 603+ carrier blocks",
            callback_retained=True,
        )
        audit(
            s,
            p["tenant_id"],
            p["seller_id"],
            p["user_id"],
            "number.carrier_block_pause",
            number.id,
            now,
            {"count": count, "last_code": code},
        )
    emit(s, p["tenant_id"], "number.updated", number, now)


def event(service, p, event_id, event_type, call_id, payload=None, *, session=None):
    with service.db.transaction(session=session) as session:
        # An event can carry an opt-out; keep authorization before call locks.
        service.db.authorize_stream(session, p)
        result = base.event(service, p, event_id, event_type, call_id, payload, session=session)
        if result["duplicate"]:
            with service.db.transaction(False, session=session) as s:
                prior = ops._by_key(service, s, p, "provider_events", event_id)
                if prior.data.get("effects_complete", False):
                    return result
        payload = payload or {}
        with service.db.transaction(session=session) as s:
            call = ops.required(service, s, "call_attempts", call_id, p)
            lead = ops.required(service, s, "leads", call.data["lead_id"], p)
            campaign = ops.required(service, s, "campaigns", lead.data["campaign_id"], p)
            policy = ops.required(service, s, "policy_versions", campaign.data["policy_id"], p).data
            number = ops.required(service, s, "number_inventory", call.data["number_id"], p)
            if event_type == "call.hangup":
                update(call, **events.hangup_facts(payload))
                code = events.sip_block(call.data, payload)
                if code:
                    ops.record_carrier_block(service, s, p, call, number, code)
                outcome = events.carrier_outcome(call.data, payload)
                if outcome:
                    update(call, carrier_outcome=outcome)
                    ops._offer_second_attempt(service, s, call, lead, policy)
            changes, hang_up_at = events.machine(
                call.data, event_type, final=events.final_attempt(call.data, policy), now=service.clock()
            )
            if changes:
                update(call, **changes)
                if hang_up_at:
                    command = ops.control_intent(
                        service, s, p, call_id, "hangup", {}, suffix="machine-hangup"
                    )
                    if hang_up_at > service.clock():
                        # The maintenance pass sends it once due; Space skips sooner.
                        update(command, not_before=hang_up_at.isoformat())
                    elif session is not None:
                        result["voicemail_control"] = (p, command.id)
                emit(s, p["tenant_id"], "call.updated", call, service.clock())
            changes = events.screening(call.data, event_type)
            if changes:
                # Call screening is a person's gate, never a mailbox: a pending skip is withdrawn (R6.3).
                waiting = ops._by_key(service, s, p, "command_outbox", f"{call_id}:machine-hangup")
                if waiting and waiting.data["status"] == "pending" and not waiting.data.get("sent_at"):
                    update(waiting, status="cancelled", reason="SCREENING_DETECTED")
                update(call, **changes)
                emit(s, p["tenant_id"], "call.updated", call, service.clock())
            changes, first_answer = events.human(call.data, event_type, service.clock())
            if changes:
                waiting = ops._by_key(service, s, p, "command_outbox", f"{call_id}:machine-hangup")
                if waiting and waiting.data["status"] == "pending":
                    update(waiting, status="cancelled", reason="HUMAN_ANSWERED")
                update(call, **changes)
                if first_answer:
                    ops._keep_callback_promise(service, s, p, call, lead)
            if event_type == "call.answered" and call.data["state"] not in TERMINAL | {"ENDING"}:
                decision = authorize(
                    ops.context(service, s, p, lead, number, permission="transcription", active=True)
                )
                if decision.allowed:
                    ops.start_transcription_in(service, s, p, call)
            if (
                event_type == "call.hangup"
                and call.data["state"] in TERMINAL
                and call.data.get("carrier_outcome")
            ):
                ops.enqueue_aftercall(service, s, p, call)
                # R4.9: a carrier fact (no answer, busy, voicemail, failure) is saved as soon as this
                # event commits, never after a job or an AI request.
                result["save_outcome"] = (p, call_id)
            receipt = ops._by_key(service, s, p, "provider_events", event_id)
            update(receipt, effects_complete=True)
        return result


def start_transcription_in(service, s, p, call):
    """One inbound stream per leg: the leg that carried speech identifies its speaker.

    Soniox omits `transcription_track`, so a single `both` stream cannot tell the rep from the
    prospect. The rep's agent leg outlives the call, so its stream names the call in client state.
    """
    if call.data.get("transcription_source") == "direct":
        update(call, transcription_layout="direct_tracks")
        return
    stream = {"transcription_engine": TRANSCRIPTION_ENGINE, "transcription_tracks": "inbound"}
    ops.control_intent(service, s, p, call.id, "transcription_start", stream)
    agent = call.data.get("agent_provider_id")
    if agent:
        state = json.dumps({"transcript_call_id": call.id, "tenant_id": p["tenant_id"]})
        ops.control_intent(
            service,
            s,
            p,
            call.id,
            "transcription_start",
            {**stream, "client_state": base64.b64encode(state.encode()).decode()},
            target=agent,
            suffix="transcription_start:agent",
        )
    update(call, transcription_layout="leg_inbound")


def stop_agent_transcription_in(service, s, p, call):
    """The prospect stream ends with its leg; the persistent agent leg needs an explicit stop."""
    start = ops._by_key(service, s, p, "command_outbox", call.id + ":transcription_start:agent")
    if not start or start.data["status"] == "cancelled":
        return
    if start.data["status"] == "pending" and not start.data.get("sent_at"):
        update(start, status="cancelled", reason="CALL_NO_LONGER_ACTIVE")
        return
    ops.control_intent(
        service,
        s,
        p,
        call.id,
        "transcription_stop",
        {},
        target=start.data["target"],
        suffix="transcription_stop:agent",
    )


def finish_call(service, s, p, call, reason=None):
    dial.finish_call(service, s, p, call, reason)
    ops.stop_agent_transcription_in(service, s, p, call)
    for lease in ops.scoped(service, s, "number_leases", p):
        if lease.data.get("call_id") == call.id and lease.data.get("active"):
            update(lease, active=False)


def _control_work(service, p):
    ops.inbound_watchdog(service, p)
    now = service.clock()
    with service.db.transaction(False) as s:
        model = MODELS["command_outbox"]
        commands = [
            doc(r)
            for r in s.scalars(
                select(model)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.op_kind == "action",
                    model.op_status == "pending",
                )
                .order_by(model.created_at, model.id)
            )
            if now >= (dt(r.data.get("not_before")) or now)
        ]
    # Answer precedes its dependent inbound transfer even when timestamps tie.
    commands.sort(key=lambda command: bool(command.get("after_command_id")))
    return commands


def _claim_control(service, p, command_id):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        current = ops.required(service, s, "command_outbox", command_id, p)
        s.refresh(current, with_for_update=True)
        command = doc(current)
        if current.data["status"] != "pending":
            return None
        if (
            service.managed.enabled(p["tenant_id"])
            and not service.managed.status()["connected"]
            and command["action"] in {"answer", "bridge", "transfer", "transcription_start"}
        ):
            return None
        sent = dt(current.data.get("sent_at"))
        if sent and not within_command_window(sent, service.clock()) and not safe_stop(command["action"]):
            update(
                current,
                status="review_required",
                recovery_reason="Original command deduplication window expired before retry submission",
            )
            return None
        inbound = command.get("inbound", False)
        call = ops.required(
            service, s, "followup_tasks" if inbound else "call_attempts", command["call_id"], p
        )
        if inbound and not ops.inbound_admit_command(service, s, p, call, current):
            return None
        if command["action"] == "send_dtmf":
            user = get(s, "users", command.get("requested_by"), p["tenant_id"])
            if (
                not user
                or user.seller_id != p["seller_id"]
                or call.data["state"] not in KEYPAD_STATES
                or command.get("target") != call.data.get("provider_id")
                or not command.get("expires_at")
                or dt(command["expires_at"]) <= service.clock()
            ):
                update(
                    current,
                    status="review_required" if sent else "cancelled",
                    recovery_reason="Keypad context changed or expired; do not replay old digits",
                )
                return None
        if not inbound and call.data["state"] in TERMINAL | {"ENDING"} and not safe_stop(command["action"]):
            update(current, status="cancelled", reason="CALL_NO_LONGER_ACTIVE")
            return None
        if command["action"] in {"playback_start", "gather", "record_start"}:
            # Saved by a version that had voicemail drop or recording (removed in R2.7): never sent.
            update(current, status="cancelled", reasons=["VOICEMAIL_DROP_REMOVED"])
            if command["action"] != "record_start":
                update(call, voicemail_result="blocked_before_playback")
            return None
        if command["action"] == "transcription_start":
            privacy = ops.required(service, s, "sellers", p["seller_id"], p).data.get("operating_policy", {})
            if not service.settings.simulation and not (
                privacy.get("transcription_privacy_verified")
                and privacy.get("transcription_privacy_reference")
            ):
                update(current, status="cancelled", reason="RAW_CAPTURE_PRIVACY_NOT_AUTHORIZED")
                return None
            lead = ops.required(service, s, "leads", call.data["lead_id"], p)
            number = ops.required(service, s, "number_inventory", call.data["number_id"], p)
            permission = "transcription"
            if (
                call.data["state"] in TERMINAL
                or not authorize(
                    ops.context(service, s, p, lead, number, permission=permission, active=True)
                ).allowed
            ):
                update(current, status="cancelled")
                return None
        target = command.get("target") or call.data.get("provider_id")
        if not target:
            update(
                current,
                status="review_required",
                recovery_reason="Missing carrier call reference; reconcile the original dial first",
            )
            return None
        owner = uid()
        update(
            current,
            status="sending",
            lease_owner=owner,
            sent_at=current.data.get("sent_at") or service.clock().isoformat(),
            lease_until=(service.clock() + timedelta(seconds=30)).isoformat(),
        )
        return command, call.id, inbound, target, owner, call.data.get("provider_id")


def _control_completed(service, p, claimed, error=None):
    command, call_id, inbound, target, owner, provider_id = claimed
    with service.db.transaction() as s:
        current = ops.required(service, s, "command_outbox", command["id"], p)
        s.refresh(current, with_for_update=True)
        if current.data["status"] != "sending" or not owns_effect(current.data, owner, service.clock()):
            return
        if error is not None:
            update(
                current,
                status="uncertain",
                lease_owner=None,
                lease_until=None,
                error=type(error).__name__,
            )
            return
        update(current, status="completed", lease_owner=None, lease_until=None)
        if (
            service.settings.simulation
            and not inbound
            and command["action"] == "hangup"
            and target == provider_id
        ):
            call = ops.required(service, s, "call_attempts", call_id, p, lock=True)
            ops.finish_call(service, s, p, call, "simulated_control_hangup")
            ops.enqueue_aftercall(service, s, p, call)


async def dispatch_controls(service, p, *, command_id=None):
    commands = [{"id": command_id}] if command_id else await asyncio.to_thread(ops._control_work, service, p)
    for command in commands:
        claimed = await asyncio.to_thread(ops._claim_control, service, p, command["id"])
        if not claimed:
            continue
        command, _, _, target, _, _ = claimed
        try:
            provider = await asyncio.to_thread(ops.carrier, service, p["tenant_id"])
            if command["action"] == "hangup":
                await provider.hangup(target, command["payload"]["command_id"])
            else:
                await provider.action(target, command["action"], command["payload"])
        except Exception as exc:
            await asyncio.to_thread(ops._control_completed, service, p, claimed, exc)
        else:
            await asyncio.to_thread(ops._control_completed, service, p, claimed)
    return {"processed": len(commands)}


def _keep_callback_promise(service, s, p, call, lead):
    """Reaching the person on a call at or after a promised callback time keeps that promise."""
    from app.aftercall.tasks import ACTIVE

    due_now = [
        t
        for t in ops.lead_records(service, s, p, "followup_tasks", lead.id)
        if t.data.get("kind") == "callback"
        and t.data.get("status") in ACTIVE
        and (due := dt(t.data.get("due_at")))
        and due <= service.clock() + timedelta(minutes=1)
    ]
    for task in due_now:
        update(
            task,
            status="completed",
            owner_id=call.data["rep_id"],
            completion_source="outbound_rep_connected",
            completed_call_id=call.id,
            completed_at=service.clock().isoformat(),
        )
        emit(s, p["tenant_id"], "task.updated", task, service.clock())
    if due_now:
        ops._sync_callback(service, s, p, lead)


def _offer_second_attempt(service, s, call, lead, policy):
    """The final stage may pair an unanswered next-to-last attempt with one immediate retry.

    It is only a suggestion for the same rep's next Space press, open five minutes; it never
    dials by itself, and it is not offered after a conversation, a pair or a pending callback.
    """
    allowance = call.data.get("attempt_allowance", policy.get("max_attempts", 4))
    if (
        call.data.get("attempt_ordinal") != allowance - 1
        or call.data.get("pair_of")
        or lead.data.get("callback_due_at")
        or lead.data.get("outreach_hold")
        or lead.data.get("status") != "eligible"
    ):
        return
    now = service.clock()
    update(
        lead,
        pair_of=call.id,
        pair_rep_id=call.data["rep_id"],
        pair_opened_at=now.isoformat(),
        pair_until=(now + SECOND_ATTEMPT_WINDOW).isoformat(),
    )
