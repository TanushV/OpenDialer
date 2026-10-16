import base64
import json
import re

from sqlalchemy import delete

from app.compliance.policy import authorize
from app.db import ActiveClaim, audit, doc, put, rows, uid, update
from app.service import dt, fail
from app.telephony.reducer import TERMINAL


class TelephonyWorkflows:
    def control_intent(self, s, p, call_id, action, payload, *, target=None, suffix=None):
        key = f"{call_id}:{suffix or action}"
        prior = next((r for r in self.scoped(s, "command_outbox", p) if r.external_key == key), None)
        if prior:
            return prior
        return put(
            s,
            "command_outbox",
            p["tenant_id"],
            p["seller_id"],
            {
                "kind": "action",
                "call_id": call_id,
                "action": action,
                "target": target,
                "payload": {**payload, "command_id": key},
                "status": "pending",
            },
            key=key,
            now=self.clock(),
        )

    def event(self, p, event_id, event_type, call_id, payload=None):
        result = super().event(p, event_id, event_type, call_id, payload)
        if result["duplicate"]:
            return result
        payload = payload or {}
        optout_phone = None
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            lead = self.required(s, "leads", call.data["lead_id"], p)
            campaign = self.required(s, "campaigns", lead.data["campaign_id"], p)
            policy = self.required(s, "policy_versions", campaign.data["policy_id"], p).data
            number = self.required(s, "number_inventory", call.data["number_id"], p)
            if event_type == "call.answered":
                for permission, action, command in [
                    (
                        "transcription",
                        "transcription_start",
                        {"transcription_engine": "Soniox", "transcription_tracks": "both"},
                    ),
                    ("recording", "record_start", {"format": "mp3", "channels": "dual"}),
                ]:
                    decision = authorize(self.context(s, p, lead, number, permission=permission, active=True))
                    if decision.allowed:
                        self.control_intent(s, p, call_id, action, command)
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
                    update(call, detached=True)
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
                        self.control_intent(s, p, call_id, "hangup", {}, target=agent, suffix="release-agent")
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
            if event_type.endswith(":human") and call.data.get("detached"):
                self.control_intent(s, p, call_id, "playback_stop", {}, suffix="late-human-stop")
                recovery = policy.get("late_human_recovery_destination")
                if recovery:
                    self.control_intent(
                        s, p, call_id, "transfer", {"to": recovery}, suffix="late-human-recovery"
                    )
                else:
                    self.control_intent(s, p, call_id, "hangup", {}, suffix="late-human-no-destination")
                update(call, late_human=True, recovery_required=True)
            if (
                event_type == "call.gather.ended"
                and call.data.get("voicemail_asset")
                and call.data["state"] not in TERMINAL
            ):
                if payload.get("digits") == "9":
                    optout_phone = lead.data["phone"]
                    self.control_intent(s, p, call_id, "playback_stop", {}, suffix="optout-stop")
                    self.control_intent(s, p, call_id, "hangup", {}, suffix="optout-hangup")
                    self.finish_call(s, p, call, "voicemail_optout")
                else:
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
            if event_type in {"call.playback.ended", "call.playback.failed"}:
                self.control_intent(s, p, call_id, "hangup", {}, suffix="voicemail-completion")
                update(
                    call,
                    voicemail_result="playback_failed"
                    if event_type.endswith("failed")
                    else "playback_completed_not_mailbox_receipt",
                )
            if (
                event_type == "call.dtmf.received"
                and payload.get("digit") == "9"
                and call.data.get("voicemail_asset")
            ):
                optout_phone = lead.data["phone"]
                self.control_intent(s, p, call_id, "playback_stop", {}, suffix="optout-stop")
                self.control_intent(s, p, call_id, "hangup", {}, suffix="optout-hangup")
                self.finish_call(s, p, call, "voicemail_optout")
            if event_type == "call.recording.saved":
                urls = payload.get("recording_urls", {})
                update(
                    call,
                    recording_url=urls.get("mp3") or urls.get("wav"),
                    recording_id=payload.get("recording_id"),
                )
        if optout_phone:
            self.suppress(p, optout_phone, "opt_out")
        return result

    def finish_call(self, s, p, call, reason=None):
        super().finish_call(s, p, call, reason)
        for lease in self.scoped(s, "number_leases", p):
            if lease.data.get("call_id") == call.id and lease.data.get("active"):
                update(lease, active=False)

    async def dispatch_controls(self, p):
        with self.db.transaction(False) as s:
            commands = [
                doc(r)
                for r in self.scoped(s, "command_outbox", p)
                if r.data["kind"] == "action" and r.data["status"] in {"pending", "uncertain"}
            ]
            orphan_vms = [
                doc(r)
                for r in self.scoped(s, "call_attempts", p)
                if r.data.get("voicemail_deadline")
                and r.data["state"] not in TERMINAL
                and dt(r.data["voicemail_deadline"]) <= self.clock()
            ]
        for call in orphan_vms:
            await self.hangup(p, call["id"])
        for command in commands:
            with self.db.transaction() as s:
                current = self.required(s, "command_outbox", command["id"], p)
                if current.data["status"] not in {"pending", "uncertain"}:
                    continue
                inbound = command.get("inbound", False)
                call = self.required(
                    s, "followup_tasks" if inbound else "call_attempts", command["call_id"], p
                )
                if command["action"] in {"transcription_start", "record_start"}:
                    lead = self.required(s, "leads", call.data["lead_id"], p)
                    number = self.required(s, "number_inventory", call.data["number_id"], p)
                    permission = (
                        "transcription" if command["action"] == "transcription_start" else "recording"
                    )
                    if (
                        call.data["state"] in TERMINAL
                        or not authorize(
                            self.context(s, p, lead, number, permission=permission, active=True)
                        ).allowed
                    ):
                        update(current, status="cancelled")
                        continue
                target = command.get("target") or call.data.get("provider_id")
                if not target:
                    continue
                update(current, status="sending")
            try:
                provider = self.carrier(p["tenant_id"])
                if command["action"] == "playback_start":
                    await provider.playback(target, command["payload"])
                elif command["action"] == "hangup":
                    await provider.hangup(target, command["payload"]["command_id"])
                else:
                    await provider.action(target, command["action"], command["payload"])
                with self.db.transaction() as s:
                    update(self.required(s, "command_outbox", command["id"], p), status="completed")
                    if (
                        self.settings.simulation
                        and not inbound
                        and command["action"] == "hangup"
                        and target == call.data.get("provider_id")
                    ):
                        self.finish_call(
                            s, p, self.required(s, "call_attempts", call.id, p), "simulated_control_hangup"
                        )
            except Exception as exc:
                with self.db.transaction() as s:
                    update(
                        self.required(s, "command_outbox", command["id"], p),
                        status="uncertain",
                        error=type(exc).__name__,
                    )
        return {"processed": len(commands), "voicemail_watchdogs": len(orphan_vms)}

    async def create_agent(self, p):
        if self.settings.simulation:
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
        carrier = self.carrier(p["tenant_id"])
        await carrier.verify_incoming_only_credential(credential)
        leg_id = uid()
        with self.db.transaction() as s:
            prior = next(
                (
                    r
                    for r in self.scoped(s, "call_legs", p)
                    if r.data.get("kind") == "agent"
                    and r.data.get("rep_id") == p["user_id"]
                    and r.data.get("state") in {"submitting", "uncertain", "ringing", "answered"}
                ),
                None,
            )
            if prior:
                if prior.data.get("provider_id"):
                    return {"agent_call_control_id": prior.data["provider_id"]}
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
                },
                object_id=leg_id,
                now=self.clock(),
            )
        try:
            receipt = await carrier.dial(
                {
                    "to": f"sip:{sip}@sip.telnyx.com;secure=srtp",
                    "from": inventory[0].data["phone"],
                    "time_limit_secs": self.settings.max_call_seconds,
                    "command_id": leg_id,
                    "client_state": base64.b64encode(
                        json.dumps({"agent_leg_id": leg_id, "tenant_id": p["tenant_id"]}).encode()
                    ).decode(),
                }
            )
        except Exception:
            with self.db.transaction() as s:
                update(self.required(s, "call_legs", leg.id, p), state="uncertain")
            fail(503, "Agent submission is uncertain; await its signed receipt or reconcile before retrying")
        with self.db.transaction() as s:
            update(
                self.required(s, "call_legs", leg.id, p),
                provider_id=receipt["call_control_id"],
                state="ringing",
            )
        return {"agent_call_control_id": receipt["call_control_id"]}

    def agent_event(self, tenant_id, payload, event_type):
        with self.db.transaction() as s:
            legs = rows(s, "call_legs", tenant_id)
            leg = next(
                (
                    r
                    for r in legs
                    if r.data.get("kind") == "agent"
                    and r.data.get("provider_id") == payload.get("call_control_id")
                ),
                None,
            )
            if not leg:
                try:
                    context = json.loads(base64.b64decode(payload.get("client_state", "")))
                    leg = next((r for r in legs if r.id == context.get("agent_leg_id")), None)
                except (ValueError, TypeError):
                    return False
            if not leg:
                return False
            update(
                leg,
                provider_id=payload.get("call_control_id"),
                state="answered"
                if event_type == "call.answered"
                else "ended"
                if event_type == "call.hangup"
                else leg.data["state"],
            )
            if event_type == "call.hangup":
                user = next((u for u in rows(s, "users", tenant_id) if u.id == leg.data["rep_id"]), None)
                if user:
                    update(
                        user, presence={"status": "paused", "audio_ready": False, "telephony_ready": False}
                    )
            return True

    async def inbound_callback(self, tenant_id, payload, event_id):
        with self.db.transaction() as s:
            number = next(
                (n for n in rows(s, "number_inventory", tenant_id) if n.data["phone"] == payload.get("to")),
                None,
            )
            if not number:
                return {"received": True, "route": "unrecognized_owned_did"}
            seller_id = number.seller_id
            leads = [
                lead_row
                for lead_row in rows(s, "leads", tenant_id, seller_id)
                if lead_row.data["phone"] == payload.get("from")
            ]
            lead = leads[0] if len(leads) == 1 else None
            calls = [
                c
                for c in rows(s, "call_attempts", tenant_id, seller_id)
                if lead and c.data["lead_id"] == lead.id
            ]
            previous = max(calls, key=lambda c: c.data["started_at"], default=None)
            users = rows(s, "users", tenant_id, seller_id)
            responsible = next((u for u in users if previous and u.id == previous.data["rep_id"]), None)
            route = "callback_queue"
            if (
                responsible
                and responsible.data.get("presence", {}).get("status") == "ready"
                and responsible.data.get("sip_username")
            ):
                route = "responsible_rep"
            data = {
                "provider_id": payload.get("call_control_id"),
                "number_id": number.id,
                "inbound": True,
                "seller_id": seller_id,
                "lead_id": lead.id if lead else None,
                "route": route,
                "ambiguous": len(leads) > 1,
                "status": "open",
                "action": "Return inbound callback",
                "owner_id": responsible.id if responsible else None,
                "due_at": self.clock().isoformat(),
                "event_id": event_id,
            }
            existing = next(
                (
                    t
                    for t in rows(s, "followup_tasks", tenant_id, seller_id)
                    if t.external_key == "callback:" + event_id
                ),
                None,
            )
            if existing:
                return doc(existing)
            task = put(
                s, "followup_tasks", tenant_id, seller_id, data, key="callback:" + event_id, now=self.clock()
            )
            if route == "responsible_rep":
                p = {"tenant_id": tenant_id, "seller_id": seller_id}
                answer = self.control_intent(
                    s,
                    p,
                    task.id,
                    "answer",
                    {},
                    target=payload.get("call_control_id"),
                    suffix="inbound-answer",
                )
                update(answer, inbound=True)
                # A callback is scoped to the called seller before matching the caller.
                transfer = self.control_intent(
                    s,
                    p,
                    task.id,
                    "transfer",
                    {"to": f"sip:{responsible.data['sip_username']}@sip.telnyx.com"},
                    target=payload.get("call_control_id"),
                    suffix="inbound-transfer",
                )
                update(transfer, inbound=True)
            return doc(task)
