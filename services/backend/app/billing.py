"""Local receipt review; never invokes a provider or treats a partial CDR as a final invoice."""

import hashlib
import json
from decimal import Decimal
from typing import Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, or_, select

from app.db import MODELS, put, update
from app.operational_schema import receipt_key
from app.providers.inference import InferenceResult
from app.service import fail
from app.telephony.reducer import TERMINAL


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class ReceiptEdit(Strict):
    expected_version: int = Field(ge=1)
    action: Literal["settle", "credit", "close_call"]
    provider: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    receipt_id: str = Field(min_length=1, max_length=200)
    source_reference: str = Field(min_length=1, max_length=500)
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    cost_usd: float = Field(ge=0, strict=True)
    attest_original: Literal[True]
    provider_record_ids: list[str] = Field(default_factory=list, max_length=1000)
    confirm_complete: bool = False
    confirm_no_charge: bool = False


class CarrierRecord(Strict):
    provider_record_id: str = Field(min_length=1, max_length=200)
    call_id: str | None = Field(default=None, min_length=1, max_length=36)
    callback_id: str | None = Field(default=None, min_length=1, max_length=36)
    leg_id: str | None = Field(default=None, max_length=200)
    kind: str = Field(default="voice", min_length=1, max_length=100)
    request_key: str | None = Field(default=None, min_length=1, max_length=200)
    cost_usd: float = Field(ge=0, strict=True)
    duration_seconds: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def one_identity(self):
        if bool(self.call_id) == bool(self.callback_id):
            raise ValueError("Choose one call or callback identity")
        return self


class CarrierImport(Strict):
    source_reference: str = Field(min_length=1, max_length=500)
    rows: list[CarrierRecord] = Field(max_length=1000)


def scoped(kind, p):
    table = MODELS[kind]
    return select(table).where(table.tenant_id == p["tenant_id"], table.seller_id == p["seller_id"])


def by_key(s, kind, p, key):
    return s.scalar(scoped(kind, p).where(MODELS[kind].external_key == key))


def net_cost(data):
    if data.get("cost_usd") is None:
        return None
    return float(Decimal(str(data["cost_usd"])) - Decimal(str(data.get("credited_usd", 0))))


def view(row):
    data = row.data
    call_hold = data.get("kind") == "call_authorization"
    status = data.get("status", "uncertain")
    return {
        "id": row.id,
        "version": row.version,
        **{
            key: data.get(key)
            for key in (
                "kind",
                "work_kind",
                "provider",
                "request_id",
                "request_key",
                "at",
                "cost_usd",
                "source_reference",
                "receipt_id",
                "review_reason",
            )
        },
        "object_id": data.get("object_id", data.get("call_id")),
        "status": status,
        "reserved_usd": data.get("reserved_usd", 0),
        "credited_usd": data.get("credited_usd", 0),
        "net_cost_usd": net_cost(data),
        "provider_record_ids": data.get("provider_record_ids", []),
        "can_settle": not call_hold and status in {"uncertain", "submitted"} and data.get("cost_usd") is None,
        "can_credit": not call_hold and status == "settled" and (net_cost(data) or 0) > 0,
        "can_close_call": call_hold and data.get("reserved_usd", 0) > 0,
    }


def proof_key(service, provider, identity):
    return "billing:" + receipt_key(provider, identity, service.db.identity_key)


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def prior_proof(service, s, p, provider, identity, digest):
    row = by_key(s, "audit_events", p, proof_key(service, provider, identity))
    if row and row.data.get("detail", {}).get("digest") != digest:
        fail(409, "Provider receipt already belongs to a different amount or operation")
    return row.data["detail"]["result"] if row else None


def store_proof(service, s, p, provider, identity, digest, target, action, source, result):
    put(
        s,
        "audit_events",
        p["tenant_id"],
        p["seller_id"],
        {
            "actor_id": p["user_id"],
            "action": "billing." + action,
            "target_id": target,
            "detail": {
                "provider": provider,
                "receipt_id": identity,
                "source_reference": source,
                "digest": digest,
                "result": result,
            },
        },
        key=proof_key(service, provider, identity),
        now=service.clock(),
    )


def active_admin(service, s, p):
    user = service.required(s, "users", p["user_id"], p)
    if user.data.get("disabled") or user.data.get("role") not in {"owner", "admin"}:
        fail(403, "An active administrator must check provider receipts")


