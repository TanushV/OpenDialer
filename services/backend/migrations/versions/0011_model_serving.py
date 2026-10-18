"""Index current serving state; keep model contents and identities encrypted/intact."""

import sqlalchemy as sa
from alembic import op

revision = "0011_model_serving"
down_revision = "0010_workspace_pages"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("model_versions")}
    for name, kind in (
        ("ctx_key", sa.String(100)),
        ("ctx_status", sa.String(40)),
        ("ctx_expires_at", sa.DateTime(timezone=True)),
        ("ctx_index_version", sa.Integer()),
    ):
        if name not in existing:
            op.add_column("model_versions", sa.Column(name, kind, nullable=True))
    if "ix_model_versions_context" not in {
        index["name"] for index in inspector.get_indexes("model_versions")
    }:
        op.create_index(
            "ix_model_versions_context",
            "model_versions",
            ["tenant_id", "seller_id", "ctx_key", "ctx_status", "ctx_expires_at"],
        )


def downgrade():
    raise RuntimeError("Use a reviewed compatible backup; retain model identities and outcomes")
