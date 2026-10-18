"""Index current carrier obligations without changing human callback tasks."""

import sqlalchemy as sa
from alembic import op

revision = "0012_control_deadlines"
down_revision = "0011_model_serving"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    for table in ("call_attempts", "followup_tasks"):
        columns = {c["name"] for c in inspector.get_columns(table)}
        fields = ["op_control_due_at"] + (["op_reconcile_at"] if table == "followup_tasks" else [])
        for field in fields:
            if field not in columns:
                op.add_column(table, sa.Column(field, sa.DateTime(timezone=True), nullable=True))
        indexes = {i["name"] for i in inspector.get_indexes(table)}
        if f"ix_{table}_control_due" not in indexes:
            op.create_index(f"ix_{table}_control_due", table, ["tenant_id", "seller_id", "op_control_due_at"])
        if table == "followup_tasks" and "ix_followup_tasks_reconcile" not in indexes:
            op.create_index(
                "ix_followup_tasks_reconcile",
                table,
                ["tenant_id", "seller_id", "op_active", "op_reconcile_at"],
            )


def downgrade():
    raise RuntimeError("Use a reviewed compatible backup; retain active carrier obligations")
