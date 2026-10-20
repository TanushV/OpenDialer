"""Answer an idle job-admission probe from one index range, without the writer."""

import sqlalchemy as sa
from alembic import op

revision = "0018_job_due_probe"
down_revision = "0017_metric_projections"
branch_labels = None
depends_on = None


def upgrade():
    names = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("jobs")}
    if "ix_jobs_due" not in names:
        op.create_index("ix_jobs_due", "jobs", ["queue", "status", "available_at", "priority"])


def downgrade():
    raise RuntimeError("Use a compatible build; the due-work index carries no source data")
