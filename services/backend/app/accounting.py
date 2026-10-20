"""One budget liability per provider attempt, settled by an attributable receipt."""

import hashlib
import hmac
import json
import logging
import math
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select

from app.db import MODELS, get, put, update
from app.operational_schema import receipt_key

# Explicit recovery metadata, never payloads, scripts, prompts, questions or speech.
EFFECT_FIELDS = {
    "call_attempts": frozenset(
        "rep_id lead_id endpoint_id number_id reservation_id state started_at submitted_at ended_at max_call_seconds provider_id detached attempt_ordinal attempt_allowance synthetic outcome".split()
    ),
    "call_legs": frozenset("kind rep_id call_id provider_id state command_id started_at ended_at".split()),
    "command_outbox": frozenset(
        "kind action call_id rep_id number_id provider_id target inbound inbound_attempt status command_id submitted_at sent_at expires_at started_at connection_id phone lead_ids ready_at model request_digest provider_batch_id provider_order_id numbers committed_usd approved_budget_usd fulfilled_numbers failed_numbers waiting_numbers billing_status".split()
    ),
    "usage_ledger": frozenset(
        "kind work_kind provider object_id call_id callback_id user_id request_key request_id provider_record_id provider_record_ids receipt_id at received_at submitted_at reserved_usd estimate_usd cost_usd credited_usd credit_received_at billing_verified status synthetic reconciled_to_provider_records duration_seconds leg_id".split()
    ),
    "registry_snapshots": frozenset(
        "kind phone request_key status started_at authority_stamp checked_at expires_at listed estimate_usd previous_attempts".split()
    ),
    "followup_tasks": frozenset(
        "kind lead_id endpoint_id number_id owner_id provider_id caller_provider_id inbound_state inbound_deadline status created_at due_at inbound caller_ended called_phone callback_phone previous_call_id responsible_rep_id routed_rep_id route routing_attempts routing_config ended_at".split()
    ),
    "audit_events": frozenset({"actor_id", "action", "target_id", "detail"}),
}


def effect_entity(row):
    kind = row.__tablename__
    data = {k: v for k, v in row.data.items() if k in EFFECT_FIELDS[kind]}
    if kind == "command_outbox":
        data["command_id"] = row.data.get("payload", {}).get("command_id") or row.external_key
    if kind == "usage_ledger" and data.get("call_id") and not data.get("object_id"):
        data["object_id"] = data["call_id"]
    if kind == "followup_tasks":
        if "routing_attempts" in data:
            data["routing_attempts"] = [
                {
                    k: v
                    for k, v in a.items()
                    if k
                    in {"id", "rep_id", "kind", "target", "state", "provider_id", "started_at", "ended_at"}
                }
                for a in data["routing_attempts"][:4]
            ]
        if "routing_config" in data:
            data["routing_config"] = {
                k: v
                for k, v in data["routing_config"].items()
                if k in {"version", "destination", "ring_seconds"}
            }
    if kind == "audit_events":
        detail = data.get("detail", {})
        result = detail.get("result", {})
        data["detail"] = {k: detail[k] for k in ("provider", "receipt_id", "digest") if k in detail}
        data["detail"]["result"] = {
            k: result[k] for k in ("entry_id", "evidence", "invoice_verified") if k in result
        }
        if "entry" in result:
            data["detail"]["result"]["entry"] = {
                k: v
                for k, v in result["entry"].items()
                if k
                in EFFECT_FIELDS["usage_ledger"]
                | {"id", "version", "net_cost_usd", "can_settle", "can_credit", "can_close_call"}
            }
    return {
        "table": kind,
        "id": row.id,
        "external_key": row.external_key,
        "created_at": row.created_at.isoformat(),
        "version": row.version,
        "data": data,
    }


