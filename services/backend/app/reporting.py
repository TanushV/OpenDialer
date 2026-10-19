"""Read saved reports cheaply; compute and publish them through the existing queue."""

import hashlib
from datetime import timedelta

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from app.db import MODELS, Job, ReportRevision, emit, put, update
from app.jobs.queue import jobdoc
from app.service import dt, fail

# Only changes affecting a displayed metric, plan or authority invalidate it.
# None means the entire small configuration/receipt is an input, not every table.
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
        "endpoint_id",
        "originally_eligible",
        "status",
        "outreach_hold",
        "goal_completed",
        "coverage_due_at",
        "attempt_count",
        "next_attempt_at",
        "callback_due_at",
        "requested_contact",
        "timezone",
        "region",
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
        "max_call_seconds",
        "outcome",
        "outcome_history",
        "outcome_available_at",
        "outcome_source",
        "contact_role",
        "feature_snapshot_id",
        "submission_decision_id",
        "answered_at",
        "bridged_at",
        "connected_seconds",
        "connected_leg_kind",
        "end_reason",
        "hard_provider_error",
    },
    "conversion_events": None,
    "feature_snapshots": None,
    "notes": {"observations", "human_notes", "corrected_at"},
    "usage_ledger": {
        "kind",
        "at",
        "call_id",
        "object_id",
        "status",
        "reserved_usd",
        "cost_usd",
        "credited_usd",
        "billing_verified",
        "reconciled_to_provider_records",
    },
    "coaching_feedback": None,
    "coach_events": {"call_id", "rep_id", "action", "latency", "strategy_exposure"},
    "outcomes": {
        "kind",
        "call_id",
        "rep_work_intervals",
        "rep_work_incomplete",
        "observed_guidance_id",
        "state",
        "review_status",
        "epistemic_status",
        "review_receipt_id",
        "invalidated_at",
        "version",
    },
    "work_intervals": {"rep_id", "started_at", "ended_at", "lease_until"},
    "experiment_assignments": None,
    "compliance_decisions": None,
    "policy_decisions": None,
    "scheduling_plans": None,
    "learning_datasets": None,
    "model_versions": None,
    "number_inventory": None,
    "number_health": None,
    "number_leases": None,
    "followup_tasks": {
        "status",
        "confirmation_state",
        "due_at",
        "snoozed_until",
        "owner_id",
        "lead_id",
        "inbound",
        "number_id",
        "occurred_at",
    },
    "suppressions": None,
    "lead_number_bindings": None,
    "command_outbox": {"status", "kind"},
    "users": {"disabled", "role"},
}
REPORT_TTL_SECONDS = 600


def snapshot_fresh(saved, revision, pointer, now):
    return bool(
        saved
        and saved["source_revision"] == revision
        and dt(saved["expires_at"]) > now
        and not pointer.get("building_namespace")
        and saved.get("definition_version") == pointer.get("active_definition")
        and saved.get("namespace") == pointer.get("active_namespace")
    )


def snapshot_status(saved, revision, fresh, job, now):
    return {
        "status": "ready" if fresh else "stale" if saved else "pending",
        "as_of": saved.get("as_of"),
        "definition_version": saved.get("definition_version"),
        "source_watermark": saved.get("source_revision"),
        "source_revision": saved.get("source_revision"),
        "current_revision": revision,
        "refresh_lag_seconds": max(0, (now - dt(saved["as_of"])).total_seconds())
        if saved.get("as_of")
        else None,
        "job_id": job["id"] if job else None,
        "refresh_status": job["status"] if job else None,
        "refresh_error": job["error"] if job else None,
    }


def invalidate_reports(session):
    changed = []
    for row in session.new | session.dirty | session.deleted:
        table = getattr(row, "__tablename__", None)
        if table not in REPORT_FIELDS or not row.tenant_id or not row.seller_id:
            continue
        history = inspect(row).attrs.data.history
        if row not in session.new and row not in session.deleted and not history.has_changes():
            continue
        previous = history.deleted[0] if history.deleted else None
        if previous is None and row not in session.new:
            # MutableDict in-place edits do not retain their former value in
            # attribute history. Read this one source, not its surrounding history.
            model = type(row)
            previous = session.scalar(select(model.data).where(model.id == row.id))
        if table == "outcomes" and not any(
            value.get("kind") == "attempt_work"
            or value.get("kind") == "conversation_lineage"
            and (value.get("observation_kind") == "approach" or value.get("observed_guidance_id"))
            for value in (row.data, previous or {})
        ):
            continue
        fields = REPORT_FIELDS[table]
        if (
            row not in session.new
            and row not in session.deleted
            and previous is not None
            and fields is not None
            and all(previous.get(k) == row.data.get(k) for k in fields)
        ):
            continue
        changed.append((row, previous))
    scopes = {(row.tenant_id, row.seller_id) for row, _ in changed}
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

    from app.optimization.projections import dirty_source_units, enqueue_units

    enqueue_units(session, dirty_source_units(changed, session))


