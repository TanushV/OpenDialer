"""Indexed non-content metadata for selecting work without decrypting its history."""

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
    )


def as_time(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def sync_columns(row):
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
    row.op_index_version = 1


def ensure_columns(connection, metadata):
    """Additive DDL only; authenticated bounded backfill happens under the authority key."""
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    for name in sorted(HOT_TABLES & existing):
        actual = {c["name"] for c in inspector.get_columns(name)}
        for column, definition in columns().items():
            if column not in actual:
                sql_type = definition.type.compile(dialect=connection.dialect)
                connection.execute(text(f'ALTER TABLE "{name}" ADD COLUMN "{column}" {sql_type}'))  # noqa: S608 fixed table/column allowlists
        for index in metadata.tables[name].indexes:
            index.create(connection, checkfirst=True)
