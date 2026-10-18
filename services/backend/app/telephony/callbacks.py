"""Bounded inbound transfers with an honest message/task fallback, using the existing outbox."""

import asyncio
import base64
import ipaddress
import json
import re
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select

from app.compliance.operating import COSTS
from app.db import ActiveClaim, audit, doc, emit, get, put, rows, uid, update
from app.service import dt, fail

DEFAULT_MESSAGE = (
    "We cannot take your call right now. Your return call has been logged for review. "
    "Please contact the business directly if your request is urgent. Goodbye."
)
DEFAULTS = {"version": 0, "destination": "", "ring_seconds": 20, "offline_reference": ""}
ENDED = {"ended", "cancelled", "failed"}


def valid_destination(value):
    if not value:
        return value
    if re.fullmatch(r"\+1[2-9]\d{9}", value) and value[2:5] not in {"900", "976"}:
        return value
    match = re.fullmatch(r"sip:[A-Za-z0-9_.+-]{1,128}@([A-Za-z0-9.-]{3,253})(?:;secure=srtp)?", value)
    if match:
        host = match[1].lower()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if not address and "." in host and not host.endswith((".local", ".localhost", ".internal")):
            return value
    raise ValueError("Use a public business SIP address or a non-premium US E.164 destination")


class CallbackRoutingEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    destination: str | None = Field(default=None, max_length=400)
    ring_seconds: int | None = Field(default=None, ge=5, le=30)
    offline_reference: str | None = Field(default=None, max_length=500)

    @field_validator("destination")
    @classmethod
    def destination_valid(cls, value):
        return valid_destination(value) if value is not None else value


