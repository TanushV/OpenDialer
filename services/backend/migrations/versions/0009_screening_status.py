"""Bounded seller-scoped screening recovery without decrypting cached phone lists."""

import sqlalchemy as sa
from alembic import op

revision = "0009_screening_status"
down_revision = "0008_usage_receipts"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    present = {c["name"] for c in inspector.get_columns("registry_snapshots")}
    for name, kind in (
        ("ctx_key", sa.String(100)),
        ("ctx_status", sa.String(40)),
        ("ctx_index_version", sa.Integer()),
    ):
        if name not in present:
            op.add_column("registry_snapshots", sa.Column(name, kind, nullable=True))
    if "ix_registry_snapshots_context" not in {
        i["name"] for i in inspector.get_indexes("registry_snapshots")
    }:
        op.create_index(
            "ix_registry_snapshots_context",
            "registry_snapshots",
            ["tenant_id", "seller_id", "ctx_key", "ctx_status", "created_at"],
        )


def downgrade():
    raise RuntimeError("Use a reviewed compatible backup; no destructive registry downgrade")
