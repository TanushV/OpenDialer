"""Disposable encrypted metric contributions and aggregate buckets; no inline replay."""

import sqlalchemy as sa
from alembic import op

revision = "0017_metric_projections"
down_revision = "0016_completion_context"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "op_call_id" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("usage_ledger")}:
        op.add_column("usage_ledger", sa.Column("op_call_id", sa.String(100), nullable=True))
    for name, columns in {
        "work_intervals": ("op_at", "op_due_at"),
        "call_attempts": ("op_work_end_at",),
    }.items():
        actual = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(name)}
        for column in columns:
            if column not in actual:
                op.add_column(name, sa.Column(column, sa.DateTime(timezone=True), nullable=True))
    for name in ("metric_contributions", "metric_buckets"):
        if name in existing:
            continue
        specific = (
            [
                sa.Column("source_kind", sa.String(24), nullable=False),
                sa.Column("source_id", sa.String(128), nullable=False),
                sa.Column("source_version", sa.BigInteger(), nullable=False),
                sa.Column("next_refresh_at", sa.DateTime(timezone=True), nullable=True),
            ]
            if name == "metric_contributions"
            else [
                sa.Column("bucket_day", sa.Date(), nullable=True),
                sa.Column("bucket_kind", sa.String(32), nullable=False),
                sa.Column("group_key", sa.String(64), nullable=False),
            ]
        )
        op.create_table(
            name,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("tenant_id", sa.String(36), nullable=False),
            sa.Column("seller_id", sa.String(36), nullable=False),
            sa.Column(
                "external_key",
                sa.String(512).with_variant(sa.String(512, collation="C"), "postgresql"),
                nullable=True,
            ),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("campaign_id", sa.String(36), nullable=True),
            sa.Column("definition_version", sa.String(64), nullable=False),
            *specific,
            sa.UniqueConstraint("tenant_id", "seller_id", "external_key", name=f"uq_{name}_key"),
        )
        for scope in ("tenant_id", "seller_id"):
            op.create_index(f"ix_{name}_{scope}", name, [scope])
    indexes = {
        "call_legs": {
            "ix_call_legs_provider_identity": ["tenant_id", "op_provider_id"],
            "ix_call_legs_rep_state": ["tenant_id", "seller_id", "op_owner_id", "op_status"],
        },
        "usage_ledger": {"ix_usage_ledger_call": ["tenant_id", "seller_id", "op_call_id"]},
        "leads": {"ix_leads_endpoint": ["tenant_id", "seller_id", "op_endpoint_id", "id"]},
        "work_intervals": {
            "ix_work_intervals_overlap": ["tenant_id", "seller_id", "op_owner_id", "op_due_at", "op_at"]
        },
        "call_attempts": {
            "ix_call_attempts_work_overlap": [
                "tenant_id",
                "seller_id",
                "op_owner_id",
                "op_work_end_at",
                "op_at",
            ]
        },
        "metric_contributions": {
            "ix_metric_contributions_due": ["tenant_id", "seller_id", "next_refresh_at"],
            "ix_metric_contributions_source": [
                "tenant_id",
                "seller_id",
                "campaign_id",
                "source_kind",
                "source_id",
                "definition_version",
            ],
        },
        "metric_buckets": {
            "ix_metric_buckets_group": [
                "tenant_id",
                "seller_id",
                "campaign_id",
                "definition_version",
                "bucket_kind",
                "bucket_day",
            ],
        },
    }
    for name, definitions in indexes.items():
        present = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(name)}
        for index, columns in definitions.items():
            if index not in present:
                op.create_index(index, name, columns)


def downgrade():
    raise RuntimeError("Use a compatible build; rebuild derived metrics without rolling back source data")