class InboundCallbacks:
    def callback_config(self, s, p):
        seller = self.required(s, "sellers", p["seller_id"], p)
        config = {**DEFAULTS, **seller.data.get("callback_routing", {})}
        # Single-seller legacy settings can migrate without assigning another seller's destination.
        if not seller.data.get("callback_routing") and len(rows(s, "sellers", p["tenant_id"])) == 1:
            legacy = self.vault.read(p["tenant_id"]).get("inbound_fallback_destination", "")
            try:
                config["destination"] = valid_destination(legacy)
            except ValueError:
                pass
        return config

    def read_callback_routing(self, p):
        with self.db.transaction(False) as s:
            return {
                **self.callback_config(s, p),
                "offline_status": "operator_configuration_required",
                "default_message": DEFAULT_MESSAGE,
                "costs": COSTS["callbacks"],
            }

    def edit_callback_routing(self, p, data):
        with self.db.transaction() as s:
            seller = self.required(s, "sellers", p["seller_id"], p)
            current = self.callback_config(s, p)
            if data.pop("expected_version") != current["version"]:
                fail(409, "Callback routing changed; refresh before saving")
            values = {**current, **data, "version": current["version"] + 1}
            if values["destination"] in {n.data["phone"] for n in self.scoped(s, "number_inventory", p)}:
                fail(422, "Fallback cannot loop back to this seller's inbound numbers")
            update(seller, callback_routing=values)
            audit(
                s, p["tenant_id"], p["seller_id"], p["user_id"], "callback.routing", seller.id, self.clock()
            )
            return {
                **values,
                "offline_status": "operator_configuration_required",
                "default_message": DEFAULT_MESSAGE,
                "costs": COSTS["callbacks"],
            }

    def _inbound_command(
        self, s, p, task, action, payload=None, *, target=None, suffix=None, after=None, attempt=None
    ):
        command = self.control_intent(
            s,
            p,
            task.id,
            action,
            payload or {},
            target=target or task.data["provider_id"],
            suffix=suffix,
        )
        update(command, inbound=True, after_command_id=after, inbound_attempt=attempt)
        return command

    def _release_inbound_claim(self, s, task):
        s.execute(delete(ActiveClaim).where(ActiveClaim.call_id == task.id))
        s.flush()

    def _route_inbound(self, s, p, task, *, after=None):
        attempts = [dict(a) for a in task.data.get("routing_attempts", [])]
        tried = {a.get("rep_id") for a in attempts}
        claims = list(s.scalars(select(ActiveClaim).where(ActiveClaim.tenant_id == p["tenant_id"])))
        lead = get(s, "leads", task.data.get("lead_id"), p["tenant_id"]) if task.data.get("lead_id") else None
        candidates = []
        for user in self.scoped(s, "users", p):
            presence = user.data.get("presence", {})
            at = dt(presence.get("at"))
            if (
                user.id not in tried
                and not user.data.get("disabled")
                and re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", user.data.get("sip_username", ""))
                and presence.get("status") == "ready"
                and presence.get("audio_ready")
                and presence.get("telephony_ready")
                and at
                and 0 <= (self.clock() - at).total_seconds() <= 90
                and not any(
                    c.rep_id == user.id or (lead and c.endpoint_id == lead.data["endpoint_id"])
                    for c in claims
                )
            ):
                candidates.append(user)
        candidates.sort(key=lambda u: (u.id != task.data.get("responsible_rep_id"), u.id))
        rep = candidates[0] if candidates and len(attempts) < 3 else None
        config = task.data["routing_config"]
        target = (
            f"sip:{rep.data['sip_username']}@sip.telnyx.com;secure=srtp" if rep else config["destination"]
        )
        if not rep and any(a.get("kind") == "business_fallback" for a in attempts):
            target = ""
        if target in {task.data.get("callback_phone"), task.data.get("called_phone")}:
            target = ""  # never bounce the caller back to themselves or recurse through this DID
        route = (
            "responsible_rep"
            if rep and rep.id == task.data.get("responsible_rep_id")
            else "teammate"
            if rep
            else "business_fallback"
            if target
            else "callback_queue"
        )
        update(task, route=route, routed_rep_id=rep.id if rep else None)
        if target:
            identity = uid()
            context = base64.b64encode(
                json.dumps({"inbound_task": task.id, "attempt": identity}).encode()
            ).decode()
            attempt = {
                "id": identity,
                "rep_id": rep.id if rep else None,
                "kind": route,
                "state": "prepared",
                "provider_id": None,
            }
            attempts.append(attempt)
            update(task, routing_attempts=attempts)
            if rep:
                s.add(
                    ActiveClaim(
                        tenant_id=p["tenant_id"],
                        seller_id=p["seller_id"],
                        endpoint_id=lead.data["endpoint_id"] if lead else task.id,
                        lead_id=lead.id if lead else task.id,
                        rep_id=rep.id,
                        number_id=task.data["number_id"],
                        reservation_id=task.id,
                        call_id=task.id,
                        expires_at=self.clock() + timedelta(seconds=self.settings.max_call_seconds),
                    )
                )
            self._inbound_command(
                s,
                p,
                task,
                "transfer",
                {
                    "to": target,
                    "from": task.data["called_phone"],
                    "timeout_secs": config["ring_seconds"],
                    "time_limit_secs": self.settings.max_call_seconds,
                    "target_leg_client_state": context,
                },
                suffix="inbound-transfer:" + identity,
                after=after,
                attempt=identity,
            )
        else:
            self._inbound_command(
                s,
                p,
                task,
                "speak",
                {
                    "payload": DEFAULT_MESSAGE,
                    "payload_type": "text",
                    "voice": "female",
                    "language": "en-US",
                    "service_level": "basic",
                    "target_legs": "self",
                },
                suffix="inbound-message",
                after=after,
            )

    async def inbound_callback(self, tenant_id, payload, event_id):
        return await asyncio.to_thread(self._receive_inbound, tenant_id, payload, event_id)

    def _receive_inbound(self, tenant_id, payload, event_id):
        if not payload.get("call_control_id"):
            fail(422, "Inbound call identity is missing")
        with self.db.transaction() as s:
            owned = [
                n for n in rows(s, "number_inventory", tenant_id) if n.data["phone"] == payload.get("to")
            ]
            if len(owned) != 1:
                return {"received": True, "route": "unrecognized_owned_did"}
            number = owned[0]
            p = {"tenant_id": tenant_id, "seller_id": number.seller_id, "user_id": "inbound"}
            self.db.authorize_stream(s, p)
            existing = next(
                (
                    t
                    for t in self.scoped(s, "followup_tasks", p)
                    if t.data.get("inbound") and t.data.get("provider_id") == payload["call_control_id"]
                ),
                None,
            )
            if existing:
                return doc(existing)
            leads = [lead for lead in self.scoped(s, "leads", p) if lead.data["phone"] == payload.get("from")]
            lead = leads[0] if len(leads) == 1 else None
            previous = max(
                (c for c in self.scoped(s, "call_attempts", p) if lead and c.data["lead_id"] == lead.id),
                key=lambda c: c.data["started_at"],
                default=None,
            )
            responsible = previous.data["rep_id"] if previous else None
            task = put(
                s,
                "followup_tasks",
                tenant_id,
                number.seller_id,
                {
                    "provider_id": payload["call_control_id"],
                    "number_id": number.id,
                    "seller_id": number.seller_id,
                    "inbound": True,
                    "inbound_state": "answering",
                    "caller_ended": False,
                    "called_phone": number.data["phone"],
                    "callback_phone": payload.get("from"),
                    "lead_id": lead.id if lead else None,
                    "ambiguous": len(leads) > 1,
                    "previous_call_id": previous.id if previous else None,
                    "responsible_rep_id": responsible,
                    "owner_id": responsible,
                    "status": "open",
                    "action": "Review return call",
                    "due_at": self.clock().isoformat(),
                    "event_id": event_id,
                    "routing_config": self.callback_config(s, p),
                    "routing_attempts": [],
                    "inbound_deadline": (self.clock() + timedelta(seconds=40)).isoformat(),
                },
                key="callback:" + payload["call_control_id"],
                now=self.clock(),
            )
            answer = self._inbound_command(s, p, task, "answer", suffix="inbound-answer")
            self._route_inbound(s, p, task, after=answer.id)
            emit(s, tenant_id, "task.updated", task, self.clock())
            return doc(task)

    def _inbound_finished(self, s, p, task):
        if task.data.get("caller_ended") and all(
            a["state"] in ENDED for a in task.data.get("routing_attempts", [])
        ):
            update(task, inbound_state="ended", ended_at=self.clock().isoformat())
            self._release_inbound_claim(s, task)
        emit(s, p["tenant_id"], "task.updated", task, self.clock())

    def inbound_event(self, tenant_id, payload, event_type):
        with self.db.transaction() as s:
            tasks = [t for t in rows(s, "followup_tasks", tenant_id) if t.data.get("inbound")]
            task = next(
                (t for t in tasks if t.data.get("provider_id") == payload.get("call_control_id")), None
            )
            attempt_id = None
            if not task:
                try:
                    context = json.loads(base64.b64decode(payload.get("client_state", "")))
                    if not isinstance(context, dict):
                        return False
                    task = next(
                        (
                            t
                            for t in tasks
                            if t.id == context.get("inbound_task")
                            and any(
                                a["id"] == context.get("attempt") for a in t.data.get("routing_attempts", [])
                            )
                        ),
                        None,
                    )
                    attempt_id = context.get("attempt")
                except (ValueError, TypeError):
                    return False
            if not task:
                return False
            p = {
                "tenant_id": tenant_id,
                "seller_id": task.seller_id,
                "user_id": task.data.get("owner_id") or "inbound",
            }
            self.db.authorize_stream(s, p)
            attempts = [dict(a) for a in task.data.get("routing_attempts", [])]
            if attempt_id:
                attempt = next(a for a in attempts if a["id"] == attempt_id)
                if attempt.get("provider_id") not in {None, payload.get("call_control_id")}:
                    return False
                attempt["provider_id"] = payload["call_control_id"]
                if attempt["state"] in ENDED:
                    return True
                if event_type == "call.hangup":
                    connected = attempt["state"] == "connected"
                    attempt["state"] = "ended"
                    update(task, routing_attempts=attempts)
                    self._release_inbound_claim(s, task)
                    if not task.data.get("caller_ended"):
                        if connected:
                            self._inbound_command(s, p, task, "hangup", suffix="inbound-complete")
                            update(task, inbound_state="ending")
                        else:
                            self._route_inbound(s, p, task)
                elif task.data.get("caller_ended") or task.data.get("inbound_state") in {"ending", "ended"}:
                    update(task, routing_attempts=attempts)
                    self._inbound_command(
                        s,
                        p,
                        task,
                        "hangup",
                        target=payload["call_control_id"],
                        suffix="inbound-leg-end:" + attempt_id,
                    )
                elif event_type == "call.bridged":
                    attempt["state"] = "connected"
                    update(
                        task,
                        routing_attempts=attempts,
                        inbound_state="connected",
                        connected_at=self.clock().isoformat(),
                        inbound_deadline=(
                            self.clock() + timedelta(seconds=self.settings.max_call_seconds)
                        ).isoformat(),
                    )
                    if (
                        attempt.get("rep_id")
                        and not task.data.get("human_edited")
                        and task.data["status"] not in {"completed", "cancelled"}
                    ):
                        update(
                            task,
                            status="completed",
                            owner_id=attempt["rep_id"],
                            completion_source="inbound_rep_connected",
                            completed_at=self.clock().isoformat(),
                        )
                        for promised in self.scoped(s, "followup_tasks", p):
                            if (
                                promised.data.get("kind") == "callback"
                                and promised.data.get("call_id") == task.data.get("previous_call_id")
                                and promised.data.get("lead_id") == task.data.get("lead_id")
                                and promised.data.get("status") in {"open", "snoozed"}
                                and not promised.data.get("human_edited")
                            ):
                                update(
                                    promised,
                                    status="completed",
                                    completion_source="inbound_rep_connected",
                                    inbound_task_id=task.id,
                                )
                                emit(s, tenant_id, "task.updated", promised, self.clock())
                        if task.data.get("lead_id"):
                            lead = self.required(s, "leads", task.data["lead_id"], p)
                            self._sync_callback(s, p, lead)
                            self.refresh_packet_in(s, p, lead)
                else:
                    update(task, routing_attempts=attempts)
            elif event_type == "call.answered" and task.data["inbound_state"] == "answering":
                for command in self.scoped(s, "command_outbox", p):
                    if command.data.get("call_id") == task.id and command.data["action"] == "answer":
                        update(
                            command,
                            status="completed",
                            lease_owner=None,
                            lease_until=None,
                            confirmation="signed_answer_event",
                        )
            elif event_type == "call.hangup":
                update(task, caller_ended=True, inbound_state="ending")
                for a in attempts:
                    if a["state"] == "prepared":
                        a["state"] = "cancelled"
                    elif a.get("provider_id") and a["state"] not in ENDED:
                        self._inbound_command(
                            s, p, task, "hangup", target=a["provider_id"], suffix="inbound-leg-end:" + a["id"]
                        )
                update(task, routing_attempts=attempts)
                for command in self.scoped(s, "command_outbox", p):
                    if (
                        command.data.get("call_id") == task.id
                        and command.data["status"] == "pending"
                        and command.data["action"] != "hangup"
                    ):
                        update(command, status="cancelled", reason="caller_ended")
            elif event_type == "call.speak.ended" and task.data["inbound_state"] == "notifying":
                self._inbound_command(s, p, task, "hangup", suffix="inbound-message-end")
                update(task, inbound_state="ending")
            self._inbound_finished(s, p, task)
            return True

    def end_inbound(self, p, task_id):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            task = self.required(s, "followup_tasks", task_id, p)
            if not task.data.get("inbound"):
                fail(404, "Inbound call not found")
            if p.get("role") == "rep" and p["user_id"] != task.data.get("routed_rep_id"):
                fail(403, "This return call belongs to another rep")
            if task.data["inbound_state"] != "ended":
                update(task, inbound_state="ending")
                self._inbound_command(s, p, task, "hangup", suffix="inbound-requested-end")
                for attempt in task.data.get("routing_attempts", []):
                    if attempt.get("provider_id") and attempt["state"] not in ENDED:
                        self._inbound_command(
                            s,
                            p,
                            task,
                            "hangup",
                            target=attempt["provider_id"],
                            suffix="inbound-leg-end:" + attempt["id"],
                        )
                emit(s, p["tenant_id"], "task.updated", task, self.clock())
            return doc(task)

    async def reconcile_inbound(self, p):
        def pending():
            with self.db.transaction() as s:
                pending_tasks = []
                for task in self.scoped(s, "followup_tasks", p):
                    if not task.data.get("inbound") or task.data.get("inbound_state") == "ended":
                        continue
                    last = dt(task.data.get("last_reconciled_at"))
                    if last and (self.clock() - last).total_seconds() < 30:
                        continue
                    update(task, last_reconciled_at=self.clock().isoformat())
                    pending_tasks.append(doc(task))
                return pending_tasks

        for task in await asyncio.to_thread(pending):
            legs = [{"provider_id": task["provider_id"]}, *task.get("routing_attempts", [])]
            for leg in legs:
                identity = leg.get("provider_id")
                if not identity or leg.get("state") in ENDED:
                    continue
                receipt = await self.carrier(p["tenant_id"]).reconcile(identity, identity)
                if (
                    receipt
                    and receipt.get("call_control_id", identity) == identity
                    and (
                        receipt.get("is_alive") is False
                        or receipt.get("state") in {"hangup", "ended", "completed"}
                    )
                ):
                    payload = {"call_control_id": identity}
                    if leg.get("id"):
                        payload["client_state"] = base64.b64encode(
                            json.dumps({"inbound_task": task["id"], "attempt": leg["id"]}).encode()
                        ).decode()
                    await asyncio.to_thread(self.inbound_event, p["tenant_id"], payload, "call.hangup")

    def inbound_watchdog(self, p):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            for task in self.scoped(s, "followup_tasks", p):
                deadline = dt(task.data.get("inbound_deadline"))
                if (
                    task.data.get("inbound")
                    and task.data.get("inbound_state") not in {"ended", "ending"}
                    and deadline
                    and deadline <= self.clock()
                ):
                    update(
                        task,
                        inbound_state="ending",
                        review_required=True,
                        recovery_reason="No terminal routing receipt before deadline",
                    )
                    self._inbound_command(s, p, task, "hangup", suffix="inbound-watchdog")
                    for attempt in task.data.get("routing_attempts", []):
                        if attempt.get("provider_id") and attempt["state"] not in ENDED:
                            self._inbound_command(
                                s,
                                p,
                                task,
                                "hangup",
                                target=attempt["provider_id"],
                                suffix="inbound-leg-end:" + attempt["id"],
                            )

    def inbound_admit_command(self, s, p, task, command):
        data = command.data
        if data["action"] != "hangup" and task.data.get("inbound_state") in {"ending", "ended"}:
            update(command, status="cancelled", reason="inbound_terminal")
            return False
        if data.get("after_command_id"):
            parent = self.required(s, "command_outbox", data["after_command_id"], p)
            if parent.data["status"] != "completed":
                return False
        if data["action"] == "transfer":
            attempts = [dict(a) for a in task.data["routing_attempts"]]
            attempt = next(a for a in attempts if a["id"] == data["inbound_attempt"])
            if attempt["state"] in ENDED:
                update(command, status="cancelled", reason="transfer_already_ended")
                return False
            if attempt.get("rep_id") and attempt["state"] == "prepared":
                user = self.required(s, "users", attempt["rep_id"], p)
                presence = user.data.get("presence", {})
                at = dt(presence.get("at"))
                if (
                    user.data.get("disabled")
                    or presence.get("status") != "ready"
                    or not presence.get("audio_ready")
                    or not presence.get("telephony_ready")
                    or not at
                    or not 0 <= (self.clock() - at).total_seconds() <= 90
                ):
                    attempt["state"] = "cancelled"
                    update(task, routing_attempts=attempts)
                    update(command, status="cancelled", reason="rep_unavailable_before_transfer")
                    self._release_inbound_claim(s, task)
                    self._route_inbound(s, p, task)
                    return False
            attempt["state"] = "sending"
            update(
                task,
                routing_attempts=attempts,
                inbound_state="ringing",
                inbound_deadline=(
                    (dt(data.get("sent_at")) or self.clock())
                    + timedelta(seconds=task.data["routing_config"]["ring_seconds"] + 10)
                ).isoformat(),
            )
        elif data["action"] == "speak":
            update(
                task,
                inbound_state="notifying",
                inbound_deadline=(
                    (dt(data.get("sent_at")) or self.clock()) + timedelta(seconds=30)
                ).isoformat(),
            )
        if data["action"] in {"transfer", "speak"}:
            estimate = (
                COSTS["callbacks"]["transfer_per_invocation_usd"]
                if data["action"] == "transfer"
                else len(DEFAULT_MESSAGE) * COSTS["callbacks"]["standard_tts_per_character_usd"]
            )
            key = "inbound:" + command.id
            if not self.reserve_budget_in(
                s, p, key, estimate, object_id=task.id, work_kind="callback_" + data["action"]
            ):
                update(command, status="cancelled", reason="callback_budget_exhausted")
                attempts = [dict(a) for a in task.data.get("routing_attempts", [])]
                for attempt in attempts:
                    if attempt["id"] == data.get("inbound_attempt"):
                        attempt["state"] = "cancelled"
                update(task, routing_attempts=attempts)
                self._release_inbound_claim(s, task)
                update(
                    task,
                    inbound_state="ending",
                    review_required=True,
                    recovery_reason="Optional routing budget exhausted; call ended and return task retained",
                )
                self._inbound_command(s, p, task, "hangup", suffix="inbound-budget-end")
                return False
            hold = next(r for r in self.scoped(s, "usage_ledger", p) if r.data.get("request_key") == key)
            update(
                hold,
                provider="telnyx",
                status="uncertain",
                kind="callback_" + data["action"],
                submitted_at=self.clock().isoformat(),
            )
        return True