class RequestAccounting:
    def journal_effect_in(
        self, s, p, kind, identity, phase, records=(), *, provider_id=None, source=None, emergency=False
    ):
        """A durable boundary before effects; this does not execute or retry them."""
        if phase not in {"intent", "submitted", "receipt"} or not identity or not kind:
            raise ValueError("Typed effect identity and phase required")
        entities = [effect_entity(row) for row in records if row is not None]
        entry = {
            "schema_version": 1,
            "operation_id": identity,
            "kind": kind,
            "phase": phase,
            "tenant_id": p["tenant_id"],
            "seller_id": p["seller_id"],
            "user_id": p.get("user_id"),
            "at": self.clock().isoformat(),
            "provider_id": provider_id,
            "entities": entities,
            "source_digest": hmac.new(
                self.db.identity_key,
                json.dumps(source if source is not None else entities, sort_keys=True, default=str).encode(),
                hashlib.sha256,
            ).hexdigest(),
        }
        # Loading may flush a caller's pending SQL. A database conflict is not
        # an independent journal failure and must abort the original transaction.
        seller = get(s, "sellers", p["seller_id"], p["tenant_id"])
        try:
            if not emergency and (
                getattr(self, "_effect_journal_fault", False)
                or seller
                and seller.data.get("effect_journal_pending")
            ):
                raise ValueError("Repair the original effect journal before new work")
            self.vault.append_journal("effects", entry)
            return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
        except Exception:
            self._effect_journal_fault = True
            # Persist after the caller releases its writer. Emergency stops still run.
            s.info.setdefault("after_transaction", []).append(lambda: self._save_effect_fault(p, entry))
            if not emergency:
                raise HTTPException(503, "EFFECT_JOURNAL_RECOVERY_REQUIRED") from None

    def _save_effect_fault(self, p, entry):
        try:
            with self.db.transaction() as s:
                self.db.authorize_stream(s, p)
                seller = get(s, "sellers", p["seller_id"], p["tenant_id"])
                if seller:
                    pending = dict(seller.data.get("effect_journal_pending", {}))
                    key = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
                    # Once held, only stop/receipt metadata can add further entries.
                    if len(pending) < 128:
                        pending[key] = entry
                    update(
                        seller,
                        effect_journal_pending=pending,
                        restore_hold=True,
                        effect_journal_gap=len(pending) >= 128
                        or seller.data.get("effect_journal_gap", False),
                    )
        except Exception as exc:
            logging.getLogger(__name__).error("Effect journal hold unavailable: %s", type(exc).__name__)

    def effect_entries(self):
        entries = self.vault.read_journal("effects")
        for entry in entries:
            if (
                entry.get("schema_version") != 1
                or entry.get("phase") not in {"intent", "submitted", "receipt"}
                or not all(
                    isinstance(entry.get(k), str) and entry[k]
                    for k in ("operation_id", "kind", "tenant_id", "seller_id", "at", "source_digest")
                )
            ):
                raise HTTPException(409, "Effect journal has invalid metadata")
            for row in entry.get("entities", []):
                if (
                    row.get("table") not in EFFECT_FIELDS
                    or not row.get("id")
                    or set(row.get("data", {})) - EFFECT_FIELDS[row["table"]]
                ):
                    raise HTTPException(409, "Effect journal has invalid recovery fields")
        return entries

    def replay_effects_in(self, s, *, p=None, entries):
        """Fill missing metadata only. Existing state and authorization always win."""
        latest = {}
        for entry in entries:
            scope = (entry["tenant_id"], entry["seller_id"])
            if p and scope != (p["tenant_id"], p["seller_id"]):
                continue
            for entity in entry.get("entities", []):
                key = (*scope, entity["table"], entity["id"])
                old = latest.get(key, {})
                if old and entity["external_key"] != old["external_key"]:
                    raise HTTPException(409, "Effect journal contains conflicting request identities")
                if old.get("version", 0) > entity["version"]:
                    continue
                latest[key] = {**entity, "data": {**old.get("data", {}), **entity["data"]}}
                if entry.get("provider_id") and (
                    entry["kind"] == "dial"
                    and entity["table"] == "call_attempts"
                    or entry["kind"] == "agent"
                    and entity["table"] == "call_legs"
                ):
                    latest[key]["data"]["provider_id"] = entry["provider_id"]
                previous_provider = old.get("data", {}).get("provider_id")
                current_provider = latest[key]["data"].get("provider_id")
                if previous_provider and current_provider and current_provider != previous_provider:
                    raise HTTPException(409, "Effect journal contains conflicting provider identities")
                if previous_provider and not current_provider:
                    latest[key]["data"]["provider_id"] = previous_provider
        recovered = 0
        for (tenant, seller_id, kind, identity), entity in latest.items():
            seller = get(s, "sellers", seller_id, tenant)
            if not seller:
                continue  # Replayed after the scoped backup creates its seller.
            existing = get(s, kind, identity)
            if existing:
                if (existing.tenant_id, existing.seller_id, existing.external_key) != (
                    tenant,
                    seller_id,
                    entity["external_key"],
                ):
                    raise HTTPException(409, "Effect recovery identity conflicts with current data")
                continue
            scope = {"tenant_id": tenant, "seller_id": seller_id}
            if entity["external_key"] and self._by_key(s, scope, kind, entity["external_key"]):
                raise HTTPException(409, "Effect recovery request conflicts with current data")
            data = dict(entity["data"])
            if kind == "command_outbox" and data.get("kind") == "script_batch":
                from app.migrate import BATCH_CLOSED, RETIRED_BATCH_REASON

                if data.get("status") not in BATCH_CLOSED:
                    data.update(
                        status="cancelled",
                        previous_status=data.get("status"),
                        recovery_reason=RETIRED_BATCH_REASON,
                    )
            elif kind == "command_outbox":
                data.update(
                    status="review_required",
                    restore_hold=True,
                    payload={"command_id": data.get("command_id")},
                    recovery_reason="Recovered original effect; verify before any repeat",
                )
            elif kind in {"call_attempts", "call_legs"} and data.get("state") not in {
                "ENDED",
                "FINALIZED",
                "ended",
            }:
                data["state"] = "RECONCILING" if kind == "call_attempts" else "uncertain"
            elif kind == "usage_ledger" and data.get("status") not in {"settled", "released"}:
                data["status"] = "uncertain"
            elif kind == "registry_snapshots":
                data.update(status="review_required", published=False, listed=None)
            restored = put(
                s,
                kind,
                tenant,
                seller_id,
                data,
                object_id=identity,
                key=entity["external_key"],
                now=datetime.fromisoformat(entity["created_at"]),
            )
            restored.version = entity["version"]
            update(seller, restore_hold=True)
            recovered += 1
        return recovered

    def replay_effects(self):
        try:
            entries = self.effect_entries()
            if entries:
                with self.db.transaction() as s:
                    self.replay_effects_in(s, entries=entries)
        except Exception:
            # Keep the app available for stopping/recovery; new effects are denied.
            self._effect_journal_fault = True

    def repair_effect_journal(self, p):
        """Explicit storage recovery only; never resubmit the represented effects."""
        self.effect_entries()  # Cannot append through truncated or missing segments.
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            self._restore_operator(s, p)
            seller = self.required(s, "sellers", p["seller_id"], p)
            if seller.data.get("effect_journal_gap"):
                raise HTTPException(
                    409, "Effect journal gap requires original carrier/account reconciliation"
                )
            pending = seller.data.get("effect_journal_pending", {})
            for entry in pending.values():
                self.vault.append_journal("effects", entry)
            self.vault.journal_checkpoint("effects")
            if pending:
                update(seller, effect_journal_pending={})
        self._effect_journal_fault = False

    def _request_hold(self, s, p, request_key):
        model = MODELS["usage_ledger"]
        return s.scalar(
            select(model).where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.external_key == "optional:" + request_key,
            )
        )

    def reserve_optional_budget(self, p, request_key, estimate_usd, *, object_id=None, work_kind=None):
        with self.db.transaction() as s:
            return self.reserve_budget_in(
                s, p, request_key, estimate_usd, object_id=object_id, work_kind=work_kind
            )

    def reserve_budget_in(self, s, p, request_key, estimate_usd, *, object_id=None, work_kind=None):
        if (
            not isinstance(request_key, str)
            or not request_key
            or isinstance(estimate_usd, bool)
            or not isinstance(estimate_usd, (int, float))
            or not math.isfinite(estimate_usd)
            or estimate_usd <= 0
        ):
            raise ValueError("Optional work requires a request identity and positive reserved estimate")
        # Serialize only this seller's budget/authorization stream, never provider I/O.
        self.db.authorize_stream(s, p)
        seller = get(s, "sellers", p["seller_id"], p["tenant_id"])
        if (
            getattr(self, "_effect_journal_fault", False)
            or seller
            and seller.data.get("effect_journal_pending")
        ):
            raise HTTPException(503, "EFFECT_JOURNAL_RECOVERY_REQUIRED")
        prior = self._request_hold(s, p, request_key)
        if prior:
            data = prior.data
            if data.get("object_id") != object_id or data.get("work_kind") != work_kind:
                raise ValueError("Request identity belongs to different work")
            if data.get("status") != "reserved":
                return False
            if data.get("estimate_usd", data.get("reserved_usd")) != estimate_usd:
                raise ValueError("Request identity already has a different budget")
            return True
        # A second admission in this transaction must include the preceding writes.
        s.info.pop(("budget_totals", p["tenant_id"], p["seller_id"]), None)
        if not self.budget_ok(s, p, estimate_usd):
            return False
        hold = put(
            s,
            "usage_ledger",
            p["tenant_id"],
            p["seller_id"],
            {
                "kind": "optional_work_reservation",
                "object_id": object_id,
                "work_kind": work_kind,
                "user_id": p.get("user_id"),
                "request_key": request_key,
                "estimate_usd": estimate_usd,
                "reserved_usd": estimate_usd,
                "at": self.clock().isoformat(),
                "cost_usd": None,
                "billing_verified": False,
                "status": "reserved",
            },
            key="optional:" + request_key,
            now=self.clock(),
        )
        self.journal_effect_in(s, p, work_kind or "optional_work", request_key, "intent", [hold])
        # Admission precedes every possible submission; a crash may happen on either side
        # of the network write. Only release_unsubmitted_budget proves it stayed unsent.
        self.journal_effect_in(s, p, work_kind or "optional_work", request_key, "submitted", [hold])
        return True

    def usage(self, p, provider, kind, result, *, object_id, request_key, reserved=0.01, session=None):
        from app.privacy import usage_metadata

        if not isinstance(request_key, str) or not request_key:
            raise ValueError("Usage must identify its original local request")
        usage = usage_metadata(result.usage)
        cost = usage.get("cost", usage.get("cost_usd"))
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(cost)
            or cost < 0
        ):
            cost = None
        if (
            isinstance(reserved, bool)
            or not isinstance(reserved, (int, float))
            or not math.isfinite(reserved)
            or reserved < 0
        ):
            raise ValueError("A finite nonnegative liability is required")
        with self.db.transaction(session=session) as s:
            self.db.authorize_stream(s, p)
            model = MODELS["usage_ledger"]
            receipts = (
                list(
                    s.scalars(
                        select(model)
                        .where(
                            model.tenant_id == p["tenant_id"],
                            model.seller_id == p["seller_id"],
                            model.op_provider_id
                            == receipt_key(provider, result.request_id, self.db.identity_key),
                        )
                        .limit(2)
                    )
                )
                if result.request_id
                else []
            )
            if len(receipts) > 1:
                raise ValueError("Duplicate historical provider receipts require reconciliation")
            receipt = receipts[0] if receipts else None
            hold = self._request_hold(s, p, request_key)
            if receipt and (not hold or receipt.id != hold.id):
                raise ValueError("Provider receipt belongs to a different request")
            target = receipt or hold
            if target:
                data = target.data
                if (
                    data.get("object_id") != object_id
                    or data.get("work_kind", data.get("kind")) != kind
                    or data.get("provider")
                    and data["provider"] != provider
                    or data.get("request_id")
                    and result.request_id
                    and data["request_id"] != result.request_id
                ):
                    raise ValueError("Provider receipt attribution conflict")
                if data.get("estimate_source") and cost is None:
                    return target.id  # An unpriced receipt cannot erase the standing estimate.
                if data.get("cost_usd") is not None and not data.get("estimate_source"):
                    if cost is not None and cost != data["cost_usd"]:
                        raise ValueError("Conflicting cost receipt requires reconciliation")
                    return target.id
            values = {
                "provider": provider,
                "kind": kind,
                "work_kind": kind,
                "object_id": object_id,
                "request_id": result.request_id or (target.data if target else {}).get("request_id"),
                "usage": usage,
                "received_at": self.clock().isoformat(),
                "reserved_usd": 0
                if cost is not None
                else max(reserved, (target.data if target else {}).get("reserved_usd", 0)),
                "cost_usd": cost,
                "billing_verified": cost is not None,
                "status": "settled" if cost is not None else "uncertain",
                "estimate_source": None,
            }
            if target:
                update(target, **values)
            else:
                target = put(
                    s,
                    "usage_ledger",
                    p["tenant_id"],
                    p["seller_id"],
                    {**values, "request_key": request_key, "at": self.clock().isoformat()},
                    key="optional:" + request_key,
                    now=self.clock(),
                )
            self.journal_effect_in(
                s, p, kind, request_key, "receipt", [target], provider_id=result.request_id, emergency=True
            )
            return target.id

    def settle_estimate_in(self, s, p, request_key, cost, source):
        """A definitive answer without a priced receipt: the hold becomes a labeled estimate.

        It counts against budgets at the estimate instead of holding forever; a later provider
        receipt replaces it through `usage` or billing reconciliation.
        """
        self.db.authorize_stream(s, p)
        hold = self._request_hold(s, p, request_key)
        if not hold or hold.data.get("cost_usd") is not None or hold.data.get("status") == "released":
            return hold
        update(
            hold,
            cost_usd=cost,
            reserved_usd=0,
            status="estimated",
            billing_verified=False,
            estimate_source=source,
            received_at=self.clock().isoformat(),
        )
        self.journal_effect_in(
            s,
            p,
            hold.data.get("work_kind") or "optional_work",
            request_key,
            "receipt",
            [hold],
            emergency=True,
        )
        return hold

    def release_refused_budget(self, p, request_key, reason):
        """The provider refused before doing any work, so the request carries no charge."""
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            hold = self._request_hold(s, p, request_key)
            if hold and hold.data.get("cost_usd") is None and hold.data.get("status") != "released":
                update(
                    hold,
                    status="released",
                    cost_usd=0,
                    reserved_usd=0,
                    billing_verified=False,
                    review_reason=reason,
                )
                self.journal_effect_in(
                    s,
                    p,
                    hold.data.get("work_kind") or "optional_work",
                    request_key,
                    "receipt",
                    [hold],
                    emergency=True,
                )

    def release_unsubmitted_budget(self, p, request_key):
        """Use only when no provider request was submitted; timeouts remain liabilities."""
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            target = self._request_hold(s, p, request_key)
            if target and target.data.get("status") == "reserved" and not target.data.get("request_id"):
                update(target, status="released", cost_usd=0, reserved_usd=0, billing_verified=False)
                self.journal_effect_in(
                    s,
                    p,
                    target.data.get("work_kind") or "optional_work",
                    request_key,
                    "receipt",
                    [target],
                    emergency=True,
                )
