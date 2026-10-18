"""Select recoverable effects by their actual kind before decrypting payloads."""

import sqlalchemy as sa
from alembic import op

revision = "0007_command_kinds"
down_revision = "0006_report_projections"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "op_kind" not in {column["name"] for column in inspector.get_columns("command_outbox")}:
        op.add_column("command_outbox", sa.Column("op_kind", sa.String(40), nullable=True))
    if "ix_command_outbox_kind" not in {index["name"] for index in inspector.get_indexes("command_outbox")}:
        op.create_index(
            "ix_command_outbox_kind",
            "command_outbox",
            ["tenant_id", "seller_id", "op_kind", "op_status", "op_due_at"],
        )
    # The authority backfills encrypted rows in bounded authenticated batches.


def downgrade():
    raise RuntimeError("Use a reviewed compatible backup; no destructive command downgrade")
