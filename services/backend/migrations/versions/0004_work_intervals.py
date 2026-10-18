"""Retain declared work intervals separately from call attempt duration."""

import sqlalchemy as sa
from alembic import op

revision = "0004_work_intervals"
down_revision = "0003_operational_indexes"
branch_labels = None
depends_on = None


def upgrade():
    if "work_intervals" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "work_intervals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("seller_id", sa.String(36), nullable=False),
        sa.Column("external_key", sa.String(512), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("op_owner_id", sa.String(36), nullable=False),
        sa.UniqueConstraint("tenant_id", "seller_id", "external_key", name="uq_work_intervals_key"),
    )
    op.create_index("ix_work_intervals_tenant_id", "work_intervals", ["tenant_id"])
    op.create_index("ix_work_intervals_seller_id", "work_intervals", ["seller_id"])
    op.create_index(
        "ix_work_intervals_owner", "work_intervals", ["tenant_id", "seller_id", "op_owner_id", "created_at"]
    )


def downgrade():
    raise RuntimeError("Restore a reviewed compatible backup; do not discard working-time corrections")
