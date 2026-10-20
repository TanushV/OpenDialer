"""Read saved reports cheaply; compute and publish them through the existing queue.

A source write records which sellers changed while it flushes. At commit it
enqueues the dirty projection units (job rows in key order) and then bumps each
seller's report revision, so that seller-wide row is always the last lock a
writer takes. Derived work never locks it: it reads the revision as an as-of
watermark and publishes with it, and the next dirty refresh repairs staleness.
GETs only read saved evidence.
"""

import hashlib
from contextlib import nullcontext
from datetime import timedelta

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from app.db import MODELS, Job, ReportRevision, emit, put, update
from app.jobs.queue import jobdoc
from app.service import dt, fail

# Only changes affecting a displayed metric, plan or authority invalidate it.
# None means the entire small configuration/receipt is an input. Derived tables
# (plans, health, learning datasets) and presence lease renewals are not inputs:
# an open interval's contribution is due again when its lease can next matter.
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
    "work_intervals": {"rep_id", "started_at", "ended_at"},
    "experiment_assignments": None,
    "compliance_decisions": None,
    "policy_decisions": None,
    "model_versions": None,
    "number_inventory": None,
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
    # Only number purchases enter a plan; dial/control commands are not report inputs.
    "command_outbox": {"status", "kind", "numbers", "failed_numbers"},
    # Staffing and report scope follow access, not ordinary presence.
    "users": {"disabled", "role"},
}
REPORT_TTL_SECONDS = 600


def home_key(epoch):
    """Workspace totals are one seller-wide view per campaign cohort, not per viewer."""
    return "home:" + hashlib.sha256(epoch.encode()).hexdigest()


def snapshot_fresh(saved, revision, pointer, now):
    return bool(
        saved
        and saved["source_revision"] == revision
        and saved.get("complete", True)
        and (not saved.get("expires_at") or dt(saved["expires_at"]) > now)
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
        "complete": saved.get("complete", True) if saved else None,
        # Units whose job failed outright; their last totals stand, others still publish.
        "failed_sources": saved.get("failed_sources", 0) if saved else None,
        "refresh_lag_seconds": max(0, (now - dt(saved["as_of"])).total_seconds())
        if saved.get("as_of")
        else None,
        "job_id": job["id"] if job else None,
        "refresh_status": job["status"] if job else None,
        "refresh_error": job["error"] if job else None,
    }


def _changes(session):
    """Report inputs changed by this flush, with each row's former value."""
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
        values = (row.data, previous or {})
        if table == "outcomes" and not any(
            value.get("kind") == "attempt_work"
            or value.get("kind") == "conversation_lineage"
            and (value.get("observation_kind") == "approach" or value.get("observed_guidance_id"))
            for value in values
        ):
            continue
        if table == "command_outbox" and not any(value.get("kind") == "purchase" for value in values):
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
    return changed


def collect_report_changes(session):
    """before_flush: remember changed sellers and dirty units; take no lock yet."""
    changed = _changes(session)
    if not changed:
        return
    from app.optimization.projections import dirty_source_units

    transaction = session.get_nested_transaction() or session.get_transaction()
    pending = session.info.setdefault("report_changes", {}).setdefault(
        transaction, {"scopes": set(), "units": set()}
    )
    pending["scopes"].update((row.tenant_id, row.seller_id) for row, _ in changed)
    pending["units"].update(dirty_source_units(changed, session))


def discard_report_changes(session, transaction):
    """after_soft_rollback: a rolled-back (savepoint) write changed nothing."""
    pending = session.info.get("report_changes") or {}
    for key in list(pending):
        parent = key
        while parent is not None and parent is not transaction:
            parent = parent.parent
        if parent is transaction:
            del pending[key]