def close_call(service, s, p, row, data):
    call = service.required(s, "call_attempts", row.data.get("call_id"), p)
    expected = row.data.get("provider_record_ids", [])
    if (
        call.data.get("state") not in TERMINAL
        or not data.confirm_complete
        or data.cost_usd != 0
        or (not expected and not data.confirm_no_charge)
        or (bool(expected) and data.confirm_no_charge)
        or len(set(data.provider_record_ids)) != len(data.provider_record_ids)
        or set(data.provider_record_ids) != set(expected)
    ):
        fail(409, "A terminal call and checked complete set of imported provider records are required")
    if row.data.get("status") == "settled":
        fail(409, "Call completion was already reconciled; reuse its original receipt")
    update(
        row,
        cost_usd=0,
        reserved_usd=0,
        status="settled",
        billing_verified=True,
        estimate_usd=row.data.get("estimate_usd", row.data.get("reserved_usd", 0)),
        reconciled_to_provider_records=True,
        review_reason=None,
    )


def reconcile_entry(service, p, identity, data):
    digest = fingerprint({"entry_id": identity, **data.model_dump(exclude={"expected_version"})})
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        active_admin(service, s, p)
        row = service.required(s, "usage_ledger", identity, p)
        prior = prior_proof(service, s, p, data.provider, data.receipt_id, digest)
        if prior:
            return prior
        if row.version != data.expected_version:
            fail(409, "Usage changed; reload before recording this receipt")
        current = row.data
        if current.get("provider") != data.provider or current.get("request_id") != data.request_id:
            fail(409, "Receipt must identify the original provider request")
        if not data.receipt_id.strip() or not data.source_reference.strip():
            fail(422, "A provider receipt and its original source reference are required")
        if data.action == "close_call":
            if current.get("kind") != "call_authorization":
                fail(409, "Only a call estimate can be closed using a complete carrier statement")
            close_call(service, s, p, row, data)
        elif data.action == "credit":
            if not view(row)["can_credit"] or data.cost_usd <= 0:
                fail(409, "Credits require a settled positive charge")
            credit = Decimal(str(current.get("credited_usd", 0))) + Decimal(str(data.cost_usd))
            if credit > Decimal(str(current["cost_usd"])):
                fail(409, "Credit exceeds the remaining original charge")
            update(row, credited_usd=float(credit), credit_received_at=service.clock().isoformat())
        else:
            if not view(row)["can_settle"] or not current.get("request_key"):
                fail(409, "Only a submitted uncertain request can be settled by this receipt")
            try:
                service.usage(
                    p,
                    data.provider,
                    current.get("work_kind", current["kind"]),
                    InferenceResult({}, {"cost": data.cost_usd}, data.request_id),
                    object_id=current.get("object_id"),
                    request_key=current["request_key"],
                    session=s,
                )
            except ValueError as exc:
                fail(409, str(exc))
        update(
            row,
            source_reference=data.source_reference,
            receipt_id=data.receipt_id,
            billing_evidence="operator-imported provider record",
        )
        result = {
            "entry": view(row),
            "evidence": "operator-imported provider record",
            "invoice_verified": False,
        }
        store_proof(
            service,
            s,
            p,
            data.provider,
            data.receipt_id,
            digest,
            identity,
            data.action,
            data.source_reference,
            result,
        )
        return result


