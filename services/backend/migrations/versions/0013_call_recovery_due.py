"""Select due orphan obligations without scanning active or historical calls."""

import sqlalchemy as sa
from alembic import op

revision = "0013_call_recovery_due"
down_revision = "0012_control_deadlines"
branch_labels = None
depends_on = None


def upgrade():
    names = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("call_attempts")}
    if "ix_call_attempts_recovery_due" not in names:
        op.create_index(
            "ix_call_attempts_recovery_due", "call_attempts", ["tenant_id", "seller_id", "op_due_at"]
        )


def downgrade():
    raise RuntimeError("Use a reviewed compatible backup; retain active carrier obligations")
