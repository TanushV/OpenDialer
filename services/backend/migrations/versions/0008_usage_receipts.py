"""Index receipt identities without decrypting a seller's full billing history."""

import sqlalchemy as sa
from alembic import op

revision = "0008_usage_receipts"
down_revision = "0007_command_kinds"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "ix_usage_ledger_receipt" not in {index["name"] for index in inspector.get_indexes("usage_ledger")}:
        op.create_index(
            "ix_usage_ledger_receipt", "usage_ledger", ["tenant_id", "seller_id", "op_provider_id"]
        )
    # The authority authenticates and backfills legacy receipt hashes in bounded batches.
    # Existing duplicate receipts remain visible for review rather than deleted in a migration.


def downgrade():
    raise RuntimeError("Use a reviewed compatible backup; no destructive billing downgrade")
