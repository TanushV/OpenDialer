"""Serve the next-lead page from an index in its exact ORDER BY, so only the top K rows decrypt."""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0019_lead_queue_order"
down_revision = "0018_job_due_probe"
branch_labels = None
depends_on = None

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def upgrade():
    # The queue orders missing coverage deadlines first. Store that explicitly so both SQLite
    # and PostgreSQL (whose NULL ordering differs) read the same index order.
    leads = sa.table("leads", sa.column("op_coverage_at", sa.DateTime(timezone=True)))
    op.execute(leads.update().where(leads.c.op_coverage_at.is_(None)).values(op_coverage_at=EPOCH))
    names = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("leads")}
    if "ix_leads_queue" not in names:
        op.create_index(
            "ix_leads_queue",
            "leads",
            [
                "tenant_id",
                "seller_id",
                "op_campaign_id",
                "op_status",
                sa.text("op_priority DESC"),
                "op_coverage_at",
                "id",
                "op_due_at",
            ],
        )


def downgrade():
    raise RuntimeError("Use a compatible build; the queue-order index carries no source data")
