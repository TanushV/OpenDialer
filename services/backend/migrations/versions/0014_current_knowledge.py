"""Index approved business knowledge without rewriting its encrypted contents."""

import sqlalchemy as sa
from alembic import op

revision = "0014_current_knowledge"
down_revision = "0013_call_recovery_due"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("playbooks")}
    for name, kind in (("ctx_key", sa.String(100)), ("ctx_index_version", sa.Integer())):
        if name not in existing:
            op.add_column("playbooks", sa.Column(name, kind, nullable=True))
    if "ix_playbooks_current" not in {item["name"] for item in inspector.get_indexes("playbooks")}:
        op.create_index(
            "ix_playbooks_current",
            "playbooks",
            ["tenant_id", "seller_id", "ctx_key", "version", "created_at", "id"],
        )


def downgrade():
    raise RuntimeError("Use a reviewed compatible backup; retain original knowledge versions")
