"""Reconcile owned inventory and one durable configuration intent per number/route."""

import asyncio
import hashlib
from datetime import timedelta

from sqlalchemy import select

from app.compliance.phone import normalize_phone
from app.db import MODELS, audit, doc, emit, put, rows, uid, update
from app.service import dt, fail


class NumberOperations:
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
            for prior in self.scoped(s, "command_outbox", p):
                if prior.data.get("kind") != "number_configure" or prior.data.get("number_id") != number_id:
                    continue
                if prior.data.get("status") == "sending" and dt(prior.data["lease_until"]) > self.clock():
                    fail(409, "Number configuration is already in progress")
                if prior.external_key != key and prior.data.get("status") in {
                    "sending",
                    "uncertain",
                    "review_required",
                }:
                    fail(409, "Reconcile the previous route before configuring another one")
            reconcile = bool(
                action and action.data.get("status") in {"sending", "uncertain", "review_required"}
            )
            values = {
                "kind": "number_configure",
                "number_id": number_id,
                "provider_id": number.data["provider_id"],
                "connection_id": connection,
                "status": "sending",
                "lease_owner": uid(),
                "lease_until": (self.clock() + timedelta(seconds=60)).isoformat(),
                "started_at": self.clock().isoformat(),
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
            receipt = receipt.get("data", receipt)
            if receipt.get("id") != intent["provider_id"] or receipt.get("connection_id") != connection:
                raise ValueError("Carrier configuration did not confirm the requested route")
        except Exception:
            await asyncio.to_thread(self._finish_number_configuration, p, intent, False)
            fail(
                503,
                "Number configuration is uncertain; retry reconciles the same intent without purchasing or resending",
            )
        return await asyncio.to_thread(self._finish_number_configuration, p, intent, True)

    def _finish_number_configuration(self, p, intent, confirmed):
        stale = False
        with self.db.transaction() as s:
            action = self.required(s, "command_outbox", intent["id"], p)
            number = self.required(s, "number_inventory", intent["number_id"], p)
            if action.data.get("lease_owner") != intent["lease_owner"]:
                fail(409, "Configuration was superseded; inspect its current state")
            stale = (
                number.version != intent["number_version"]
                or number.data.get("provider_id") != intent["provider_id"]
            )
            update(
                action,
                status="review_required" if stale else "completed" if confirmed else "uncertain",
                lease_until=None,
                lease_owner=None,
                finished_at=self.clock().isoformat(),
            )
            if confirmed and not stale:
                lifecycle = number.data["lifecycle"]
                if lifecycle not in {"paused_for_outbound", "retired_callback_only", "investigating"}:
                    lifecycle = "identity_configured"
                update(
                    number,
                    connection_id=intent["connection_id"],
                    configuration_status="confirmed",
                    identity_configured=True,
                    callback_tested=False,
                    lifecycle=lifecycle,
                    callback_retained=True,
                )
            elif not stale:
                update(number, configuration_status="uncertain")
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
            result = doc(number)
        if stale:
            fail(409, "Inventory changed during configuration; reconcile the current carrier identity")
        return result
