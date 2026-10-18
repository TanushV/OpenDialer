"""Read saved reports cheaply; compute and publish them through the existing queue."""

import hashlib
from datetime import timedelta

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from app.db import MODELS, Job, ReportRevision, emit, put, update
from app.jobs.queue import jobdoc
from app.service import dt, fail

# Raw speech, ephemeral help, UI reads and the projections themselves are not inputs.
REPORT_INPUTS = {
    "campaigns",
    "policy_versions",
    "leads",
    "call_attempts",
    "conversion_events",
    "feature_snapshots",
    "notes",
    "usage_ledger",
    "coaching_feedback",
    "coach_events",
    "experiment_assignments",
    "compliance_decisions",
    "policy_decisions",
    "scheduling_plans",
    "learning_datasets",
    "model_versions",
    "number_inventory",
    "number_health",
    "number_leases",
    "followup_tasks",
    "suppressions",
    "lead_number_bindings",
    "work_intervals",
    "command_outbox",
    "active_claims",
}
REPORT_TTL_SECONDS = 600


def invalidate_reports(session):
    scopes = {
        (row.tenant_id, row.seller_id)
        for row in session.new | session.dirty | session.deleted
        if getattr(row, "__tablename__", None) in REPORT_INPUTS
        and (row in session.new or row in session.deleted or session.is_modified(row))
        and row.tenant_id
        and row.seller_id
    }
    for row in session.new | session.dirty | session.deleted:
        if getattr(row, "__tablename__", None) != "users":
            continue
        history = inspect(row).attrs.data.history
        previous = history.deleted[0] if history.deleted else row.data
        if (
            row in session.new
            or row in session.deleted
            or any(previous.get(field) != row.data.get(field) for field in ("disabled", "role"))
        ):
            scopes.add((row.tenant_id, row.seller_id))
    if not scopes:
        return
    transaction = session.get_nested_transaction() or session.get_transaction()
    if session.info.get("report_revision_transaction") is not transaction:
        session.info["report_revision_transaction"] = transaction
        session.info["report_revision_scopes"] = set()
    seen = session.info["report_revision_scopes"]
    if session.bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy.dialects.postgresql import insert
    for tenant_id, seller_id in sorted(scopes - seen):
        statement = insert(ReportRevision).values(tenant_id=tenant_id, seller_id=seller_id, generation=1)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[ReportRevision.tenant_id, ReportRevision.seller_id],
                set_={"generation": ReportRevision.generation + 1},
            )
        )
        seen.add((tenant_id, seller_id))


