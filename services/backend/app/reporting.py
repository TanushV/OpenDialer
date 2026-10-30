"""Saved report views: computed in the background, served from their last publication.

A write that changes a report input bumps the seller's report revision at commit. A saved view
is fresh while its source revision matches and its time-dependent values have not expired; a
stale read returns the last publication and requests one background refresh.
"""

import hashlib
from datetime import timedelta

from sqlalchemy import inspect, select

from app.db import Job, ReportRevision, emit, put, update
from app.jobs.queue import jobdoc
from app.service import dt, fail

# Writes to these tables change a report; field sets limit invalidation to report inputs
# (None means every field). Presence renewals and transcript churn never invalidate.
REPORT_FIELDS = {
    "campaigns": {
        "optimization",
        "optimization_epoch",
        "optimization_trial",
        "controlled_comparisons",
        "policy_id",
        "staffing",
        "dataset_summaries",
    },
    "policy_versions": None,
    "leads": {
        "campaign_id",
        "phone",
        "state",
        "state_source",
        "region",
        "list_name",
        "originally_eligible",
        "status",
        "outreach_hold",
        "goal_completed",
        "coverage_due_at",
        "attempt_count",
        "segment",
    },
    "call_attempts": {
        "lead_id",
        "campaign_id",
        "rep_id",
        "number_id",
        "started_at",
        "submitted_at",
        "provider_id",
        "ended_at",
        "rep_released_at",
        "detached",
        "state",
        "outcome",
        "outcome_history",
        "outcome_available_at",
        "contact_role",
        "feature_snapshot_id",
        "end_reason",
    },
    "conversion_events": None,
    "feature_snapshots": None,
    "usage_ledger": {"status", "reserved_usd", "cost_usd", "credited_usd", "billing_verified", "call_id"},
    "coaching_feedback": None,
    "coach_events": {"call_id", "rep_id", "action", "latency", "strategy_exposure"},
    "outcomes": {"kind", "call_id", "observed_guidance_id", "state", "review_status", "version"},
    "work_intervals": {"rep_id", "started_at", "ended_at"},
    "experiment_assignments": None,
    "compliance_decisions": None,
    "model_versions": None,
    "number_inventory": None,
    "suppressions": None,
    "followup_tasks": {"inbound", "number_id", "occurred_at", "status"},
}
REPORT_TTL_SECONDS = 600


def collect_report_changes(session):
    """before_flush: remember which sellers' report inputs this transaction changed."""
    scopes = session.info.setdefault("report_scopes", set())
    for row in session.new | session.dirty | session.deleted:
        table = getattr(row, "__tablename__", None)
        if table not in REPORT_FIELDS or not row.tenant_id or not row.seller_id:
            continue
        if (row.tenant_id, row.seller_id) in scopes:
            continue
        fields = REPORT_FIELDS[table]
        if fields is not None and row in session.dirty:
            history = inspect(row).attrs.data.history
            previous = history.deleted[0] if history.deleted else None
            if previous is not None and all(previous.get(k) == row.data.get(k) for k in fields):
                continue
        scopes.add((row.tenant_id, row.seller_id))


def publish_report_changes(session):
    """before_commit: bump each changed seller's revision, the last row a writer locks."""
    if session.in_nested_transaction():
        return
    session.flush()
    scopes = session.info.pop("report_scopes", None)
    if not scopes:
        return
    if session.bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy.dialects.postgresql import insert
    for tenant_id, seller_id in sorted(scopes):
        statement = insert(ReportRevision).values(tenant_id=tenant_id, seller_id=seller_id, generation=1)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[ReportRevision.tenant_id, ReportRevision.seller_id],
                set_={"generation": ReportRevision.generation + 1},
            )
        )


def home_key(epoch):
    """Workspace totals are one seller-wide view per campaign cohort, not per viewer."""
    return "home:" + hashlib.sha256(epoch.encode()).hexdigest()


def projection_status(saved, revision, fresh, job, now):
    return {
        "status": "ready" if fresh else "stale" if saved else "pending",
        "as_of": saved.get("as_of"),
        "definition_version": saved.get("definition_version"),
        "source_watermark": saved.get("source_revision"),
        "source_revision": saved.get("source_revision"),
        "current_revision": revision,
        "complete": True if saved else None,
        "failed_sources": 0 if saved else None,
        "refresh_lag_seconds": max(0, (now - dt(saved["as_of"])).total_seconds())
        if saved.get("as_of")
        else None,
        "job_id": job["id"] if job else None,
        "refresh_status": job["status"] if job else None,
        "refresh_error": job["error"] if job else None,
    }


