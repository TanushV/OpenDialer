"""Find the selected call's human history without reading other conversations."""

import sqlalchemy as sa
from alembic import op

revision = "0015_review_context"
down_revision = "0014_current_knowledge"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    for table in ("reviews", "comments"):
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, kind in (("ctx_key", sa.String(100)), ("ctx_index_version", sa.Integer())):
            if name not in existing:
                op.add_column(table, sa.Column(name, kind, nullable=True))
        index = f"ix_{table}_context"
        if index not in {item["name"] for item in inspector.get_indexes(table)}:
            op.create_index(index, table, ["tenant_id", "seller_id", "ctx_key", "created_at"])


def downgrade():
    raise RuntimeError("Use a reviewed compatible backup; retain human review history")