class ReportProjections:
    def prepare_metric_read(self, p):
        from app.optimization.projections import (
            _group_key,
            _rebuild_catchup,
            _rebuild_control,
            definition_version,
            refresh_due,
        )

        refresh_due(self, p)
        _rebuild_catchup(self, p)
        with self.db.transaction(False) as session:
            campaign = self.selected_campaign(session, p)
            config = self.optimization_config(session, p, campaign.id)
            definition = definition_version(
                config, campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
            )
            pointer = campaign.data.get("metric_projection", {})
            group = {
                "bucket_kind": "total",
                "bucket_day": None,
                "campaign_id": campaign.id,
                "definition_version": definition,
                "namespace": pointer.get("active_namespace", "initial"),
            }
            missing = not self._by_key(session, p, "metric_buckets", _group_key(self, group))
            needs_rebuild = definition != pointer.get("active_definition") or missing
            pending = session.scalar(
                select(Job.status)
                .where(
                    Job.tenant_id == p["tenant_id"],
                    Job.key.startswith(f"projection:{p['seller_id']}:", autoescape=True),
                    Job.status.in_(["queued", "running", "dead"]),
                )
                .order_by(Job.status)
                .limit(1)
            )
            model = MODELS["metric_contributions"]
            prefix = f"v{pointer.get('active_definition')}:{pointer.get('active_namespace')}:"
            remaining_due = session.scalar(
                select(model.id)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.external_key >= prefix,
                    model.external_key < prefix + "~",
                    model.next_refresh_at <= self.clock(),
                )
                .limit(1)
            )
        if pending == "dead":
            raise RuntimeError("Metric source requires recovery before current totals can publish")
        if needs_rebuild:
            _rebuild_control(self, p, start=True)
        return not (needs_rebuild or pending or remaining_due or pointer.get("building_namespace"))

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
        campaign = self.selected_campaign(s, p, epoch.split(":", 1)[0] if epoch else None)
        p = {**p, "campaign_id": campaign.id}
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
                    # Source deltas (30) and rebuild (20) precede this small view;
                    # unrelated retry planning (15) must not strand first-run totals.
                    priority=18 if home else 10,
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

    def refresh_optimization_job(self, p, *, lease=None):
        with self.db.transaction(False) as s:
            p, _, _, _ = self.report_scope(s, p)
            if p["role"] != "admin":
                fail(403, "Administrator access is required to refresh optimization")
        return self.refresh_optimization(p, lease=lease)

    def cached_optimization_report(self, p, epoch=None):
        with self.db.transaction(False) as s:
            p, campaign, epoch, key = self.report_scope(s, p, epoch)
            current = self.report_revision(s, p)
            row = self._by_key(s, p, "report_projections", key)
            saved = dict(row.data) if row else {}
            settings = self.optimization_config(s, p, campaign.id)
        fresh = snapshot_fresh(saved, current, campaign.data.get("metric_projection", {}), self.clock())
        job = None if fresh else self.enqueue_report(p, key, current, epoch)
        return {
            **saved.get("report", {}),
            "settings": settings,
            "projection": {
                **snapshot_status(saved, current, fresh, job, self.clock()),
                "calculated_settings": saved.get("report", {}).get("settings"),
            },
        }

    def rebuild_optimization_report(self, p, epoch=None, *, lease=None):
        if not self.prepare_metric_read(p):
            return {"status": "pending"}
        with self.db.transaction(False) as s:
            p, _, epoch, key = self.report_scope(s, p, epoch)
            generation = self.report_revision(s, p)
        # Historical calculation runs outside the publication/write transaction.
        report = self.optimization_report(p, epoch=epoch)
        with self.db.transaction() as s:
            self.report_revision(s, p, lock=True)
            self.jobs.require_owned_in(s, lease)
            _, _, _, current_key = self.report_scope(s, p, epoch)
            if current_key != key:
                return {"status": "superseded"}
            if self.report_revision(s, p, lock=True) != generation:
                return {"status": "superseded"}
            row = self._by_key(s, p, "report_projections", key)
            value = {
                "report": report,
                "source_revision": generation,
                "definition_version": report["metrics"]["definition_version"],
                "namespace": report["metrics"]["projection_namespace"],
                "as_of": report["as_of"],
                "expires_at": min(
                    self.clock() + timedelta(seconds=REPORT_TTL_SECONDS),
                    dt(report["metrics"]["next_refresh_at"])
                    if report["metrics"].get("next_refresh_at")
                    else self.clock() + timedelta(seconds=REPORT_TTL_SECONDS),
                ).isoformat(),
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
            p, campaign, epoch, identity = self.report_scope(s, p)
            key = "home:" + identity
            revision = self.report_revision(s, p)
            row = self._by_key(s, p, "report_projections", key)
            saved = dict(row.data) if row else {}
        fresh = snapshot_fresh(saved, revision, campaign.data.get("metric_projection", {}), self.clock())
        job = None if fresh else self.enqueue_report(p, key, revision, epoch, home=True)
        return {
            **saved.get("report", {}),
            "projection": {
                **snapshot_status(saved, revision, fresh, job, self.clock()),
                "refresh_error": "Workspace totals need recovery" if job and job["error"] else None,
            },
        }

    def rebuild_home_metrics(self, p, *, lease=None):
        if not self.prepare_metric_read(p):
            return {"status": "pending"}
        with self.db.transaction(False) as s:
            p, _, epoch, identity = self.report_scope(s, p)
            revision = self.report_revision(s, p)
        report = self.metrics(p)
        with self.db.transaction() as s:
            self.report_revision(s, p, lock=True)
            self.jobs.require_owned_in(s, lease)
            _, _, _, current_identity = self.report_scope(s, p)
            if current_identity != identity or self.report_revision(s, p, lock=True) != revision:
                return {"status": "superseded"}
            key = "home:" + identity
            row = self._by_key(s, p, "report_projections", key)
            values = {
                "report": report,
                "source_revision": revision,
                "definition_version": report["definition_version"],
                "namespace": report["projection_namespace"],
                "as_of": report["as_of"],
                "expires_at": min(
                    self.clock() + timedelta(seconds=60),
                    dt(report["next_refresh_at"])
                    if report.get("next_refresh_at")
                    else self.clock() + timedelta(seconds=60),
                ).isoformat(),
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
