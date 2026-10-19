"""Index call-scoped observation lineage and local edit receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0016_completion_context"
down_revision = "0015_review_context"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    existing = {c["name"] for c in inspector.get_columns("outcomes")}
    for name, kind in (
        ("ctx_key", sa.String(100)),
        ("ctx_status", sa.String(40)),
        ("ctx_index_version", sa.Integer()),
    ):
        if name not in existing:
            op.add_column("outcomes", sa.Column(name, kind, nullable=True))
    if "ix_outcomes_context" not in {i["name"] for i in inspector.get_indexes("outcomes")}:
        op.create_index(
            "ix_outcomes_context",
            "outcomes",
            ["tenant_id", "seller_id", "ctx_key", "ctx_status", "created_at", "id"],
        )


def downgrade():
    raise RuntimeError("Use a compatible build; retain conversation lineage and edit identities")
