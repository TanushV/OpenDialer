"""Indexed context relationships; no raw speech or plaintext domain index.

Frozen DDL. The authority performs authenticated, bounded, resumable backfill.
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_context_reads"
down_revision = "0004_work_intervals"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    for table in (
        "research_facts",
        "research_sources",
        "script_versions",
        "consent_evidence",
        "transcript_segments",
    ):
        inspector = sa.inspect(connection)
        present = {c["name"] for c in inspector.get_columns(table)}
        fields = {"ctx_key": sa.String(100), "ctx_index_version": sa.Integer()}
        index_columns = ["tenant_id", "seller_id", "ctx_key"]
        if table == "research_facts":
            fields["ctx_status"] = sa.String(40)
            index_columns.append("ctx_status")
        if table in {"research_facts", "transcript_segments"}:
            fields["ctx_expires_at"] = sa.DateTime(timezone=True)
            index_columns.append("ctx_expires_at")
        else:
            index_columns.append("created_at")
        for name, datatype in fields.items():
            if name not in present:
                op.add_column(table, sa.Column(name, datatype, nullable=True))
        index = f"ix_{table}_context"
        if index not in {item["name"] for item in inspector.get_indexes(table)}:
            op.create_index(index, table, index_columns)


def downgrade():
    raise RuntimeError("Use a reviewed compatible backup; no destructive context downgrade")
