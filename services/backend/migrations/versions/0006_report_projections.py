"""Saved encrypted reports and transactional source generations."""

import sqlalchemy as sa
from alembic import op

revision = "0006_report_projections"
down_revision = "0005_context_reads"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "report_revisions" not in existing:
        op.create_table(
            "report_revisions",
            sa.Column("tenant_id", sa.String(36), primary_key=True),
            sa.Column("seller_id", sa.String(36), primary_key=True),
            sa.Column("generation", sa.Integer(), nullable=False),
        )
    if "report_projections" not in existing:
        op.create_table(
            "report_projections",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("tenant_id", sa.String(36), nullable=False, index=True),
            sa.Column("seller_id", sa.String(36), nullable=False, index=True),
            sa.Column("external_key", sa.String(512)),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("tenant_id", "seller_id", "external_key", name="uq_report_projections_key"),
        )


def downgrade():
    raise RuntimeError("Use a reviewed compatible backup; no destructive report downgrade")