class ReportProjections:
    def report_revision(self, s, p):
        """The seller watermark, read without a lock; only source commits write it."""
        query = select(ReportRevision.generation).where(
            ReportRevision.tenant_id == p["tenant_id"], ReportRevision.seller_id == p["seller_id"]
        )
        return s.scalar(query) or 0

    def report_scope(self, s, p, epoch=None):
        user = self.required(s, "users", p["user_id"], p)
        if user.data.get("disabled"):
            fail(403, "Account is disabled")
        p = {**p, "role": "rep" if "rep" in (p["role"], user.data["role"]) else "admin"}
        campaign = self.selected_campaign(s, p, epoch.split(":", 1)[0] if epoch else None)
        p = {**p, "campaign_id": campaign.id}
        current = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
        epoch = epoch or current
        if len(epoch) > 200:
            fail(422, "Invalid cohort identity")
        if epoch != current:
            from app.db import MODELS

            model = MODELS["experiment_assignments"]
            exists = s.scalar(
                select(model.id)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.external_key.startswith(epoch + ":", autoescape=True),
                )
                .limit(1)
            )
            if not exists:
                fail(404, "Controlled cohort not found")
        key = hashlib.sha256(f"{p['user_id']}:{p['role']}:{epoch}".encode()).hexdigest()
        return p, campaign, epoch, key

    def _saved_view(self, s, p, key):
        row = self._by_key(s, p, "report_projections", key)
        saved = dict(row.data) if row else {}
        job = s.scalar(select(Job).where(Job.tenant_id == p["tenant_id"], Job.key == "refresh:" + key))
        revision = self.report_revision(s, p)
        fresh = bool(
            saved
            and saved["source_revision"] == revision
            and (not saved.get("expires_at") or dt(saved["expires_at"]) > self.clock())
        )
        return saved, revision, fresh, jobdoc(job) if job else None

    def request_report(self, p, key, kind, epoch):
        """One stable refresh job per view; a stale read queues it unless it is already pending."""
        payload = {
            "kind": kind,
            "seller_id": p["seller_id"],
            "campaign_id": p.get("campaign_id"),
            "epoch": epoch,
            **({} if kind == "home_refresh" else {"user_id": p["user_id"], "role": p["role"]}),
        }
        with self.db.transaction() as s:
            job = s.scalar(select(Job).where(Job.tenant_id == p["tenant_id"], Job.key == "refresh:" + key))
            if not job:
                return self.jobs.enqueue_in(
                    s, "ml", payload, "refresh:" + key, tenant_id=p["tenant_id"], priority=10
                )
            if job.status not in {"queued", "running"}:
                job.payload, job.status, job.error, job.attempts = payload, "queued", None, 0
                job.lease_owner = job.lease_expires_at = None
                job.available_at = self.clock()
            return jobdoc(job)

    def _publish_view(self, p, key, report, revision, expires_at, event, lease, **extra):
        with self.db.transaction() as s:
            self.jobs.require_owned_in(s, lease)
            row = self._by_key(s, p, "report_projections", key)
            values = {
                "report": report,
                "source_revision": revision,
                "definition_version": extra.pop("definition_version"),
                "as_of": self.clock().isoformat(),
                "expires_at": expires_at,
                **extra,
            }
            row = (
                update(row, **values)
                if row
                else put(
                    s, "report_projections", p["tenant_id"], p["seller_id"], values, key=key, now=self.clock()
                )
            )
            emit(s, p["tenant_id"], event, row, self.clock())
            return row

    def cached_home_metrics(self, p):
        with self.db.transaction(False) as s:
            p, _, epoch, _ = self.report_scope(s, p)
            key = home_key(epoch)
            saved, revision, fresh, job = self._saved_view(s, p, key)
        if not fresh and not (job and job["status"] in {"queued", "running"}):
            job = self.request_report(p, key, "home_refresh", epoch)
        return {
            **saved.get("report", {}),
            "projection": projection_status(saved, revision, fresh, None if fresh else job, self.clock()),
        }

    def rebuild_home_metrics(self, p, *, lease=None):
        """Seller-wide totals; the reader's authority is checked when it is served."""
        from app.optimization.projections import compute, read_metrics

        with self.db.transaction(False) as s:
            campaign = self.selected_campaign(s, p)
            epoch = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
            revision = self.report_revision(s, p)
        totals = compute(self, {**p, "campaign_id": campaign.id}, details=False)
        report = read_metrics(self, {**p, "campaign_id": campaign.id}, totals)
        row = self._publish_view(
            p,
            home_key(epoch),
            report,
            revision,
            report["next_refresh_at"],
            "workspace.metrics_ready",
            lease,
            definition_version=report["definition_version"],
            epoch=epoch,
        )
        return {"status": "ready", "id": row.id}

    def cached_optimization_report(self, p, epoch=None):
        with self.db.transaction(False) as s:
            p, campaign, epoch, key = self.report_scope(s, p, epoch)
            saved, revision, fresh, job = self._saved_view(s, p, key)
            settings = self.optimization_config(s, p, campaign.id)
        if not fresh and not (job and job["status"] in {"queued", "running"}):
            job = self.request_report(p, key, "report_refresh", epoch)
        return {
            **saved.get("report", {}),
            "settings": settings,
            "projection": {
                **projection_status(saved, revision, fresh, None if fresh else job, self.clock()),
                "calculated_settings": saved.get("report", {}).get("settings"),
            },
        }

    def rebuild_optimization_report(self, p, epoch=None, *, lease=None, totals=None):
        with self.db.transaction(False) as s:
            p, _, epoch, key = self.report_scope(s, p, epoch)
            revision = self.report_revision(s, p)
        report = self.optimization_report(p, epoch=epoch, totals=totals)
        expiry = self.clock() + timedelta(seconds=REPORT_TTL_SECONDS)
        if report["metrics"].get("next_refresh_at"):
            expiry = min(expiry, dt(report["metrics"]["next_refresh_at"]))
        row = self._publish_view(
            p,
            key,
            report,
            revision,
            expiry.isoformat(),
            "optimization.updated",
            lease,
            definition_version=report["metrics"]["definition_version"],
            epoch=epoch,
            owner_id=p["user_id"],
            role=p["role"],
        )
        return {"status": "ready", "id": row.id, "source_revision": revision}

    def request_optimization_refresh(self, p):
        with self.db.transaction(False) as s:
            p, _, epoch, key = self.report_scope(s, p)
        job = self.request_report(p, "optimization:" + key, "optimization_refresh", epoch)
        return {"status": job["status"], "job_id": job["id"]}

    def refresh_optimization_job(self, p, *, lease=None):
        with self.db.transaction(False) as s:
            p, _, _, _ = self.report_scope(s, p)
            if p["role"] != "admin":
                fail(403, "Administrator access is required to refresh optimization")
        return self.refresh_optimization(p, lease=lease)
