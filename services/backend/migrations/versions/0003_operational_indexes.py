"""Add indexed operational metadata; preserve encrypted payloads and source versions.

This migration is frozen: later application schema changes must not change an old upgrade.
"""

from alembic import op
from sqlalchemy import inspect

revision = "0003_operational_indexes"
down_revision = "0002_optimization"
branch_labels = None
depends_on = None

TABLES = (
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
)
STRINGS = ("status", "campaign_id", "lead_id", "endpoint_id", "number_id", "owner_id", "provider_id")


def upgrade():
    connection = op.get_bind()
    inspector = inspect(connection)
    time_type = "TIMESTAMP WITH TIME ZONE" if connection.dialect.name == "postgresql" else "DATETIME"
    fields = {
        **{"op_" + name: "VARCHAR(100)" for name in STRINGS},
        **{"op_" + name: time_type for name in ("due_at", "coverage_at", "at")},
        "op_priority": "INTEGER",
        "op_active": "BOOLEAN",
        "op_amount": "FLOAT",
        "op_index_version": "INTEGER",
    }
    for table in TABLES:
        actual = {c["name"] for c in inspector.get_columns(table)}
        for name, sql_type in fields.items():
            if name not in actual:
                connection.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {sql_type}')  # noqa: S608 fixed identifiers
        present = {index["name"] for index in inspector.get_indexes(table)}
        for suffix, columns in {
            "work": ["op_campaign_id", "op_status", "op_due_at", "op_coverage_at"],
            "lead": ["op_lead_id", "op_at"],
            "time": ["op_at"],
        }.items():
            name = f"ix_{table}_{suffix}"
            if name not in present:
                op.create_index(name, table, ["tenant_id", "seller_id", *columns])


def downgrade():
    raise RuntimeError("Use a reviewed compatible backup; no destructive index/data downgrade")
