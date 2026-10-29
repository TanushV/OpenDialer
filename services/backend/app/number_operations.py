"""Reconcile owned inventory and one durable configuration intent per number/route."""

import asyncio
import hashlib
from datetime import timedelta
from decimal import Decimal

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.compliance.phone import normalize_phone
from app.db import MODELS, ActiveClaim, audit, doc, emit, put, uid, update
from app.optimization.numbering import assess_number_health, callback_current, number_ready
from app.service import dt, fail


class ExternalReputation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=3, max_length=300)
    ownership_reference: str = Field(min_length=3, max_length=300)
    observed_at: AwareDatetime
    expires_at: AwareDatetime
    spam_label: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def ordered(self):
        if not timedelta(0) < self.expires_at - self.observed_at <= timedelta(days=90):
            raise ValueError("Imported evidence needs a positive expiry within 90 days")
        return self


def carrier_rejection(exc):
    """A definitive 4xx means the carrier did not apply the request; timeouts, rate limits,
    conflicts and server errors stay uncertain and reconcile the original intent."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    status = exc.response.status_code
    if not 400 <= status < 500 or status in {408, 409, 429}:
        return None
    try:
        errors = exc.response.json().get("errors") or []
    except (ValueError, AttributeError):
        errors = []
    detail = "; ".join(
        str(error.get("title") or error.get("detail") or error.get("code"))
        for error in errors
        if isinstance(error, dict)
    )
    return f"{status} {detail}".strip()[:300]


class NumberOperations:
    def purchase_orders_in(self, s, p):
        model = MODELS["command_outbox"]
        return list(
            s.scalars(
                select(model)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.op_kind == "number_purchase",
                )
                .order_by(model.created_at.desc())
            )
        )

    def _claim_purchase(self, p, quote_id):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            self._configuration_actor(s, p)
            quote = self.required(s, "command_outbox", quote_id, p)
            if quote.data.get("kind") == "number_purchase":
                return doc(quote), False
            if quote.data.get("kind") != "number_quote" or dt(quote.data["expires_at"]) <= self.clock():
                fail(409, "Quote expired; search again and review the current price")
            config = self.configs.read(p["tenant_id"])
            if not self.settings.simulation and not all(
                config.get(k) for k in ("telnyx_api_key", "telnyx_connection_id")
            ):
                fail(409, "Connect Telnyx before ordering numbers")
            numbers = self.scoped(s, "number_inventory", p)
            phones = {n.data["phone"] for n in numbers}
            orders = [r for r in self.purchase_orders_in(s, p) if r.data.get("status") != "failed"]
            reserved = {r.data["phone"] for r in orders}
            if quote.data["phone"] in phones | reserved or self.number_owners(s, [quote.data["phone"]]):
                fail(409, "This number is already owned or has an order awaiting reconciliation")
            if len(phones | reserved) + 1 > config.get("maximum_owned_numbers", 0):
                fail(409, "Maximum owned numbers reached; review your number limit")
            if quote.data["initial_commitment_usd"] > config.get("number_initial_limit_usd", 0):
                fail(409, "This order exceeds the approved initial cost per number")
            # This cap covers rentals added through this app, including uncertain orders.
            monthly = sum(Decimal(str(r.data["monthly_cost_usd"])) for r in orders) + Decimal(
                str(quote.data["monthly_cost_usd"])
            )
            if monthly > Decimal(str(config.get("number_monthly_limit_usd", 0))):
                fail(409, "This order exceeds the monthly limit for app-ordered rentals")
            update(
                quote,
                kind="number_purchase",
                status="sending",
                approved_by=p["user_id"],
                approved_at=self.clock().isoformat(),
            )
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "number.purchase_approved",
                quote.id,
                self.clock(),
                {"phone": quote.data["phone"], "monthly_cost_usd": str(monthly)},
            )
            return doc(quote), True

    def _purchase_receipt(self, p, order_id, status, **values):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            row = self.required(s, "command_outbox", order_id, p)
            update(row, status=status, **values)
            emit(s, p["tenant_id"], "number.updated", row, self.clock())
            return doc(row)

    async def purchase_number(self, p, quote_id):
        order, send = await asyncio.to_thread(self._claim_purchase, p, quote_id)
        if not send:
            return order
        carrier = self.carrier(p["tenant_id"])
        # Recheck availability and price before the single charge-bearing submission.
        try:
            candidates = await carrier.search_numbers(order["region"])
            current = next((n for n in candidates if n.get("phone_number") == order["phone"]), {})
            cost = current.get("cost_information", {})
            same = (
                cost.get("currency") == "USD"
                and Decimal(str(cost.get("monthly_cost"))) == Decimal(str(order["monthly_cost_usd"]))
                and Decimal(str(cost.get("upfront_cost"))) + Decimal(str(cost.get("monthly_cost")))
                == Decimal(str(order["initial_commitment_usd"]))
            )
            if not same:
                raise ValueError("Availability or price changed; search again before approving")
        except Exception:
            return await asyncio.to_thread(
                self._purchase_receipt,
                p,
                order["id"],
                "failed",
                error="Could not confirm the quoted price; search again",
            )
        try:
            receipt = await carrier.purchase_number(order["phone"], order["id"])
            if not isinstance(receipt, dict) or not receipt.get("id"):
                raise ValueError("Missing provider order reference")
        except Exception as exc:
            rejected = carrier_rejection(exc)
            return await asyncio.to_thread(
                self._purchase_receipt,
                p,
                order["id"],
                "failed" if rejected else "uncertain",
                error=rejected or "Receipt unavailable. Check order status; do not place another order.",
            )
        result = await asyncio.to_thread(
            self._purchase_receipt,
            p,
            order["id"],
            "ordered",
            provider_order_id=receipt["id"],
            provider_status=receipt.get("status"),
        )
        try:
            await self.sync_numbers(p)
        except Exception:
            result["inventory_pending"] = True
        return result

    async def reconcile_number_order(self, p, order_id):
        with self.db.transaction(False) as s:
            self._configuration_actor(s, p)
            row = self.required(s, "command_outbox", order_id, p)
            if row.data.get("kind") != "number_purchase":
                fail(409, "Not an approved number order")
            original = doc(row)
        receipt = await self.carrier(p["tenant_id"]).find_number_order(order_id)
        if not receipt or not receipt.get("id"):
            return original  # An absent result is not proof that a submission failed.
        result = await asyncio.to_thread(
            self._purchase_receipt,
            p,
            order_id,
            "ordered",
            provider_order_id=receipt["id"],
            provider_status=receipt.get("status"),
            error=None,
        )
        await self.sync_numbers(p)
        return result

    def number_owners(self, s, phones):
        """Owner of each phone across sellers, read by lookup key without decrypting other inventory."""
        model = MODELS["number_inventory"]
        keys = {phone: phone for phone in phones}
        if not keys:
            return {}
        return {
            keys[key]: (tenant_id, seller_id)
            for key, tenant_id, seller_id in s.execute(
                select(model.external_key, model.tenant_id, model.seller_id).where(
                    model.external_key.in_(keys)
                )
            )
        }

    def number_readiness_in(self, s, p, number):
        value = doc(number)
        config = self.callback_config(s, p)
        errors = []
        if not self.settings.simulation:
            if not value.get("provider_id") or value.get("provider_present") is not True:
                errors.append("provider_ownership_unconfirmed")
            if value.get("provider_status") != "active":
                errors.append("provider_not_active")
            if value.get("configuration_status") != "confirmed" or not value.get("identity_configured"):
                errors.append("route_unconfirmed")
            if value.get("provider_recording_enabled") is not False:
                errors.append("recording_not_disabled")
        callback_valid = callback_current(value, self.clock(), config["version"])
        external = assess_number_health(
            [],
            {"as_of": self.clock().isoformat()},
            value.get("external_reputation"),
            self.health_policy_in(s, p),
        )

        return {
            "configuration_errors": errors,
            "callback_evidence_current": callback_valid,
            "outbound_ready": not errors
            and external["status"] != "investigating"
            and callback_valid
            and number_ready(value, now=self.clock()),
            "route_version": config["version"],
        }

    def number_review_view(self, p, number_id, lead_id=None):
        with self.db.transaction(False) as s:
            self._configuration_actor(s, p)
            number = self.required(s, "number_inventory", number_id, p)
            view = next(n for n in self.inventory_evidence(s, p, fast=True) if n["id"] == number_id)
            task = MODELS["followup_tasks"]
            candidates = list(
                s.scalars(
                    select(task)
                    .where(
                        task.tenant_id == p["tenant_id"],
                        task.seller_id == p["seller_id"],
                        task.op_number_id == number_id,
                    )
                    .order_by(task.created_at.desc(), task.id)
                    .limit(20)
                )
            )
            config = self.optimization_config(s, p)
            ceiling = self.number_workload_ceiling(config)
            binding = None
            if lead_id:
                self.required(s, "leads", lead_id, p)
                binding = self._by_key(s, p, "lead_number_bindings", lead_id)
            return {
                "number": {**doc(number), **self.number_readiness_in(s, p, number)},
                "health": view["health"],
                "binding": doc(binding) if binding else None,
                "callback_candidates": [doc(t) for t in candidates if t.data.get("callback_evidence")],
                "callback_candidates_limit": 20,
                "workload": {
                    "current_limit": view["daily_limit"],
                    "approved_daily_limit": number.data["daily_limit"],
                    "maximum_reviewed_limit": ceiling,
                    # The staffed, approved ceiling bounds every suggestion. Healthy or sparse
                    # (unknown) evidence may use it; deterioration or errors never raise a limit.
                    "recommended_limit": ceiling
                    if view["health"]["status"] in {"observed", "unknown"}
                    else min(ceiling, number.data["daily_limit"]),
                    "reason": view["health"]["recommended_action"],
                },
                "policy": config["health_policy"],
            }

    def number_workload_ceiling(self, config):
        from app.optimization.capacity import normalize_availability, staffed_slots

        now, end = self.clock(), self.clock() + timedelta(days=1)
        windows = normalize_availability(config["staffed_windows"], now, end, config["rep_minutes"], config)
        slots, _, limited = staffed_slots(windows, now, end, config["handling_seconds"])
        # A bounded lower staffed count never promises capacity omitted by the planner.
        return min(config["daily_capacity_per_number"], sum(count for _, count in slots))

    def confirm_callback_in(self, s, p, number, task_id):
        task = self.required(s, "followup_tasks", task_id, p)
        proof = task.data.get("callback_evidence")
        if not proof or proof.get("number_id") != number.id:
            fail(409, "A matching connected inbound callback receipt is required")
        if not callback_current(
            {**doc(number), "callback_proof": proof}, self.clock(), self.callback_config(s, p)["version"]
        ):
            fail(409, "Callback evidence expired or its route changed; verify the current route")
        if not self.settings.simulation:
            for identity in (proof["inbound_event_id"], proof["bridge_event_id"]):
                inbox = self._by_key(s, {**p, "seller_id": ""}, "webhook_inbox", identity)
                if (
                    not inbox
                    or not inbox.data.get("signature_verified")
                    or inbox.data.get("status") != "processed"
                ):
                    fail(409, "A completed signed provider callback receipt is required")
            if proof.get("simulated"):
                fail(409, "Simulated callback evidence cannot verify live service")
        update(
            number,
            callback_proof=proof,
            callback_tested=True,
            lifecycle="callback_tested"
            if number.data["lifecycle"] == "identity_configured"
            else number.data["lifecycle"],
        )

    def reassign_number(self, p, number_id, body):
        change = body["reassignment"]
        if (
            not isinstance(change, dict)
            or set(change) != {"lead_id", "old_number_id", "reason"}
            or not isinstance(change["reason"], str)
            or not 3 <= len(change["reason"].strip()) <= 500
        ):
            fail(422, "Reassignment requires lead, old number and a reviewed reason")
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            self._configuration_actor(s, p)
            lead = self.required(s, "leads", change["lead_id"], p)
            number = self.required(s, "number_inventory", number_id, p)
            old = self.required(s, "number_inventory", change["old_number_id"], p)
            binding = self._by_key(s, p, "lead_number_bindings", lead.id)
            if not binding:
                fail(409, "The lead has no existing identity to reassign")
            identity = self.local_edit_in(s, p, "number_reassignment", number_id, binding, body, "owner_id")
            if identity[2]:
                return {
                    **doc(number),
                    "reassignment": binding.data.get("reassignment"),
                    "receipt": identity[2],
                }
            self.check_edit_version(binding, body)
            if binding.data["number_id"] != old.id or old.id == number.id:
                fail(409, "Original binding changed; reload before reassignment")
            if not self.number_readiness_in(s, p, number)["outbound_ready"]:
                fail(409, "The replacement identity is not ready")
            if s.scalar(
                select(ActiveClaim).where(
                    ActiveClaim.tenant_id == p["tenant_id"],
                    ActiveClaim.seller_id == p["seller_id"],
                    ActiveClaim.endpoint_id == lead.data["endpoint_id"],
                )
            ):
                fail(409, "Finish the active call or reservation before reassigning")
            receipt = {
                **change,
                "new_number_id": number.id,
                "effective_at": self.clock().isoformat(),
                "actor_id": p["user_id"],
            }
            update(binding, number_id=number.id, reassignment=receipt)
            update(old, inbound_retained=True)
            update(lead, number_assignment_version=binding.version)
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "number.binding_reassigned",
                binding.id,
                self.clock(),
                receipt,
            )
            emit(s, p["tenant_id"], "planning.changed", lead, self.clock())
            return {
                **doc(number),
                "reassignment": receipt,
                "receipt": self.local_edit_receipt(s, p, identity, binding),
            }

    def _configuration_actor(self, s, p):
        user = self.required(s, "users", p["user_id"], p)
        if user.data.get("disabled") or user.data.get("role") not in {"owner", "admin"}:
            fail(403, "An active administrator must reconcile number configuration")

    async def sync_numbers(self, p):
        # Provider pagination must complete before any missing-inventory decisions.
        inventory = await self.carrier(p["tenant_id"]).list_numbers()
        if self.managed.enabled(p["tenant_id"]):
            self.managed.require_ready(p["tenant_id"])
            connection = self.configs.read(p["tenant_id"]).get("telnyx_connection_id")
            inventory = [
                n
                for n in inventory
                if n.get("phone_number") in self.managed.numbers
                or str(n.get("connection_id") or "") == connection
            ]
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
            self._configuration_actor(s, p)
            # Only this seller's inventory is decrypted; other owners are found by lookup key.
            all_numbers = {r.data["phone"]: r for r in self.scoped(s, "number_inventory", p)}
            owners = self.number_owners(s, set(normalized) - set(all_numbers))
            for phone, item in normalized.items():
                row = all_numbers.get(phone)
                if not row and phone in owners:
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
                    if (
                        routing_changed
                        or unavailable
                        or item.get("call_recording_enabled")
                        or (not self.settings.simulation and item.get("call_recording_enabled") is not False)
                    ):
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

    def _claim_number_configuration(self, p, number_id, connection, expected_version=None):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            self._configuration_actor(s, p)
            number = self.required(s, "number_inventory", number_id, p)
            self.managed.require_ready(p["tenant_id"])
            if self.managed.enabled(p["tenant_id"]) and connection != self.configs.read(p["tenant_id"]).get(
                "telnyx_connection_id"
            ):
                fail(409, "Use this workspace’s assigned calling application")
            if expected_version is not None and number.version != expected_version:
                fail(409, "Number changed; reload before configuring it")
            if not number.data.get("provider_id"):
                fail(409, "Sync owned carrier inventory before configuring this number")
            if number.data.get("provider_present") is False:
                fail(409, "Number was absent from the last complete carrier inventory")
            if (
                number.data.get("connection_id") == connection
                and number.data.get("configuration_status") == "confirmed"
                and (self.settings.simulation or number.data.get("provider_recording_enabled") is False)
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

    def _allow_configuration_retry(self, p, intent):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            self._configuration_actor(s, p)
            action = self.required(s, "command_outbox", intent["id"], p)
            number = self.required(s, "number_inventory", intent["number_id"], p)
            if (
                action.version != intent["version"]
                or number.version != intent["number_version"]
                or action.data.get("lease_owner") != intent["lease_owner"]
                or dt(action.data.get("lease_until")) <= self.clock()
                or self.configs.read(p["tenant_id"]).get("telnyx_connection_id") != intent["connection_id"]
            ):
                fail(409, "Configuration ownership changed during verification; reload")
            update(
                action,
                verified_retry_count=action.data.get("verified_retry_count", 0) + 1,
                verified_retry_at=self.clock().isoformat(),
            )
            return {**intent, "version": action.version}

    async def configure_owned_number(self, p, number_id, body=None):
        body = body or {}
        if (
            set(body) - {"expected_version", "retry_after_verification"}
            or ("retry_after_verification" in body and type(body["retry_after_verification"]) is not bool)
            or (
                "expected_version" in body
                and (type(body["expected_version"]) is not int or body["expected_version"] < 1)
            )
        ):
            fail(422, "Invalid configuration operation")
        retry = body.get("retry_after_verification", False)
        if retry and (type(body.get("expected_version")) is not int or body["expected_version"] < 1):
            fail(422, "Explicit retry requires the current number version")
        connection = self.configs.read(p["tenant_id"]).get("telnyx_connection_id")
        if not connection:
            fail(409, "Configure the authorized Telnyx voice connection first")
        intent, cached = await asyncio.to_thread(
            self._claim_number_configuration, p, number_id, connection, body.get("expected_version")
        )
        if cached:
            return cached
        carrier = self.carrier(p["tenant_id"])
        try:
            if intent["reconcile"]:
                receipt = await carrier.get_number(intent["provider_id"])
                if retry and not self._configuration_receipt(intent, receipt):
                    state = receipt.get("data", receipt) if isinstance(receipt, dict) else {}
                    if (
                        state.get("id") != intent["provider_id"]
                        or state.get("phone_number") != intent["phone"]
                        or state.get("status") != "active"
                    ):
                        raise ValueError("Original owned identity was not verified")
                    intent = await asyncio.to_thread(self._allow_configuration_retry, p, intent)
                    receipt = await carrier.configure_number(intent["provider_id"], connection)
            else:
                receipt = await carrier.configure_number(intent["provider_id"], connection)
            if not self._configuration_receipt(intent, receipt):
                raise ValueError("Carrier configuration did not confirm the requested route")
        except Exception as exc:
            rejection = carrier_rejection(exc)
            await asyncio.to_thread(self._finish_number_configuration, p, intent, False, rejection=rejection)
            if rejection:
                fail(422, f"Carrier rejected the number configuration ({rejection}); correct it and retry")
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

    def _finish_number_configuration(self, p, intent, confirmed, *, recovery=False, rejection=None):
        stale = False
        connection = self.configs.read(p["tenant_id"]).get("telnyx_connection_id")
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
            if stale:
                status = "review_required"
            elif confirmed:
                status = "completed"
            elif rejection:
                # The carrier definitively refused it: terminal, not an uncertain effect to reconcile.
                status = "failed"
            else:
                status = "review_required" if recovery else "uncertain"
            update(
                action,
                status=status,
                lease_until=None,
                lease_owner=None,
                finished_at=self.clock().isoformat(),
                next_attempt_at=(self.clock() + timedelta(seconds=60)).isoformat(),
                provider_error=rejection,
                recovery_reason=None
                if status in {"completed", "failed"}
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
                    provider_present=True,
                    provider_status="active",
                    provider_recording_enabled=False,
                    callback_tested=False,
                    lifecycle=lifecycle,
                    callback_retained=True,
                )
            elif not stale:
                update(
                    number,
                    configuration_status="failed" if rejection else "uncertain",
                    identity_configured=False,
                    callback_tested=False,
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
