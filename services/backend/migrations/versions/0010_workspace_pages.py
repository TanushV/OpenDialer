"""Scoped keyset history and non-content job ownership; encrypted records are retained."""

import sqlalchemy as sa
from alembic import op

revision = "0010_workspace_pages"
down_revision = "0009_screening_status"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = (
        "leads",
        "call_attempts",
        "followup_tasks",
        "number_inventory",
        "registry_snapshots",
        "model_versions",
        "playbooks",
        "trackers",
        "users",
    )
    for table in tables:
        existing = {i["name"] for i in inspector.get_indexes(table)}
        name = f"ix_{table}_page"
        if name not in existing:
            op.create_index(name, table, ["tenant_id", "seller_id", "created_at", "id"])
        if table == "call_attempts" and "ix_call_attempts_owner_page" not in existing:
            op.create_index(
                "ix_call_attempts_owner_page",
                table,
                ["tenant_id", "seller_id", "op_owner_id", "created_at", "id"],
            )
    columns = {c["name"] for c in inspector.get_columns("jobs")}
    for column in ("seller_id", "owner_id"):
        if column not in columns:
            op.add_column("jobs", sa.Column(column, sa.String(36), nullable=True))
    existing = {i["name"] for i in inspector.get_indexes("jobs")}
    for name, columns in (
        ("ix_jobs_scope_page", ["tenant_id", "seller_id", "id"]),
        ("ix_jobs_owner_page", ["tenant_id", "seller_id", "owner_id", "id"]),
    ):
        if name not in existing:
            op.create_index(name, "jobs", columns)


def downgrade():
    raise RuntimeError("Use a reviewed compatible backup; preserve workspace and job identities")