def import_records(service, p, data):
    matched = duplicates = duration = 0
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        active_admin(service, s, p)
        for record in data.rows:
            call_id = record.callback_id or record.call_id
            call = service.required(
                s, "followup_tasks" if record.callback_id else "call_attempts", call_id, p
            )
            if record.callback_id and not call.data.get("inbound"):
                fail(422, "Callback billing requires an inbound call")
            digest = fingerprint({"carrier_record": record.model_dump()})
            if prior_proof(service, s, p, "telnyx", record.provider_record_id, digest):
                duplicates += 1
                continue
            hold = service._request_hold(s, p, record.request_key) if record.request_key else None
            if record.request_key and (
                not hold
                or hold.data.get("object_id") != call_id
                or hold.data.get("work_kind") != record.kind
                or hold.data.get("provider") not in {None, "telnyx"}
            ):
                fail(409, "Carrier receipt does not match its original reserved action")
            model = MODELS["usage_ledger"]
            matches = list(
                s.scalars(
                    scoped("usage_ledger", p)
                    .where(
                        or_(
                            model.external_key == "cdr:" + record.provider_record_id,
                            model.op_provider_id
                            == receipt_key("telnyx", record.provider_record_id, service.db.identity_key),
                        )
                    )
                    .limit(2)
                )
            )
            if len(matches) > 1:
                fail(409, "Duplicate historical carrier identities require statement review")
            existing = matches[0] if matches else None
            if not existing and hold and hold.data.get("provider_record_id") == record.provider_record_id:
                existing = hold
            if existing:
                expected = {
                    "cost_usd": record.cost_usd,
                    "duration_seconds": record.duration_seconds,
                    "leg_id": record.leg_id,
                    "kind": record.kind,
                }
                if existing.data.get("object_id", existing.data.get("call_id")) != call_id or any(
                    existing.data.get(key) != value for key, value in expected.items()
                ):
                    fail(409, "Billing receipt attribution conflicts")
                duplicates += 1
            else:
                if hold and (hold.data.get("cost_usd") is not None or hold.data.get("provider_record_id")):
                    fail(409, "Original request already has a different billing receipt")
                values = {
                    **record.model_dump(exclude_none=True),
                    "object_id": call_id,
                    "provider": "telnyx",
                    "source_reference": data.source_reference,
                    "billing_verified": True,
                    "status": "settled",
                    "reserved_usd": 0,
                    "at": (hold.data if hold else {}).get("at", service.clock().isoformat()),
                }
                if hold:
                    update(hold, **values)
                    existing = hold
                else:
                    existing = put(
                        s,
                        "usage_ledger",
                        p["tenant_id"],
                        p["seller_id"],
                        values,
                        key="cdr:" + record.provider_record_id,
                        now=service.clock(),
                    )
                matched += 1
                duration += record.duration_seconds
            if record.call_id:
                call_budget = by_key(s, "usage_ledger", p, "call:" + call_id)
                if call_budget:
                    known = call_budget.data.get("provider_record_ids", [])
                    if record.provider_record_id not in known:
                        if len(known) >= 1000:
                            fail(409, "Call receipt limit reached; resolve the original statement")
                        changes = {"provider_record_ids": [*known, record.provider_record_id]}
                        if call_budget.data.get("reconciled_to_provider_records"):
                            changes.update(
                                status="uncertain",
                                cost_usd=None,
                                billing_verified=False,
                                reserved_usd=call_budget.data.get("estimate_usd", 0.05),
                                reconciled_to_provider_records=False,
                                review_reason="Additional carrier receipt arrived after the statement was marked complete",
                            )
                        update(call_budget, **changes)
            store_proof(
                service,
                s,
                p,
                "telnyx",
                record.provider_record_id,
                digest,
                existing.id,
                "carrier_import",
                data.source_reference,
                {"entry_id": existing.id},
            )
    return {
        "matched": matched,
        "duplicates": duplicates,
        "duration_seconds": duration,
        "evidence": "operator-imported provider records",
        "invoice_verified": False,
    }


def bind_api(app, service, administrator):
    @app.get("/v1/admin/billing/ledger")
    def ledger(
        status: Literal["unresolved", "settled", "all"] = "unresolved",
        cursor: str | None = None,
        p=Depends(administrator),
    ):
        with service.db.transaction(False) as s:
            model = MODELS["usage_ledger"]
            query = scoped("usage_ledger", p)
            if status == "unresolved":
                query = query.where(
                    or_(
                        model.op_status.in_(["uncertain", "submitted", "reserved"]),
                        and_(model.op_status.is_(None), model.op_amount > 0),
                    )
                )
            elif status == "settled":
                query = query.where(model.op_status == "settled")
            if cursor:
                row = service.required(s, "usage_ledger", cursor, p)
                query = query.where(
                    or_(
                        model.created_at > row.created_at,
                        and_(model.created_at == row.created_at, model.id > row.id),
                    )
                )
            found = list(s.scalars(query.order_by(model.created_at, model.id).limit(51)))
            return {
                "entries": [view(row) for row in found[:50]],
                "next_cursor": found[49].id if len(found) > 50 else None,
            }

    @app.post("/v1/admin/billing/ledger/{identity}/reconcile")
    def reconcile(identity: str, data: ReceiptEdit, p=Depends(administrator)):
        return reconcile_entry(service, p, identity, data)

    @app.post("/v1/admin/billing/reconcile")
    def carrier_records(data: CarrierImport, p=Depends(administrator)):
        return import_records(service, p, data)