class ReportProjections:
    def report_revision(self, s, p, *, lock=False):
        query = select(ReportRevision.generation).where(
            ReportRevision.tenant_id == p["tenant_id"],
            ReportRevision.seller_id == p["seller_id"],
        )
        return s.scalar(query.with_for_update() if lock and not self.db.sqlite else query) or 0

    def report_scope(self, s, p, epoch=None):
        user = self.required(s, "users", p["user_id"], p)
        if user.data.get("disabled"):
            fail(403, "Account is disabled")
        p = {**p, "role": "rep" if "rep" in (p["role"], user.data["role"]) else "admin"}
        campaign = self.scoped(s, "campaigns", p)[0]
        current = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
        epoch = epoch or current
        if len(epoch) > 200:
            fail(422, "Invalid cohort identity")
        if epoch != current:
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

    def enqueue_report(self, p, key, revision, epoch, *, full=False, home=False):
        slot = int(self.clock().timestamp()) // (60 if home else REPORT_TTL_SECONDS)
        prefix = f"{'home' if home else 'optimization' if full else 'report'}:{key}:"
        identity = f"{prefix}{revision}:{slot}"
        try:
            with self.db.transaction() as s:
                self.report_revision(s, p, lock=True)
                pending = s.scalar(
                    select(Job)
                    .where(
                        Job.tenant_id == p["tenant_id"],
                        Job.key.startswith(prefix, autoescape=True),
                        Job.status.in_(["queued", "running"]),
                    )
                    .limit(1)
                )
                if pending:
                    return jobdoc(pending)
                return self.jobs.enqueue_in(
                    s,
                    "ml",
                    {
                        "kind": "home_refresh"
                        if home
                        else "optimization_refresh"
                        if full
                        else "report_refresh",
                        "seller_id": p["seller_id"],
                        "user_id": p["user_id"],
                        "role": p["role"],
                        "epoch": epoch,
                    },
                    identity,
                    tenant_id=p["tenant_id"],
                    priority=10,
                )
        except IntegrityError:
            # Concurrent viewers share the original job; a failed enqueue is rolled back.
            with self.db.transaction(False) as s:
                job = s.scalar(select(Job).where(Job.tenant_id == p["tenant_id"], Job.key == identity))
                if not job:
                    raise
                return jobdoc(job)

    def request_optimization_refresh(self, p):
        with self.db.transaction(False) as s:
            p, _, epoch, key = self.report_scope(s, p)
            generation = self.report_revision(s, p)
        job = self.enqueue_report(p, key, generation, epoch, full=True)
        return {"status": job["status"], "job_id": job["id"]}

    def refresh_optimization_job(self, p):
        with self.db.transaction(False) as s:
            p, _, _, _ = self.report_scope(s, p)
            if p["role"] != "admin":
                fail(403, "Administrator access is required to refresh optimization")
        return self.refresh_optimization(p)

    def cached_optimization_report(self, p, epoch=None):
        with self.db.transaction(False) as s:
            p, campaign, epoch, key = self.report_scope(s, p, epoch)
            current = self.report_revision(s, p)
            row = self._by_key(s, p, "report_projections", key)
            saved = dict(row.data) if row else {}
            settings = self.optimization_config(s, p, campaign.id)
        fresh = bool(saved and saved["source_revision"] == current and dt(saved["expires_at"]) > self.clock())
        job = None if fresh else self.enqueue_report(p, key, current, epoch)
        return {
            **saved.get("report", {}),
            "settings": settings,
            "projection": {
                "status": "ready" if fresh else "stale" if saved else "pending",
                "as_of": saved.get("as_of"),
                "calculated_settings": saved.get("report", {}).get("settings"),
                "source_revision": saved.get("source_revision"),
                "current_revision": current,
                "job_id": job["id"] if job else None,
                "refresh_status": job["status"] if job else None,
                "refresh_error": job["error"] if job else None,
            },
        }

    def rebuild_optimization_report(self, p, epoch=None):
        with self.db.transaction(False) as s:
            p, _, epoch, key = self.report_scope(s, p, epoch)
            generation = self.report_revision(s, p)
        # Historical calculation runs outside the publication/write transaction.
        report = self.optimization_report(p, epoch=epoch)
        with self.db.transaction() as s:
            _, _, _, current_key = self.report_scope(s, p, epoch)
            if current_key != key:
                return {"status": "superseded"}
            if self.report_revision(s, p, lock=True) != generation:
                return {"status": "superseded"}
            row = self._by_key(s, p, "report_projections", key)
            value = {
                "report": report,
                "source_revision": generation,
                "as_of": report["as_of"],
                "expires_at": (self.clock() + timedelta(seconds=REPORT_TTL_SECONDS)).isoformat(),
                "epoch": epoch,
                "owner_id": p["user_id"],
                "role": p["role"],
            }
            row = (
                update(row, **value)
                if row
                else put(
                    s, "report_projections", p["tenant_id"], p["seller_id"], value, key=key, now=self.clock()
                )
            )
            emit(s, p["tenant_id"], "optimization.updated", row, self.clock())
            return {"status": "ready", "id": row.id, "source_revision": generation}

    def cached_home_metrics(self, p):
        with self.db.transaction(False) as s:
            p, _, epoch, identity = self.report_scope(s, p)
            key = "home:" + identity
            revision = self.report_revision(s, p)
            row = self._by_key(s, p, "report_projections", key)
            saved = dict(row.data) if row else {}
        fresh = bool(
            saved and saved["source_revision"] == revision and dt(saved["expires_at"]) > self.clock()
        )
        job = None if fresh else self.enqueue_report(p, key, revision, epoch, home=True)
        return {
            **saved.get("report", {}),
            "projection": {
                "status": "ready" if fresh else "stale" if saved else "pending",
                "as_of": saved.get("as_of"),
                "source_revision": saved.get("source_revision"),
                "current_revision": revision,
                "job_id": job["id"] if job else None,
                "refresh_status": job["status"] if job else None,
                "refresh_error": "Workspace totals need recovery" if job and job["error"] else None,
            },
        }

    def rebuild_home_metrics(self, p):
        with self.db.transaction(False) as s:
            p, _, epoch, identity = self.report_scope(s, p)
            revision = self.report_revision(s, p)
        report = self.metrics(p)
        with self.db.transaction() as s:
            _, _, _, current_identity = self.report_scope(s, p)
            if current_identity != identity or self.report_revision(s, p, lock=True) != revision:
                return {"status": "superseded"}
            key = "home:" + identity
            row = self._by_key(s, p, "report_projections", key)
            values = {
                "report": report,
                "source_revision": revision,
                "as_of": report["as_of"],
                "expires_at": (self.clock() + timedelta(seconds=60)).isoformat(),
                "epoch": epoch,
                "owner_id": p["user_id"],
                "role": p["role"],
            }
            row = (
                update(row, **values)
                if row
                else put(
                    s, "report_projections", p["tenant_id"], p["seller_id"], values, key=key, now=self.clock()
                )
            )
            emit(s, p["tenant_id"], "workspace.metrics_ready", row, self.clock())
            return {"status": "ready", "id": row.id}