def publish_report_changes(session):
    """before_commit: dirty units in job-key order, then the revision row last."""
    if session.in_nested_transaction():
        return  # A savepoint release; its outer commit publishes.
    session.flush()
    pending = session.info.pop("report_changes", None) or {}
    scopes, units = set(), set()
    for change in pending.values():
        scopes |= change["scopes"]
        units |= change["units"]
    if units:
        from app.optimization.projections import enqueue_units

        enqueue_units(session, units)
        session.flush()
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


class ReportProjections:
    def _catch_up(self, p):
        """Bounded inline projection work: (totals complete, units processed, failed units).

        A projection page that changes active totals requests the other saved views;
        a view already running is not asked again."""
        from app.optimization.projections import (
            _group_key,
            _rebuild_catchup,
            _rebuild_control,
            definition_version,
            refresh_due,
        )

        processed = refresh_due(self, p)["processed"]
        processed += _rebuild_catchup(self, p)
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
            # A failed (dead) unit is surfaced as a per-unit error; it does not
            # hold every other source's totals hostage.
            units = Job.key.startswith(f"projection:{p['seller_id']}:", autoescape=True)
            pending = session.scalar(
                select(Job.id)
                .where(Job.tenant_id == p["tenant_id"], units, Job.status.in_(["queued", "running"]))
                .limit(1)
            )
            failed = len(
                list(
                    session.scalars(
                        select(Job.id)
                        .where(Job.tenant_id == p["tenant_id"], units, Job.status == "dead")
                        .limit(1001)
                    )
                )
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
        if needs_rebuild:
            _rebuild_control(self, p, start=True)
        complete = not (needs_rebuild or pending or remaining_due or pointer.get("building_namespace"))
        return complete, processed, failed

    def prepare_metric_read(self, p):
        return self._catch_up(p)[0]

    def report_revision(self, s, p):
        """The seller watermark, read without a lock; only source commits write it."""
        query = select(ReportRevision.generation).where(
            ReportRevision.tenant_id == p["tenant_id"],
            ReportRevision.seller_id == p["seller_id"],
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

    def report_job(self, s, p, key):
        job = s.scalar(select(Job).where(Job.tenant_id == p["tenant_id"], Job.key == "refresh:" + key))
        return jobdoc(job) if job else None

    def enqueue_report(self, p, key, epoch, *, full=False, home=False, session=None):
        """One stable job per saved view. A request requeues it, or, while it runs,
        asks for exactly one follow-up run. No revision lock."""
        from app.privacy import job_metadata

        payload = job_metadata(
            {
                "kind": "home_refresh" if home else "optimization_refresh" if full else "report_refresh",
                "seller_id": p["seller_id"],
                "campaign_id": p.get("campaign_id"),
                "epoch": epoch,
                **({} if home else {"user_id": p["user_id"], "role": p["role"]}),
            }
        )
        identity = ("optimization:" if full else "refresh:") + key
        try:
            with nullcontext(session) if session is not None else self.db.transaction() as s:
                job = s.scalar(
                    select(Job).where(Job.tenant_id == p["tenant_id"], Job.key == identity).with_for_update()
                )
                if not job:
                    # Source deltas (30) and rebuild (20) precede this small view.
                    return self.jobs.enqueue_in(
                        s,
                        "ml",
                        payload,
                        identity,
                        tenant_id=p["tenant_id"],
                        priority=18 if home else 10,
                        latest=True,
                    )
                job.payload = {
                    **payload,
                    "desired_generation": job.payload.get("desired_generation", 0) + 1,
                    "claimed_generation": job.payload.get("claimed_generation", 0),
                }
                if job.status != "running":
                    job.status, job.error, job.attempts = "queued", None, 0
                    job.lease_owner = job.lease_expires_at = None
                    job.available_at = self.clock()
                s.flush()
                return jobdoc(job)
        except IntegrityError:
            if session is not None:
                raise
            # Concurrent first requests share the original job.
            with self.db.transaction(False) as s:
                job = s.scalar(select(Job).where(Job.tenant_id == p["tenant_id"], Job.key == identity))
                if not job:
                    raise
                return jobdoc(job)

    def due_report_refreshes(self, s, p, *, saved_views=100):
        """Saved views whose watermark moved and whose refresh is not already queued."""
        campaign = self.selected_campaign(s, {k: v for k, v in p.items() if k != "campaign_id"})
        pointer = campaign.data.get("metric_projection", {})
        epoch = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
        revision = self.report_revision(s, p)
        model, users = MODELS["report_projections"], MODELS["users"]
        home = home_key(epoch)
        rows = {
            r.external_key: r
            for r in s.scalars(
                select(model)
                .where(model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"])
                .order_by(model.created_at.desc(), model.id)
                .limit(saved_views)
            )
        }
        if home not in rows and (row := self._by_key(s, p, "report_projections", home)):
            rows[home] = row
        owners = {r.data.get("owner_id") for r in rows.values()} - {None}
        active = {
            u.id
            for u in s.scalars(
                select(users).where(users.tenant_id == p["tenant_id"], users.id.in_(sorted(owners)))
            )
            if not u.data.get("disabled")
        }
        views = {home: {"campaign_id": campaign.id, "epoch": epoch, "home": True}}
        for key, row in rows.items():
            # Saved optimization views of current users; planning summaries and
            # former per-viewer home rows have their own producers or none.
            if row.data.get("owner_id") in active and row.data.get("epoch") and ":" not in key:
                views[key] = {
                    "user_id": row.data["owner_id"],
                    "role": row.data.get("role", "rep"),
                    "epoch": row.data["epoch"],
                    "campaign_id": row.data["epoch"].split(":", 1)[0],
                    "home": False,
                }
        from app.jobs.queue import aware

        # One pending refresh per view is the backpressure. A run scheduled for a
        # later contribution boundary is not pending: a source change pulls it forward.
        pending = {
            j.key
            for j in s.scalars(
                select(Job).where(
                    Job.tenant_id == p["tenant_id"], Job.key.in_(["refresh:" + k for k in views])
                )
            )
            if j.status == "running" or j.status == "queued" and aware(j.available_at) <= self.clock()
        }
        due = []
        for key, view in views.items():
            saved = dict(rows[key].data) if key in rows else {}
            if "refresh:" + key in pending:
                continue
            if saved and snapshot_fresh(saved, revision, pointer, self.clock()):
                continue
            due.append((key, view))
        return due

    def schedule_report_refreshes(self, p):
        """Dirty-chain and maintenance repair of saved views; a read-only probe unless one is due.

        Called after a projection page changed active totals, after a rebuild
        activates, and by the periodic sweep. It never reads the revision in a write."""
        with self.db.transaction(False) as s:
            due = self.due_report_refreshes(s, p)
        if not due:
            return []
        with self.db.transaction() as s:
            return [
                self.enqueue_report(
                    {**p, **{k: view.get(k) for k in ("user_id", "role", "campaign_id")}},
                    key,
                    view["epoch"],
                    home=view["home"],
                    session=s,
                )
                for key, view in due
            ]

    def request_optimization_refresh(self, p):
        with self.db.transaction(False) as s:
            p, _, epoch, key = self.report_scope(s, p)
        job = self.enqueue_report(p, key, epoch, full=True)
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
            job = self.report_job(s, p, key)
        fresh = snapshot_fresh(saved, current, campaign.data.get("metric_projection", {}), self.clock())
        if not saved and not job:
            # A view never calculated before has no background producer yet. Its
            # first request is the only write a read makes; staleness never writes.
            job = self.enqueue_report(p, key, epoch)
        return {
            **saved.get("report", {}),
            "settings": settings,
            "projection": {
                **snapshot_status(saved, current, fresh, None if fresh else job, self.clock()),
                "calculated_settings": saved.get("report", {}).get("settings"),
            },
        }

    def rebuild_optimization_report(self, p, epoch=None, *, lease=None):
        complete, processed, failed = self._catch_up(p)
        with self.db.transaction(False) as s:
            p, _, epoch, key = self.report_scope(s, p, epoch)
            watermark = self.report_revision(s, p)
        # Historical calculation runs outside the publication/write transaction.
        report = self.optimization_report(p, epoch=epoch)
        with self.db.transaction() as s:
            self.jobs.require_owned_in(s, lease)
            _, _, _, current_key = self.report_scope(s, p, epoch)
            if current_key != key:
                return {"status": "superseded"}
            row = self._by_key(s, p, "report_projections", key)
            value = {
                "report": report,
                "source_revision": watermark,
                "complete": complete,
                "failed_sources": failed,
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
        # Published either way; an incomplete page that made progress continues now.
        status = "pending" if processed and not complete else "ready"
        return {"status": status, "id": row.id, "source_revision": watermark, "processed": processed}

    def _refresh_at(self, s, p, identity, at):
        """Schedule a saved view's next run at its next contribution boundary.

        No poll: the queue's due time wakes the lane. A request already pending
        keeps its earlier time; a running owner's completion requeues it."""
        from app.jobs.queue import aware

        job = s.scalar(
            select(Job).where(Job.tenant_id == p["tenant_id"], Job.key == identity).with_for_update()
        )
        if not job:
            return
        at = max(at, self.clock())
        requested = job.payload.get("desired_generation", 0) > job.payload.get("claimed_generation", 0)
        if job.status == "queued" or job.status == "running" and requested:
            job.available_at = min(aware(job.available_at), at)
            return
        if job.status == "running":
            job.payload = {**job.payload, "desired_generation": job.payload.get("desired_generation", 0) + 1}
        else:
            job.status, job.error, job.attempts = "queued", None, 0
        job.available_at = at

    def cached_home_metrics(self, p):
        with self.db.transaction(False) as s:
            p, campaign, epoch, _ = self.report_scope(s, p)
            key = home_key(epoch)
            revision = self.report_revision(s, p)
            row = self._by_key(s, p, "report_projections", key)
            saved = dict(row.data) if row else {}
            job = self.report_job(s, p, key)
        fresh = snapshot_fresh(saved, revision, campaign.data.get("metric_projection", {}), self.clock())
        job = None if fresh else job
        return {
            **saved.get("report", {}),
            "projection": {
                **snapshot_status(saved, revision, fresh, job, self.clock()),
                "refresh_error": "Workspace totals need recovery" if job and job["error"] else None,
            },
        }

    def rebuild_home_metrics(self, p, *, lease=None):
        """Seller-wide totals; the reader's authority is checked when it is served."""
        complete, processed, failed = self._catch_up(p)
        with self.db.transaction(False) as s:
            campaign = self.selected_campaign(s, p)
            epoch = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
            watermark = self.report_revision(s, p)
        report = self.metrics({**p, "campaign_id": campaign.id})
        key = home_key(epoch)
        with self.db.transaction() as s:
            self.jobs.require_owned_in(s, lease)
            row = self._by_key(s, p, "report_projections", key)
            values = {
                "report": report,
                "source_revision": watermark,
                "complete": complete,
                "failed_sources": failed,
                "definition_version": report["definition_version"],
                "namespace": report["projection_namespace"],
                "as_of": report["as_of"],
                # Time-dependent totals expire at their next contribution boundary.
                "expires_at": report.get("next_refresh_at"),
                "epoch": epoch,
            }
            row = (
                update(row, **values)
                if row
                else put(
                    s, "report_projections", p["tenant_id"], p["seller_id"], values, key=key, now=self.clock()
                )
            )
            emit(s, p["tenant_id"], "workspace.metrics_ready", row, self.clock())
            if report.get("next_refresh_at"):
                self._refresh_at(s, p, "refresh:" + key, dt(report["next_refresh_at"]))
        status = "pending" if processed and not complete else "ready"
        return {"status": status, "id": row.id, "processed": processed}
