"""Indexed non-content metadata for selecting work without decrypting its history."""

import hashlib
import hmac
import json
from datetime import UTC, datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Index, Integer, String, inspect, text

HOT_TABLES = {
    "leads",
    "call_attempts",
    "call_legs",
    "number_inventory",
    "number_leases",
    "reservations",
    "command_outbox",
    "webhook_inbox",
    "followup_tasks",
    "commitments",
    "conversion_events",
    "feature_snapshots",
    "experiment_assignments",
    "scheduling_plans",
    "usage_ledger",
}

# Only the relationship/expiry fields used to assemble a single call's context.
CONTEXT_FIELDS = {
    "registry_snapshots": ("kind", "status", None),
    "research_facts": ("domain", "status", "expires_at"),
    "research_sources": ("domain", None, None),
    "script_versions": ("lead_id", None, None),
    "consent_evidence": ("endpoint_id", None, None),
    "transcript_segments": ("call_id", None, "raw_expires_at"),
}


def context_key(kind, value, secret):
    if value is None:
        return None
    if CONTEXT_FIELDS[kind][0] == "domain":
        return hmac.new(secret, value.lower().rstrip(".").encode(), hashlib.sha256).hexdigest()
    return value


def context_columns(kind):
    _, status, expiry = CONTEXT_FIELDS[kind]
    return {
        "ctx_key": Column(String(100), nullable=True),
        "ctx_index_version": Column(Integer, nullable=True),
        **({"ctx_status": Column(String(40), nullable=True)} if status else {}),
        **({"ctx_expires_at": Column(DateTime(timezone=True), nullable=True)} if expiry else {}),
    }


def context_indexes(kind):
    _, status, expiry = CONTEXT_FIELDS[kind]
    return (
        Index(
            f"ix_{kind}_context",
            "tenant_id",
            "seller_id",
            "ctx_key",
            *(["ctx_status"] if status else []),
            *(["ctx_expires_at"] if expiry else ["created_at"]),
        ),
    )


def columns():
    return {
        **{
            k: Column(String(100), nullable=True)
            for k in (
                "op_status",
                "op_campaign_id",
                "op_lead_id",
                "op_endpoint_id",
                "op_number_id",
                "op_owner_id",
                "op_provider_id",
            )
        },
        "op_due_at": Column(DateTime(timezone=True), nullable=True),
        "op_coverage_at": Column(DateTime(timezone=True), nullable=True),
        "op_at": Column(DateTime(timezone=True), nullable=True),
        "op_priority": Column(Integer, nullable=True),
        "op_active": Column(Boolean, nullable=True),
        "op_amount": Column(Float, nullable=True),
        "op_index_version": Column(Integer, nullable=True),
    }


def indexes(name):
    return (
        Index(
            f"ix_{name}_work",
            "tenant_id",
            "seller_id",
            "op_campaign_id",
            "op_status",
            "op_due_at",
            "op_coverage_at",
        ),
        Index(f"ix_{name}_lead", "tenant_id", "seller_id", "op_lead_id", "op_at"),
        Index(f"ix_{name}_time", "tenant_id", "seller_id", "op_at"),
        *(
            (Index("ix_usage_ledger_receipt", "tenant_id", "seller_id", "op_provider_id"),)
            if name == "usage_ledger"
            else ()
        ),
    )


def receipt_key(provider, request_id, secret):
    if not provider or not request_id:
        return None
    return hmac.new(
        secret, json.dumps([provider, request_id], separators=(",", ":")).encode(), hashlib.sha256
    ).hexdigest()


def as_time(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def sync_columns(row, lookup_key=None):
    if row.__tablename__ in CONTEXT_FIELDS:
        key, status, expiry = CONTEXT_FIELDS[row.__tablename__]
        row.ctx_key = context_key(row.__tablename__, row.data.get(key), lookup_key)
        if status:
            row.ctx_status = row.data.get(status)
        if expiry:
            row.ctx_expires_at = as_time(row.data.get(expiry))
        row.ctx_index_version = 1
        return
    if row.__tablename__ == "work_intervals":
        row.op_owner_id = row.data["rep_id"]
        return
    if row.__tablename__ not in HOT_TABLES:
        return
    data = row.data
    row.op_status = data.get("status", data.get("state", data.get("lifecycle")))
    for name in ("campaign_id", "lead_id", "endpoint_id", "number_id", "provider_id"):
        setattr(row, "op_" + name, data.get(name))
    row.op_owner_id = data.get("rep_id", data.get("owner_id"))
    row.op_due_at = as_time(data.get("callback_due_at") or data.get("next_attempt_at") or data.get("due_at"))
    row.op_coverage_at = as_time(data.get("coverage_due_at"))
    row.op_at = (
        as_time(data.get("started_at") or data.get("at") or data.get("as_of") or data.get("occurred_at"))
        or row.created_at
    )
    row.op_priority = 2 if data.get("callback_due_at") else 1 if data.get("requested_contact") else 0
    row.op_active = bool(data.get("active"))
    row.op_amount = float(
        data.get("cost_usd") if data.get("cost_usd") is not None else data.get("reserved_usd") or 0
    )
    if row.__tablename__ == "usage_ledger" and data.get("cost_usd") is not None:
        from decimal import Decimal

        row.op_amount = float(Decimal(str(data["cost_usd"])) - Decimal(str(data.get("credited_usd", 0))))
    row.op_index_version = 1
    if row.__tablename__ == "command_outbox":
        row.op_kind = data.get("kind")
        row.op_index_version = 2
    if row.__tablename__ == "usage_ledger":
        row.op_provider_id = receipt_key(
            data.get("provider"), data.get("request_id") or data.get("provider_record_id"), lookup_key
        )
        row.op_index_version = 3


def ensure_columns(connection, metadata):
    """Additive DDL only; authenticated bounded backfill happens under the authority key."""
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    for name in sorted((HOT_TABLES | CONTEXT_FIELDS.keys()) & existing):
        actual = {c["name"] for c in inspector.get_columns(name)}
        definitions = columns() if name in HOT_TABLES else context_columns(name)
        if name == "command_outbox":
            definitions["op_kind"] = Column(String(40), nullable=True)
        for column, definition in definitions.items():
            if column not in actual:
                sql_type = definition.type.compile(dialect=connection.dialect)
                connection.execute(text(f'ALTER TABLE "{name}" ADD COLUMN "{column}" {sql_type}'))  # noqa: S608 fixed table/column allowlists
        for index in metadata.tables[name].indexes:
            index.create(connection, checkfirst=True)
