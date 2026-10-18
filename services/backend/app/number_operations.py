"""Reconcile owned inventory and one durable configuration intent per number/route."""

import asyncio
import hashlib
import re
from datetime import timedelta

from sqlalchemy import select

from app.compliance.phone import normalize_phone
from app.db import MODELS, audit, doc, emit, put, rows, uid, update
from app.service import dt, fail


class NumberOperations:
    def _configuration_actor(self, s, p):
        user = self.required(s, "users", p["user_id"], p)
        if user.data.get("disabled") or user.data.get("role") not in {"owner", "admin"}:
            fail(403, "An active administrator must reconcile number configuration")

    def number_orders(self, p, intent_id=None):
        with self.db.transaction(False) as s:
            if intent_id:
                row = self.required(s, "command_outbox", intent_id, p)
                if row.data.get("kind") != "purchase":
                    fail(404, "Number order intent not found")
                return doc(row)
            model = MODELS["command_outbox"]
            result = s.scalars(
                select(model)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.external_key.like("purchase:%"),
                )
                .order_by(model.created_at.desc())
            ).all()
            return {"orders": [doc(row) for row in result if row.data.get("kind") == "purchase"]}

    def _claim_order_check(self, p, intent_id, body):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            row = self.required(s, "command_outbox", intent_id, p)
            if row.data.get("kind") != "purchase":
                fail(404, "Number order intent not found")
            if type(body.get("expected_version")) is not int:
                fail(422, "The current order version is required")
            if row.version != body["expected_version"]:
                fail(409, "Order changed; reload its current state")
            if row.data.get("lease_until") and dt(row.data["lease_until"]) > self.clock():
                fail(409, "Order reconciliation is in progress")
            receipt = row.data.get("receipt") or {}
            known = row.data.get("provider_order_id") or receipt.get("data", receipt).get("id")
            requested = body.get("provider_order_id") or known
            if known and requested != known:
                fail(409, "Reconcile the existing provider order reference")
            if not requested:
                update(
                    row,
                    status="review_required",
                    billing_status="unreconciled",
                    next_action="Find the provider order ID in your carrier account, then reconcile this same intent. Do not submit another purchase.",
                )
                return doc(row), None
            if not isinstance(requested, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", requested):
                fail(422, "A valid carrier order ID is required")
            update(
                row,
                status="reconciling",
                lease_owner=uid(),
                lease_until=(self.clock() + timedelta(seconds=60)).isoformat(),
            )
            return doc(row), requested

    async def reconcile_number_order(self, p, intent_id, body):
        intent, reference = await asyncio.to_thread(self._claim_order_check, p, intent_id, body)
        if not reference:
            return intent
        try:
            response = await self.carrier(p["tenant_id"]).get_number_order(reference)
            receipt = response.get("data", response)
            items = receipt.get("phone_numbers")
            if (
                receipt.get("id") != reference
                or not isinstance(items, list)
                or any(not isinstance(item, dict) for item in items)
                or len(items) != len(intent["numbers"])
                or {item.get("phone_number") for item in items} != set(intent["numbers"])
            ):
                raise ValueError("Carrier order does not match this intent")
        except Exception:
            await asyncio.to_thread(self._finish_order_check, p, intent, None)
            fail(502, "Order not confirmed; reload and reconcile the same intent using the carrier receipt")
        return await asyncio.to_thread(self._finish_order_check, p, intent, receipt)

    def _finish_order_check(self, p, intent, receipt):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            row = self.required(s, "command_outbox", intent["id"], p)
            if row.version != intent["version"] or row.data.get("lease_owner") != intent["lease_owner"]:
                fail(409, "Order reconciliation was superseded; reload")
            values = {
                "lease_owner": None,
                "lease_until": None,
                "checked_at": self.clock().isoformat(),
                "billing_status": row.data.get("billing_status", "unreconciled"),
            }
            if receipt is None:
                values.update(
                    status="uncertain",
                    next_action="Check the provider order ID and retry this read-only reconciliation; purchase is disabled.",
                )
            else:
                items = receipt["phone_numbers"]
                fulfilled = [
                    i["phone_number"]
                    for i in items
                    if i.get("status") == "success" and i.get("requirements_met") is True
                ]
                failed = [
                    i["phone_number"]
                    for i in items
                    if i.get("status") in {"failure", "failed", "cancelled", "canceled"}
                ]
                waiting = [i["phone_number"] for i in items if i["phone_number"] not in fulfilled + failed]
                status = (
                    "awaiting_fulfillment"
                    if waiting
                    else "completed"
                    if not failed
                    else "partially_failed"
                    if fulfilled
                    else "failed"
                )
                values.update(
                    provider_order_id=receipt["id"],
                    provider_status=receipt.get("status"),
                    status=status,
                    fulfilled_numbers=fulfilled,
                    failed_numbers=failed,
                    waiting_numbers=waiting,
                    next_action="Sync owned inventory, configure routing and verify callbacks before outbound use."
                    if fulfilled
                    else "Review pending requirements or failures in the carrier account. No new order will be submitted.",
                )
                inventory = {n.data["phone"]: n for n in rows(s, "number_inventory")}
                for phone in fulfilled:
                    current = inventory.get(phone)
                    if current:
                        if current.tenant_id != p["tenant_id"] or current.seller_id != p["seller_id"]:
                            fail(409, "Ordered inventory has conflicting ownership; review the assignment")
                        continue
                    number = put(
                        s,
                        "number_inventory",
                        p["tenant_id"],
                        p["seller_id"],
                        {
                            "phone": phone,
                            "region": phone[2:5],
                            "lifecycle": "purchased",
                            "daily_limit": 5,
                            "capacity": 1,
                            "identity_configured": False,
                            "callback_tested": False,
                            "order_intent_id": intent["id"],
                            "provider_order_id": receipt["id"],
                            "synthetic": self.settings.simulation,
                        },
                        key=phone,
                        now=self.clock(),
                    )
                    emit(s, p["tenant_id"], "number.updated", number, self.clock())
            update(row, **values)
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "number.order_reconciled",
                row.id,
                self.clock(),
                {"status": row.data["status"]},
            )
            emit(s, p["tenant_id"], "number.order_updated", row, self.clock())
            return doc(row)

    async def sync_numbers(self, p):
        # Provider pagination must complete before any missing-inventory decisions.
        inventory = await self.carrier(p["tenant_id"]).list_numbers()
        return await asyncio.to_thread(self._merge_inventory, p, inventory)

    def _merge_inventory(self, p, inventory):
        normalized = {}
        for item in inventory:
            phone = normalize_phone(str(item.get("phone_number", ""))).e164
            if not phone or not item.get("id") or phone in normalized:
                fail(502, "Incomplete or conflicting carrier inventory; previous state retained")
            normalized[phone] = item
        added, changed, missing, assigned_elsewhere = 0, 0, 0, 0
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            all_numbers = {r.data["phone"]: r for r in rows(s, "number_inventory")}
            for phone, item in normalized.items():
                row = all_numbers.get(phone)
                if row and (row.tenant_id != p["tenant_id"] or row.seller_id != p["seller_id"]):
                    assigned_elsewhere += 1
                    continue
                values = {
                    "provider_id": item["id"],
                    "connection_id": item.get("connection_id"),
                    "provider_status": item.get("status"),
                    "provider_present": True,
                    "provider_checked_at": self.clock().isoformat(),
                    "provider_recording_enabled": item.get("call_recording_enabled"),
                    "callback_retained": True,
                }
                if row:
                    routing_changed = (
                        row.data.get("connection_id") != item.get("connection_id")
                        or row.data.get("provider_id") != item["id"]
                    )
                    unavailable = item.get("status") not in {None, "active"}
                    if routing_changed or unavailable or item.get("call_recording_enabled"):
                        values.update(
                            callback_tested=False,
                            identity_configured=False,
                            configuration_status="review_required",
                            lifecycle="investigating" if not unavailable else "paused_for_outbound",
                        )
                    if any(row.data.get(k) != v for k, v in values.items() if k != "provider_checked_at"):
                        changed += 1
                    update(row, **values)
                else:
                    row = put(
                        s,
                        "number_inventory",
                        p["tenant_id"],
                        p["seller_id"],
                        {
                            "phone": phone,
                            "region": phone[2:5] if phone.startswith("+1") else "general",
                            "lifecycle": "purchased",
                            "daily_limit": 5,
                            "capacity": 1,
                            "identity_configured": False,
                            "callback_tested": False,
                            "synthetic": self.settings.simulation,
                            **values,
                        },
                        key=phone,
                        now=self.clock(),
                    )
                    added += 1
                emit(s, p["tenant_id"], "number.updated", row, self.clock())
            for phone, row in all_numbers.items():
                if (
                    row.tenant_id == p["tenant_id"]
                    and row.seller_id == p["seller_id"]
                    and row.data.get("provider_id")
                    and phone not in normalized
                ):
                    update(
                        row,
                        provider_present=False,
                        provider_checked_at=self.clock().isoformat(),
                        lifecycle="paused_for_outbound",
                        configuration_status="missing_at_provider",
                        callback_retained=True,
                    )
                    missing += 1
            result = {
                "added": added,
                "updated": changed,
                "missing": missing,
                "assigned_elsewhere": assigned_elsewhere,
                "provider_count": len(inventory),
            }
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "numbers.synchronized",
                p["seller_id"],
                self.clock(),
                result,
            )
        return result

    def _claim_number_configuration(self, p, number_id, connection):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            self._configuration_actor(s, p)
            number = self.required(s, "number_inventory", number_id, p)
            if not number.data.get("provider_id"):
                fail(409, "Sync owned carrier inventory before configuring this number")
            if number.data.get("provider_present") is False:
                fail(409, "Number was absent from the last complete carrier inventory")
            if (
                number.data.get("connection_id") == connection
                and number.data.get("configuration_status") == "confirmed"
            ):
                return None, doc(number)
            model = MODELS["command_outbox"]
            key = "configure:" + number_id + ":" + hashlib.sha256(connection.encode()).hexdigest()
            action = s.scalar(
                select(model).where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.external_key == key,
                )
            )
            for prior in s.scalars(
                select(model).where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.op_kind == "number_configure",
                    model.op_number_id == number_id,
                    model.op_status.in_(["sending", "reconciling", "uncertain", "review_required"]),
                )
            ):
                if dt(prior.data.get("lease_until")) and dt(prior.data["lease_until"]) > self.clock():
                    fail(409, "Number configuration is already in progress")
                if prior.external_key != key and prior.data.get("status") in {
                    "sending",
                    "reconciling",
                    "uncertain",
                    "review_required",
                }:
                    fail(409, "Reconcile the previous route before configuring another one")
            reconcile = bool(
                action
                and action.data.get("status") in {"sending", "reconciling", "uncertain", "review_required"}
            )
            if reconcile and action.data.get("provider_id") != number.data["provider_id"]:
                fail(409, "Original number identity changed; review the existing configuration intent")
            values = {
                "kind": "number_configure",
                "number_id": number_id,
                "provider_id": number.data["provider_id"],
                "phone": number.data["phone"],
                "connection_id": connection,
                "status": "sending",
                "lease_owner": uid(),
                "lease_until": (self.clock() + timedelta(seconds=60)).isoformat(),
                "started_at": action.data.get("started_at") if action else self.clock().isoformat(),
            }
            if action:
                update(action, **values)
            else:
                action = put(
                    s, "command_outbox", p["tenant_id"], p["seller_id"], values, key=key, now=self.clock()
                )
            update(number, configuration_status="pending", identity_configured=False, callback_tested=False)
            return {**doc(action), "reconcile": reconcile, "number_version": number.version}, None

    async def configure_owned_number(self, p, number_id):
        connection = self.vault.read(p["tenant_id"]).get("telnyx_connection_id")
        if not connection:
            fail(409, "Configure the authorized Telnyx voice connection first")
        intent, cached = await asyncio.to_thread(self._claim_number_configuration, p, number_id, connection)
        if cached:
            return cached
        carrier = self.carrier(p["tenant_id"])
        try:
            if intent["reconcile"]:
                receipt = await carrier.get_number(intent["provider_id"])
            else:
                receipt = await carrier.configure_number(intent["provider_id"], connection)
            if not self._configuration_receipt(intent, receipt):
                raise ValueError("Carrier configuration did not confirm the requested route")
        except Exception:
            await asyncio.to_thread(self._finish_number_configuration, p, intent, False)
            fail(
                503,
                "Number configuration is uncertain; retry reconciles the same intent without purchasing or resending",
            )
        return await asyncio.to_thread(self._finish_number_configuration, p, intent, True)

    def _configuration_receipt(self, intent, receipt):
        if not isinstance(receipt, dict):
            return False
        receipt = receipt.get("data", receipt)
        if not isinstance(receipt, dict):
            return False
        if (
            receipt.get("id") != intent["provider_id"]
            or receipt.get("connection_id") != intent["connection_id"]
        ):
            return False
        required = not self.settings.simulation
        return all(
            receipt.get(key) == expected and type(receipt.get(key)) is type(expected)
            for key, expected in {
                "phone_number": intent["phone"],
                "status": "active",
                "call_recording_enabled": False,
            }.items()
            if required or key in receipt
        )

    def _claim_number_check(self, p, identity, expected_version=None, provider_id=None):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            self._configuration_actor(s, p)
            action = self.required(s, "command_outbox", identity, p)
            if action.data.get("kind") != "number_configure":
                fail(409, "This effect is not number configuration")
            if expected_version is not None and action.version != expected_version:
                fail(409, "Configuration changed; refresh before reconciling")
            if provider_id and provider_id != action.data.get("provider_id"):
                fail(409, "Verify the original number identity; it cannot be replaced")
            if action.data.get("status") == "completed":
                return None, doc(action)
            if action.data.get("status") not in {"sending", "reconciling", "uncertain", "review_required"}:
                fail(409, "Only an uncertain submitted configuration can be reconciled")
            if action.data.get("lease_until") and dt(action.data["lease_until"]) > self.clock():
                fail(409, "Number configuration is already in progress")
            number = self.required(s, "number_inventory", action.data["number_id"], p)
            if (
                not action.data.get("provider_id")
                or number.data.get("provider_id") != action.data["provider_id"]
            ):
                fail(409, "Original number identity changed; sync and review its carrier ownership")
            update(
                action,
                status="reconciling",
                lease_owner=uid(),
                lease_until=(self.clock() + timedelta(seconds=30)).isoformat(),
            )
            return {
                **doc(action),
                "phone": action.data.get("phone") or number.data["phone"],
                "number_version": number.version,
            }, None

    async def reconcile_number_configuration(self, p, identity, *, expected_version=None, provider_id=None):
        intent, cached = await asyncio.to_thread(
            self._claim_number_check, p, identity, expected_version, provider_id
        )
        if cached:
            return cached
        try:
            receipt = await self.carrier(p["tenant_id"]).get_number(intent["provider_id"])
            confirmed = self._configuration_receipt(intent, receipt)
        except Exception:
            confirmed = False
        return await asyncio.to_thread(self._finish_number_configuration, p, intent, confirmed, recovery=True)

    def _finish_number_configuration(self, p, intent, confirmed, *, recovery=False):
        stale = False
        connection = self.vault.read(p["tenant_id"]).get("telnyx_connection_id")
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            self._configuration_actor(s, p)
            action = self.required(s, "command_outbox", intent["id"], p)
            number = self.required(s, "number_inventory", intent["number_id"], p)
            if (
                action.version != intent["version"]
                or action.data.get("lease_owner") != intent["lease_owner"]
                or not action.data.get("lease_until")
                or dt(action.data["lease_until"]) <= self.clock()
            ):
                fail(409, "Configuration was superseded; inspect its current state")
            stale = (
                number.version != intent["number_version"]
                or number.data.get("provider_id") != intent["provider_id"]
            )
            update(
                action,
                status="review_required"
                if stale
                else "completed"
                if confirmed
                else "review_required"
                if recovery
                else "uncertain",
                lease_until=None,
                lease_owner=None,
                finished_at=self.clock().isoformat(),
                next_attempt_at=(self.clock() + timedelta(seconds=60)).isoformat(),
                recovery_reason=None
                if confirmed and not stale
                else "Verify the original number, active route and disabled recording in the carrier account; this check never resubmits configuration.",
            )
            if confirmed and not stale:
                lifecycle = number.data["lifecycle"]
                if lifecycle not in {"paused_for_outbound", "retired_callback_only", "investigating"}:
                    lifecycle = "identity_configured"
                update(
                    number,
                    connection_id=intent["connection_id"],
                    configuration_status="confirmed"
                    if connection == intent["connection_id"]
                    else "route_changed",
                    identity_configured=connection == intent["connection_id"],
                    callback_tested=False,
                    lifecycle=lifecycle,
                    callback_retained=True,
                )
            elif not stale:
                update(
                    number, configuration_status="uncertain", identity_configured=False, callback_tested=False
                )
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "number.configuration",
                number.id,
                self.clock(),
                {"intent_id": action.id, "confirmed": confirmed and not stale, "stale": stale},
            )
            emit(s, p["tenant_id"], "number.updated", number, self.clock())
            result = doc(action) if recovery else doc(number)
        if stale:
            fail(409, "Inventory changed during configuration; reconcile the current carrier identity")
        return result
