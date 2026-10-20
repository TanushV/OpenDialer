import asyncio
import base64
import json
import re
from datetime import timedelta

from sqlalchemy import delete, select

from app.compliance.policy import authorize
from app.db import MODELS, ActiveClaim, audit, doc, emit, get, put, uid, update
from app.service import dt, fail
from app.telephony.recovery import safe_stop, within_command_window
from app.telephony.reducer import HUMAN_EVENTS, TERMINAL, owns_effect

KEYPAD_STATES = {"DIALING", "RINGING", "ANSWERED_UNCLASSIFIED", "HUMAN", "GATEKEEPER", "SCREENING", "IVR"}
TRANSCRIPTION_ENGINE = "Soniox"


class TelephonyWorkflows:
    def _keypad_intent(self, p, call_id, digits, request_id):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            call = self.required(s, "call_attempts", call_id, p)
            if call.data["rep_id"] != p["user_id"] and p["role"] == "rep":
                fail(403, "Call belongs to another rep")
            prior = self._by_key(s, p, "command_outbox", f"{call_id}:dtmf:{request_id}")
            if prior:
                if prior.data["payload"]["digits"] != digits:
                    fail(409, "Keypad request identity already belongs to different digits")
                return prior.id
            if call.data["state"] not in KEYPAD_STATES or call.data.get("detached"):
                fail(409, "Call is no longer active for this rep")
            if not call.data.get("provider_id"):
                fail(409, "Wait for the original carrier call reference")
            command = self.control_intent(
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
                expires_at=(self.clock() + timedelta(seconds=10)).isoformat(),
            )
            return command.id

    def keypad_status(self, p, call_id, request_id):
        with self.db.transaction(False) as s:
            call = self.required(s, "call_attempts", call_id, p)
            if call.data["rep_id"] != p["user_id"] and p["role"] == "rep":
                fail(403, "Call belongs to another rep")
            command = self._by_key(s, p, "command_outbox", f"{call_id}:dtmf:{request_id}")
            if not command or command.data.get("action") != "send_dtmf":
                fail(404, "Keypad request not received")
            return {
                "command_id": command.id,
                "status": command.data["status"],
                "sent": command.data["status"] == "completed",
                "reason": command.data.get("recovery_reason") or command.data.get("reason"),
            }

    async def send_keypad(self, p, call_id, digits, request_id):
        command_id = await asyncio.to_thread(self._keypad_intent, p, call_id, digits, request_id)
        await self.dispatch_controls(p, command_id=command_id)
        return await asyncio.to_thread(self.keypad_status, p, call_id, request_id)

    def control_intent(self, s, p, call_id, action, payload, *, target=None, suffix=None):
        if action == "record_start":
            fail(403, "Call recording is disabled by the short-retention policy")
        key = f"{call_id}:{suffix or action}"
        prior = self._by_key(s, p, "command_outbox", key)
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
            now=self.clock(),
        )

    def event(self, p, event_id, event_type, call_id, payload=None, *, session=None):
        with self.db.transaction(session=session) as session:
            # An event can carry an opt-out; keep authorization before call locks.
            self.db.authorize_stream(session, p)
            result = super().event(p, event_id, event_type, call_id, payload, session=session)
            if result["duplicate"]:
                with self.db.transaction(False, session=session) as s:
                    prior = self._by_key(s, p, "provider_events", event_id)
                    if prior.data.get("effects_complete", False):
                        return result
            payload = payload or {}
            optout_phone = None
            with self.db.transaction(session=session) as s:
                call = self.required(s, "call_attempts", call_id, p)
                lead = self.required(s, "leads", call.data["lead_id"], p)
                campaign = self.required(s, "campaigns", lead.data["campaign_id"], p)
                policy = self.required(s, "policy_versions", campaign.data["policy_id"], p).data
                number = self.required(s, "number_inventory", call.data["number_id"], p)
                if event_type == "call.answered" and call.data["state"] not in TERMINAL | {"ENDING"}:
                    decision = authorize(
                        self.context(s, p, lead, number, permission="transcription", active=True)
                    )
                    if decision.allowed:
                        self.start_transcription_in(s, p, call)
                if (
                    event_type == "call.playback.started"
                    and call.data.get("detach_requested")
                    and call.data["state"] == "VM_PLAYING"
                ):
                    if not policy.get("detached_topology_verified") or not policy.get(
                        "late_human_recovery_destination"
                    ):
                        self.control_intent(s, p, call_id, "playback_stop", {})
                    else:
                        update(call, detached=True, rep_released_at=self.clock().isoformat())
                        s.execute(delete(ActiveClaim).where(ActiveClaim.call_id == call.id))
                        if not any(r.external_key == call.id for r in self.scoped(s, "number_leases", p)):
                            put(
                                s,
                                "number_leases",
                                p["tenant_id"],
                                p["seller_id"],
                                {
                                    "call_id": call.id,
                                    "number_id": call.data["number_id"],
                                    "active": True,
                                    "expires_at": call.data["voicemail_deadline"],
                                },
                                key=call.id,
                                now=self.clock(),
                            )
                        user = self.required(s, "users", call.data["rep_id"], p)
                        presence = user.data.get("presence", {})
                        agent = presence.get("agent_call_control_id")
                        if agent:
                            self.control_intent(
                                s, p, call_id, "hangup", {}, target=agent, suffix="release-agent"
                            )
                        if not self.settings.simulation:
                            update(
                                user,
                                presence={
                                    **presence,
                                    "status": "paused",
                                    "audio_ready": False,
                                    "telephony_ready": False,
                                    "agent_call_control_id": None,
                                },
                            )
                        audit(
                            s,
                            p["tenant_id"],
                            p["seller_id"],
                            p["user_id"],
                            "voicemail.detached",
                            call.id,
                            self.clock(),
                        )
                if event_type in HUMAN_EVENTS and call.data.get("detached"):
                    self.control_intent(s, p, call_id, "playback_stop", {}, suffix="late-human-stop")
                    recovery = policy.get("late_human_recovery_destination")
                    if recovery:
                        self.control_intent(
                            s, p, call_id, "transfer", {"to": recovery}, suffix="late-human-recovery"
                        )
                    else:
                        self.control_intent(s, p, call_id, "hangup", {}, suffix="late-human-no-destination")
                    update(call, late_human=True, recovery_required=True)
                if event_type == "call.gather.ended" and call.data.get("voicemail_asset"):
                    if payload.get("digits") == "9":
                        optout_phone = lead.data["phone"]
                        self.control_intent(s, p, call_id, "playback_stop", {}, suffix="optout-stop")
                        self.control_intent(s, p, call_id, "hangup", {}, suffix="optout-hangup")
                        if call.data["state"] not in TERMINAL:
                            update(call, state="ENDING", end_reason="voicemail_optout")
                            emit(s, p["tenant_id"], "call.updated", call, self.clock())
                    elif call.data["state"] not in TERMINAL:
                        self.control_intent(
                            s,
                            p,
                            call_id,
                            "gather",
                            {
                                "minimum_digits": 1,
                                "maximum_digits": 1,
                                "valid_digits": "9",
                                "timeout_millis": 60000,
                                "initial_timeout_millis": 60000,
                                "gather_id": call_id,
                            },
                            suffix="optout-rearm:" + event_id,
                        )
                if event_type == "call.playback.ended":
                    # Telnyx reports every outcome here; only `completed` played the whole asset.
                    status = payload.get("status")
                    self.control_intent(s, p, call_id, "hangup", {}, suffix="voicemail-completion")
                    update(
                        call,
                        voicemail_playback_status=status,
                        voicemail_result="playback_completed_not_mailbox_receipt"
                        if status == "completed"
                        else "playback_failed",
                    )
                if (
                    event_type == "call.dtmf.received"
                    and payload.get("digit") == "9"
                    and call.data.get("voicemail_asset")
                ):
                    optout_phone = lead.data["phone"]
                    self.control_intent(s, p, call_id, "playback_stop", {}, suffix="optout-stop")
                    self.control_intent(s, p, call_id, "hangup", {}, suffix="optout-hangup")
                    if call.data["state"] not in TERMINAL:
                        update(call, state="ENDING", end_reason="voicemail_optout")
                        emit(s, p["tenant_id"], "call.updated", call, self.clock())
                if event_type == "call.recording.saved":
                    update(
                        call,
                        recording_id=payload.get("recording_id"),
                        recording_url=None,
                        recording_review_required=True,
                    )
                receipt = self._by_key(s, p, "provider_events", event_id)
                update(receipt, effects_complete=not bool(optout_phone))
            if optout_phone:
                self.suppress(p, optout_phone, "opt_out", event_id=event_id, session=session)
                with self.db.transaction(session=session) as s:
                    receipt = self._by_key(s, p, "provider_events", event_id)
                    update(receipt, effects_complete=True)
            return result

    def start_transcription_in(self, s, p, call):
        """One inbound stream per leg: the leg that carried speech identifies its speaker.

        Soniox omits `transcription_track`, so a single `both` stream cannot tell the rep from the
        prospect. The rep's agent leg outlives the call, so its stream names the call in client state.
        """
        stream = {"transcription_engine": TRANSCRIPTION_ENGINE, "transcription_tracks": "inbound"}
        self.control_intent(s, p, call.id, "transcription_start", stream)
        agent = call.data.get("agent_provider_id")
        if agent:
            state = json.dumps({"transcript_call_id": call.id, "tenant_id": p["tenant_id"]})
            self.control_intent(
                s,
                p,
                call.id,
                "transcription_start",
                {**stream, "client_state": base64.b64encode(state.encode()).decode()},
                target=agent,
                suffix="transcription_start:agent",
            )
        update(call, transcription_layout="leg_inbound")

    def stop_agent_transcription_in(self, s, p, call):
        """The prospect stream ends with its leg; the persistent agent leg needs an explicit stop."""
        start = self._by_key(s, p, "command_outbox", call.id + ":transcription_start:agent")
        if not start or start.data["status"] == "cancelled":
            return
        if start.data["status"] == "pending" and not start.data.get("sent_at"):
            update(start, status="cancelled", reason="CALL_NO_LONGER_ACTIVE")
            return
        self.control_intent(
            s,
            p,
            call.id,
            "transcription_stop",
            {},
            target=start.data["target"],
            suffix="transcription_stop:agent",
        )

    def finish_call(self, s, p, call, reason=None):
        super().finish_call(s, p, call, reason)
        self.stop_agent_transcription_in(s, p, call)
        for lease in self.scoped(s, "number_leases", p):
            if lease.data.get("call_id") == call.id and lease.data.get("active"):
                update(lease, active=False)
        return self.journal_effect_in(s, p, "call_state", call.id, "receipt", (call,), emergency=True)

    def voicemail_reasons(self, s, p, call, asset, detach=False):
        lead = self.required(s, "leads", call.data["lead_id"], p)
        number = self.required(s, "number_inventory", call.data["number_id"], p)
        campaign = self.required(s, "campaigns", lead.data["campaign_id"], p)
        policy = self.required(s, "policy_versions", campaign.data["policy_id"], p).data
        reasons = []
        # Freeze the original allowance. A policy/list edit cannot manufacture a final attempt.
        ordinal = call.data.get("attempt_ordinal")
        allowance = call.data.get("attempt_allowance")
        if not ordinal or not allowance or ordinal < max(allowance, policy.get("max_attempts", 4)):
            reasons.append("FINAL_ATTEMPT_REQUIRED")
        if not authorize(self.context(s, p, lead, number, permission="prerecorded", active=True)).allowed:
            reasons.append("PRERECORDED_PERMISSION_NOT_APPROVED")
        if (
            not policy.get("voicemail_enabled")
            or not policy.get("voicemail_optout_supported")
            or asset not in policy.get("approved_assets", [])
        ):
            reasons.append("PRERECORDED_PERMISSION_OR_ASSET_NOT_APPROVED")
        if detach and (
            not policy.get("detached_topology_verified") or not policy.get("late_human_recovery_destination")
        ):
            reasons.append("DETACHED_TOPOLOGY_AND_HUMAN_RECOVERY_NOT_VERIFIED")
        return reasons

    def _control_work(self, p):
        self.inbound_watchdog(p)
        with self.db.transaction(False) as s:
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
            ]
            model = MODELS["call_attempts"]
            orphan_vms = [
                doc(r)
                for r in s.scalars(
                    select(model)
                    .where(
                        model.tenant_id == p["tenant_id"],
                        model.seller_id == p["seller_id"],
                        model.op_control_due_at <= self.clock(),
                    )
                    .order_by(model.op_control_due_at, model.id)
                )
            ]
        # Answer precedes its dependent inbound transfer even when timestamps tie.
        commands.sort(key=lambda command: bool(command.get("after_command_id")))
        return commands, orphan_vms

    def _claim_control(self, p, command_id):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            current = self.required(s, "command_outbox", command_id, p)
            s.refresh(current, with_for_update=True)
            command = doc(current)
            if current.data["status"] != "pending":
                return None
            sent = dt(current.data.get("sent_at"))
            if sent and not within_command_window(sent, self.clock()) and not safe_stop(command["action"]):
                update(
                    current,
                    status="review_required",
                    recovery_reason="Original command deduplication window expired before retry submission",
                )
                return None
            inbound = command.get("inbound", False)
            call = self.required(s, "followup_tasks" if inbound else "call_attempts", command["call_id"], p)
            if inbound and not self.inbound_admit_command(s, p, call, current):
                return None
            if command["action"] == "send_dtmf":
                user = get(s, "users", command.get("requested_by"), p["tenant_id"])
                if (
                    not user
                    or user.seller_id != p["seller_id"]
                    or user.data.get("disabled")
                    or (user.id != call.data["rep_id"] and user.data["role"] not in {"owner", "admin"})
                    or call.data.get("detached")
                    or call.data["state"] not in KEYPAD_STATES
                    or command.get("target") != call.data.get("provider_id")
                    or not command.get("expires_at")
                    or dt(command["expires_at"]) <= self.clock()
                ):
                    update(
                        current,
                        status="review_required" if sent else "cancelled",
                        recovery_reason="Keypad context changed or expired; do not replay old digits",
                    )
                    return None
            if (
                not inbound
                and call.data["state"] in TERMINAL | {"ENDING"}
                and not safe_stop(command["action"])
            ):
                update(current, status="cancelled", reason="CALL_NO_LONGER_ACTIVE")
                return None
            if not inbound and command["action"] == "playback_start":
                reasons = self.voicemail_reasons(
                    s,
                    p,
                    call,
                    command["payload"].get("media_name"),
                    call.data.get("detach_requested", False),
                )
                if call.data["state"] != "VM_PENDING":
                    reasons.append("MAILBOX_NOT_READY")
                if reasons:
                    update(current, status="cancelled", reasons=reasons)
                    update(call, voicemail_result="blocked_before_playback")
                    return None
            if command["action"] in {"transcription_start", "record_start"}:
                privacy = self.required(s, "sellers", p["seller_id"], p).data.get("operating_policy", {})
                if command["action"] == "record_start" or (
                    not self.settings.simulation
                    and not (
                        privacy.get("transcription_privacy_verified")
                        and privacy.get("transcription_privacy_reference")
                    )
                ):
                    update(current, status="cancelled", reason="RAW_CAPTURE_PRIVACY_NOT_AUTHORIZED")
                    return None
                lead = self.required(s, "leads", call.data["lead_id"], p)
                number = self.required(s, "number_inventory", call.data["number_id"], p)
                permission = "transcription" if command["action"] == "transcription_start" else "recording"
                if (
                    call.data["state"] in TERMINAL
                    or not authorize(
                        self.context(s, p, lead, number, permission=permission, active=True)
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
                sent_at=current.data.get("sent_at") or self.clock().isoformat(),
                lease_until=(self.clock() + timedelta(seconds=30)).isoformat(),
            )
            for phase in ("intent", "submitted"):
                self.journal_effect_in(
                    s,
                    p,
                    command["action"],
                    current.external_key,
                    phase,
                    [current, call],
                    provider_id=target,
                    source=command["payload"],
                    emergency=safe_stop(command["action"]),
                )
            return command, call.id, inbound, target, owner, call.data.get("provider_id")

    def _control_completed(self, p, claimed, error=None):
        command, call_id, inbound, target, owner, provider_id = claimed
        with self.db.transaction() as s:
            current = self.required(s, "command_outbox", command["id"], p)
            s.refresh(current, with_for_update=True)
            if current.data["status"] != "sending" or not owns_effect(current.data, owner, self.clock()):
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
                self.settings.simulation
                and not inbound
                and command["action"] == "hangup"
                and target == provider_id
            ):
                self.finish_call(
                    s, p, self.required(s, "call_attempts", call_id, p), "simulated_control_hangup"
                )
            self.journal_effect_in(
                s,
                p,
                command["action"],
                current.external_key,
                "receipt",
                [current],
                provider_id=target,
                emergency=True,
            )

    async def dispatch_controls(self, p, *, command_id=None):
        commands, orphan_vms = (
            ([{"id": command_id}], []) if command_id else await asyncio.to_thread(self._control_work, p)
        )
        for call in orphan_vms:
            await self.hangup(p, call["id"])
        for command in commands:
            claimed = await asyncio.to_thread(self._claim_control, p, command["id"])
            if not claimed:
                continue
            command, _, _, target, _, _ = claimed
            try:
                provider = await asyncio.to_thread(self.carrier, p["tenant_id"])
                if command["action"] == "playback_start":
                    await provider.playback(target, command["payload"])
                elif command["action"] == "hangup":
                    await provider.hangup(target, command["payload"]["command_id"])
                else:
                    await provider.action(target, command["action"], command["payload"])
            except Exception as exc:
                await asyncio.to_thread(self._control_completed, p, claimed, exc)
            else:
                await asyncio.to_thread(self._control_completed, p, claimed)
        return {"processed": len(commands), "voicemail_watchdogs": len(orphan_vms)}

    def _simulated_agent(self, p):
        provider_id = "agent-" + uid()
        with self.db.transaction() as s:
            put(
                s,
                "call_legs",
                p["tenant_id"],
                p["seller_id"],
                {
                    "kind": "agent",
                    "rep_id": p["user_id"],
                    "provider_id": provider_id,
                    "state": "answered",
                },
                key=provider_id,
                now=self.clock(),
            )
        return {"agent_call_control_id": provider_id}

    def _agent_context(self, p):
        conf = self.vault.read(p["tenant_id"])
        with self.db.transaction(False) as s:
            user = self.required(s, "users", p["user_id"], p)
            sip = user.data.get("sip_username") or (
                conf.get("sip_username") if p["role"] in {"owner", "admin"} else None
            )
            inventory = [
                n
                for n in self.scoped(s, "number_inventory", p)
                if n.data["lifecycle"] in {"healthy", "limited_real_use"}
            ]
            credential = user.data.get("telephony_credential_id") or (
                conf.get("telephony_credential_id") if p["role"] in {"owner", "admin"} else None
            )
            if not sip or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", sip) or not credential or not inventory:
                fail(503, "Configure a scoped incoming-only SIP credential and an owned healthy caller ID")
        return sip, credential, inventory[0].data["phone"]

    def _claim_agent(self, p, leg_id, owner):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            prior = self.agent_leg_in(s, p, rep_id=p["user_id"], active=True)
            if prior:
                if prior.data.get("provider_id"):
                    return doc(prior), False
                fail(409, "An agent leg is awaiting provider reconciliation; do not create a second leg")
            leg = put(
                s,
                "call_legs",
                p["tenant_id"],
                p["seller_id"],
                {
                    "kind": "agent",
                    "rep_id": p["user_id"],
                    "provider_id": None,
                    "state": "submitting",
                    "command_id": leg_id,
                    "lease_owner": owner,
                    "lease_until": (self.clock() + timedelta(seconds=30)).isoformat(),
                },
                object_id=leg_id,
                now=self.clock(),
            )
            for phase in ("intent", "submitted"):
                self.journal_effect_in(s, p, "agent", leg.id, phase, [leg])
        return doc(leg), True

    def _agent_failed(self, p, leg_id, owner):
        with self.db.transaction() as s:
            current = self.required(s, "call_legs", leg_id, p)
            s.refresh(current, with_for_update=True)
            if current.data["state"] == "submitting" and owns_effect(current.data, owner, self.clock()):
                update(current, state="uncertain", lease_owner=None, lease_until=None)
            if current.data.get("provider_id"):
                return {"agent_call_control_id": current.data["provider_id"]}

    def _agent_completed(self, p, leg_id, owner, receipt):
        with self.db.transaction() as s:
            current = self.required(s, "call_legs", leg_id, p)
            s.refresh(current, with_for_update=True)
            if current.data.get("provider_id") not in {None, receipt["call_control_id"]}:
                fail(409, "Carrier returned a conflicting agent leg reference")
            self.journal_effect_in(
                s,
                p,
                "agent",
                leg_id,
                "receipt",
                [current],
                provider_id=receipt["call_control_id"],
                emergency=True,
            )
            # Signed events can establish or end the leg before the HTTP response.
            if current.data.get("provider_id") == receipt["call_control_id"] and not current.data.get(
                "lease_owner"
            ):
                return {"agent_call_control_id": receipt["call_control_id"]}
            if current.data["state"] != "submitting" or not owns_effect(current.data, owner, self.clock()):
                fail(409, "Agent reconciliation owns the result; refresh its current status")
            update(
                current,
                provider_id=receipt["call_control_id"],
                lease_owner=None,
                lease_until=None,
                state="ringing"
                if current.data["state"] in {"submitting", "uncertain"}
                else current.data["state"],
            )
        return {"agent_call_control_id": receipt["call_control_id"]}

    async def verified_credential(self, carrier, credential, sip):
        from app.providers.telnyx import CarrierConfigurationError

        try:
            return await carrier.verify_incoming_only_credential(credential, sip_username=sip)
        except CarrierConfigurationError as exc:
            fail(503, {"code": "TELEPHONY_CREDENTIAL_MISCONFIGURED", "message": str(exc)})

    async def create_agent(self, p):
        if self.settings.simulation:
            return await asyncio.to_thread(self._simulated_agent, p)
        sip, credential, phone = await asyncio.to_thread(self._agent_context, p)
        carrier = await asyncio.to_thread(self.carrier, p["tenant_id"])
        await self.verified_credential(carrier, credential, sip)
        leg_id, owner = uid(), uid()
        leg, created = await asyncio.to_thread(self._claim_agent, p, leg_id, owner)
        if not created:
            return {"agent_call_control_id": leg["provider_id"]}
        try:
            receipt = await carrier.dial(
                {
                    "to": f"sip:{sip}@sip.telnyx.com",
                    "from": phone,
                    "time_limit_secs": self.settings.max_call_seconds,
                    "command_id": leg_id,
                    "client_state": base64.b64encode(
                        json.dumps({"agent_leg_id": leg_id, "tenant_id": p["tenant_id"]}).encode()
                    ).decode(),
                }
            )
        except Exception:
            result = await asyncio.to_thread(self._agent_failed, p, leg_id, owner)
            if result:
                return result
            fail(503, "Agent submission is uncertain; await its signed receipt or reconcile before retrying")
        return await asyncio.to_thread(self._agent_completed, p, leg_id, owner, receipt)

    def agent_event(self, tenant_id, payload, event_type, *, session=None):
        provider_id = payload.get("call_control_id")
        if not isinstance(provider_id, str) or not provider_id:
            return False
        with self.db.transaction(session=session) as s:
            leg = self.agent_leg_in(s, {"tenant_id": tenant_id}, provider_id=provider_id)
            if not leg:
                try:
                    context = json.loads(base64.b64decode(payload.get("client_state", "")))
                    if not isinstance(context, dict) or context.get("tenant_id") != tenant_id:
                        return False
                    leg = self.agent_leg_in(s, {"tenant_id": tenant_id}, leg_id=context.get("agent_leg_id"))
                except (ValueError, TypeError):
                    return False
            if not leg or leg.data.get("provider_id") not in {None, provider_id}:
                return False
            update(
                leg,
                provider_id=payload.get("call_control_id"),
                lease_owner=None,
                lease_until=None,
                state="ended"
                if leg.data["state"] == "ended"
                else "answered"
                if event_type == "call.answered"
                else "ended"
                if event_type == "call.hangup"
                else leg.data["state"],
            )
            if event_type == "call.hangup":
                user = get(s, "users", leg.data["rep_id"], tenant_id)
                if user and user.data.get("presence", {}).get("agent_call_control_id") in {None, provider_id}:
                    update(
                        user, presence={"status": "paused", "audio_ready": False, "telephony_ready": False}
                    )
            self.journal_effect_in(
                s,
                {"tenant_id": tenant_id, "seller_id": leg.seller_id},
                "agent",
                leg.id,
                "receipt",
                (leg,),
                provider_id=provider_id,
                emergency=True,
            )
            return True
