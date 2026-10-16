"""Add encrypted optimization records without rewriting existing application data."""

import sqlalchemy as sa
from alembic import op

revision = "0002_optimization"
down_revision = "0001_initial"
branch_labels = None
depends_on = None
TABLES = (
    "learning_datasets",
    "conversion_events",
    "allocation_decisions",
    "scheduling_plans",
    "evaluation_reports",
    "coaching_feedback",
)


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for name in TABLES:
        if name in existing:
            continue
        op.create_table(
            name,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("tenant_id", sa.String(36), nullable=False),
            sa.Column("seller_id", sa.String(36), nullable=False),
            sa.Column("external_key", sa.String(512), nullable=True),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("tenant_id", "seller_id", "external_key", name=f"uq_{name}_key"),
        )
        op.create_index(f"ix_{name}_tenant_id", name, ["tenant_id"])
        op.create_index(f"ix_{name}_seller_id", name, ["seller_id"])


def downgrade():
    raise RuntimeError(
        "Restore a reviewed backup to downgrade; never discard optimization/conversion evidence"
    )
