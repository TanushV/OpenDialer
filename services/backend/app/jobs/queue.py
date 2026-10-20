import hashlib
from datetime import datetime, timedelta

from sqlalchemy import func, select, text

from app.db import Job, uid
from app.privacy import job_metadata


def aware(value):
    from datetime import UTC

    return value.replace(tzinfo=UTC) if value and not value.tzinfo else value


# Consecutive coalesces without progress: the first ones are a concurrent
# correction's fresh work and retry at once; later ones back off, and every one
# after the last step waits the maximum. Completion or a processed page resets.
COALESCE_IMMEDIATE = 2
COALESCE_BACKOFF_SECONDS = (5, 10, 20, 40, 60)


class LostJobLease(ValueError):
    """A former worker cannot publish, complete or fail the current owner's work."""


def jobdoc(j):
    return {
        "id": j.id,
        "tenant_id": j.tenant_id,
        "queue": j.queue,
        "key": j.key,
        "payload": j.payload,
        "status": j.status,
        "attempts": j.attempts,
        "error": j.error,
        "lease_owner": j.lease_owner,
        "available_at": aware(j.available_at).isoformat(),
    }


class JobQueue:
    def __init__(self, db, clock):
        self.db, self.clock = db, clock

    def require_owned_in(self, s, lease):
        if lease is None:
            return
        job = s.get(Job, lease[0], with_for_update=True)
        if (
            not job
            or job.status != "running"
            or job.lease_owner != lease[1]
            or not job.lease_expires_at
            or aware(job.lease_expires_at) <= self.clock()
        ):
            raise LostJobLease("Job lease is not owned by this worker")

    def enqueue(self, queue, payload, key, *, tenant_id, priority=0, max_attempts=3):
        # Existing work is returned unchanged by enqueue_in; periodic schedulers
        # re-announcing an identity need no writer.
        with self.db.transaction(False) as s:
            previous = s.scalar(select(Job).where(Job.tenant_id == tenant_id, Job.key == key))
            if previous:
                return jobdoc(previous)
        with self.db.transaction() as s:
            return self.enqueue_in(
                s, queue, payload, key, tenant_id=tenant_id, priority=priority, max_attempts=max_attempts
            )

    def enqueue_in(
        self,
        s,
        queue,
        payload,
        key,
        *,
        tenant_id,
        priority=0,
        max_attempts=3,
        available_at=None,
        latest=False,
    ):
        """Commit follow-on work in the same transaction as the change that requires it."""
        payload = job_metadata(payload)
        if latest and queue != "ml":
            raise ValueError("Only internal derived work may replace a queued payload")
        query = select(Job).where(Job.tenant_id == tenant_id, Job.key == key)
        previous = s.scalar(query.with_for_update() if latest else query)
        if previous:
            if latest:
                generation_keys = {"desired_generation", "claimed_generation", "coalesced"}
                old = {k: v for k, v in previous.payload.items() if k not in generation_keys}
                if old != payload:
                    previous.payload = {
                        **payload,
                        "desired_generation": previous.payload.get("desired_generation", 0) + 1,
                        "claimed_generation": previous.payload.get("claimed_generation", 0),
                    }
                    previous.available_at = available_at or self.clock()
                    if previous.status != "running":
                        previous.status, previous.attempts, previous.error = "queued", 0, None
                        previous.lease_owner = previous.lease_expires_at = None
                    s.flush()
            return jobdoc(previous)
        if latest:
            payload = {**payload, "desired_generation": 1, "claimed_generation": 0}
        job = Job(
            id=uid(),
            tenant_id=tenant_id,
            queue=queue,
            key=key,
            payload=payload,
            priority=priority,
            max_attempts=max_attempts,
            available_at=available_at or self.clock(),
        )
        s.add(job)
        s.flush()
        return jobdoc(job)

    @staticmethod
    def _execution_class(queue, execution_class):
        if execution_class not in {None, "training", "maintenance"} or (
            execution_class is not None and queue != "ml"
        ):
            raise ValueError("Invalid job execution class")
        if execution_class is None:
            return None
        # Existing training identities already distinguish CPU work without
        # decrypting payloads, changing durable keys or adding another queue.
        training = Job.key.startswith("operational-train:", autoescape=True)
        return training if execution_class == "training" else ~training

    def _due(self, query, queue, tenant_id, key_prefix, match):
        query = query.where(Job.queue == queue, Job.status == "queued", Job.available_at <= self.clock())
        if tenant_id:
            query = query.where(Job.tenant_id == tenant_id)
        if key_prefix is not None:
            query = query.where(Job.key.startswith(key_prefix, autoescape=True))
        return query.where(match) if match is not None else query

    def has_due(self, queue, tenant_id=None, *, execution_class=None):
        """One read-only probe before a runner launches a wave of claimers."""
        match = self._execution_class(queue, execution_class)
        with self.db.transaction(False) as s:
            return s.scalar(self._due(select(Job.id), queue, tenant_id, None, match).limit(1)) is not None

    def claim(
        self,
        queue,
        worker,
        lease_seconds=60,
        tenant_id=None,
        *,
        capacity=None,
        shared_queues=(),
        key_prefix=None,
        execution_class=None,
    ):
        claimed = self.claim_many(
            queue,
            worker,
            1,
            lease_seconds,
            tenant_id,
            capacity=capacity,
            shared_queues=shared_queues,
            key_prefix=key_prefix,
            execution_class=execution_class,
        )
        return claimed[0] if claimed else None

    def claim_many(
        self,
        queue,
        worker,
        limit,
        lease_seconds=60,
        tenant_id=None,
        *,
        capacity=None,
        shared_queues=(),
        key_prefix=None,
        execution_class=None,
    ):
        """Lease up to `limit` due jobs in one write transaction: one writer turn per wave."""
        match = self._execution_class(queue, execution_class)
        pool = tuple(sorted(shared_queues or (queue,)))
        if limit < 1 or (capacity is not None and (capacity < 1 or queue not in pool)):
            raise ValueError("Invalid execution pool capacity")

        def due(query):
            return self._due(query, queue, tenant_id, key_prefix, match)

        def running(s):
            query = select(func.count()).select_from(Job).where(Job.queue.in_(pool), Job.status == "running")
            return s.scalar(query.where(match) if match is not None else query)

        # Read-only admission probe (ix_jobs_due): an idle or saturated pool never
        # takes the writer. The write transaction below repeats both checks.
        with self.db.transaction(False) as s:
            if s.scalar(due(select(Job.id)).limit(1)) is None:
                return []
            if capacity is not None and running(s) >= capacity:
                return []
        with self.db.transaction() as s:
            # This fence covers only admission to one execution pool, never the
            # job's work or other writers. It also covers independent runners.
            if capacity is not None:
                if not self.db.sqlite:
                    key = int.from_bytes(
                        hashlib.sha256(
                            ("jobs:capacity:" + ":".join(pool) + ":" + (execution_class or "all")).encode()
                        ).digest()[:8],
                        signed=True,
                    )
                    # Serialize only the tiny admission transaction. Treating a
                    # simultaneous claim as an empty queue strands spare slots
                    # behind the first long-running job in a capacity wave.
                    s.execute(text("SELECT pg_advisory_xact_lock(:pool)"), {"pool": key})
                limit = min(limit, capacity - running(s))
                if limit < 1:
                    return []
            # The claim is conditional on the rows still being queued and due inside
            # this transaction: SQLite admits one writer (BEGIN IMMEDIATE), and
            # PostgreSQL re-checks the predicate under skipped-if-locked row locks.
            query = (
                due(select(Job))
                .order_by(Job.priority.desc(), Job.available_at, Job.id)
                .limit(max(100, limit) if queue == "live_coach" else limit)
            )
            if not self.db.sqlite:
                query = query.with_for_update(skip_locked=True)
            candidates = list(s.scalars(query))
            if queue == "live_coach":
                candidates = self._coalesce_queued(s, candidates)
            claimed = candidates[:limit]
            expires = self.clock() + timedelta(seconds=lease_seconds)
            for job in claimed:
                job.status, job.lease_owner = "running", worker
                if "desired_generation" in job.payload:
                    job.payload = {**job.payload, "claimed_generation": job.payload["desired_generation"]}
                job.lease_expires_at = expires
                job.attempts += 1
            return [jobdoc(job) for job in claimed]

    @staticmethod
    def _coalesce_queued(s, candidates):
        """Retire bounded legacy duplicates without charging execution attempts.

        Current live producers already keep one job per call. This handles old
        persisted keys; the selected job reads the latest current call context.
        A running owner is never superseded or leased a second time.
        """
        running = {
            (job.tenant_id, job.seller_id, job.payload.get("call_id"))
            for job in s.scalars(select(Job).where(Job.queue == "live_coach", Job.status == "running"))
        }
        selected = {}
        for job in candidates:
            call_id = job.payload.get("call_id")
            identity = (job.tenant_id, job.seller_id, call_id or job.id)
            original = selected.get(identity)
            if original:
                job.status = "completed"
                job.error = None
                job.lease_owner = job.lease_expires_at = None
                job.payload = {**job.payload, "superseded_by": original.id}
            else:
                selected[identity] = job
        return [job for identity, job in selected.items() if identity not in running]

    def coalesce_live_in(self, s, queue, payload, key, *, tenant_id, priority, available_at):
        """One latest context/packet; changed inputs never steal the running owner's lease."""
        payload = job_metadata(payload)
        if queue != "live_coach" and (queue, payload.get("kind")) not in {
            ("postcall", "conversation_review"),
            ("scripts", "packet_refresh"),
        }:
            raise ValueError("Only current conversation or packet work can be coalesced")
        job = s.scalar(select(Job).where(Job.tenant_id == tenant_id, Job.key == key).with_for_update())
        if not job:
            return self.enqueue_in(
                s,
                queue,
                {**payload, "desired_generation": 1},
                key,
                tenant_id=tenant_id,
                priority=priority,
                available_at=available_at,
            )
        if "pending_deadline" in payload and (
            job.status == "queued"
            or (
                job.status == "running"
                and job.payload.get("desired_generation", 0) > job.payload.get("claimed_generation", 0)
            )
        ):
            payload = {
                **payload,
                "pending_deadline": job.payload.get("pending_deadline", payload["pending_deadline"]),
            }
        if payload.get("debounce"):
            available_at = min(available_at, datetime.fromisoformat(payload["pending_deadline"]))
        job.payload = {
            **payload,
            "desired_generation": job.payload.get("desired_generation", 0) + 1,
            "claimed_generation": job.payload.get("claimed_generation", 0),
        }
        job.available_at = available_at
        if job.status != "running":
            job.status, job.error, job.attempts = "queued", None, 0
        s.flush()
        return jobdoc(job)

    def complete(self, job_id, worker):
        self._finish(job_id, worker)

    def fail(self, job_id, worker, error, retry_seconds=10):
        self._finish(job_id, worker, error, retry_seconds)

    def coalesce_refresh(self, job_id, worker, *, progressed=False):
        """A changed metrics source is newer work, not a failed provider attempt.

        A page that made progress is due again at once. A refresh that keeps
        being superseded or keeps waiting retries at once twice, then backs off
        (5 s doubling to 60 s) until it next completes, so it cannot spin.
        Neither path consumes an attempt. Returns True when newer input arrived
        during the run, so the requeued job is fresh work rather than a retry.
        """
        return self._finish(job_id, worker, coalesced=True, progressed=progressed)

    def _finish(self, job_id, worker, error=None, retry_seconds=0, *, coalesced=False, progressed=False):
        with self.db.transaction() as s:
            job = s.get(Job, job_id, with_for_update=True)
            if (
                not job
                or job.status != "running"
                or job.lease_owner != worker
                or not job.lease_expires_at
                or aware(job.lease_expires_at) <= self.clock()
            ):
                raise LostJobLease("Job lease is not owned by this worker")
            if coalesced:
                if job.queue != "ml" or job.payload.get("kind") not in {
                    "home_refresh",
                    "report_refresh",
                    "forecast_refresh",
                    "retry_plan_refresh",
                    "health_refresh",
                    "learning_refresh",
                    "optimization_refresh",
                    "metric_refresh",
                    "metric_dependents",
                    "metric_rebuild",
                    "metric_cleanup",
                }:
                    raise ValueError("Only read-only workspace refresh can be coalesced")
                job.attempts = max(0, job.attempts - 1)
            if job.payload.get("desired_generation", 0) > job.payload.get("claimed_generation", 0):
                # A newer context arrived while the original owner was running.
                # Its due time was set by the enqueueing transaction, not by completion.
                job.status, job.error = "queued", error[:500] if error else None
                if not error:
                    job.attempts = max(0, job.attempts - 1)
                elif job.attempts >= job.max_attempts:
                    job.status = "dead"
                job.lease_owner, job.lease_expires_at = None, None
                return True
            job.error = error[:500] if error else None
            streak = previous = job.payload.get("coalesced", 0)
            if coalesced:
                streak = (
                    0 if progressed else min(previous + 1, COALESCE_IMMEDIATE + len(COALESCE_BACKOFF_SECONDS))
                )
                waited = streak - COALESCE_IMMEDIATE
                retry_seconds = COALESCE_BACKOFF_SECONDS[waited - 1] if waited > 0 else 0
            elif not error:
                streak = 0
            if streak != previous:
                job.payload = {k: v for k, v in job.payload.items() if k != "coalesced"} | (
                    {"coalesced": streak} if streak else {}
                )
            job.status = (
                "queued"
                if coalesced
                else "completed"
                if not error
                else "dead"
                if job.attempts >= job.max_attempts
                else "queued"
            )
            job.available_at = self.clock() + timedelta(seconds=retry_seconds)
            job.lease_owner, job.lease_expires_at = None, None
            return False

    def heartbeat(self, job_id, worker, seconds=60):
        with self.db.transaction() as s:
            job = s.get(Job, job_id, with_for_update=True)
            if (
                not job
                or job.lease_owner != worker
                or job.status != "running"
                or not job.lease_expires_at
                or aware(job.lease_expires_at) <= self.clock()
            ):
                raise LostJobLease("Lost job lease")
            job.lease_expires_at = self.clock() + timedelta(seconds=seconds)

    def next_due(self, queue):
        """Earliest queued availability, so an in-process lane can sleep until it (read-only)."""
        with self.db.transaction(False) as s:
            return aware(
                s.scalar(select(func.min(Job.available_at)).where(Job.queue == queue, Job.status == "queued"))
            )

    def recover_expired(self):
        with self.db.transaction(False) as s:
            expired = s.scalar(
                select(Job.id).where(Job.status == "running", Job.lease_expires_at <= self.clock()).limit(1)
            )
        if expired is None:
            return
        with self.db.transaction() as s:
            jobs = s.scalars(
                select(Job)
                .where(Job.status == "running", Job.lease_expires_at <= self.clock())
                .with_for_update(skip_locked=True)
            )
            for job in jobs:
                job.status = (
                    "reconcile"
                    if job.queue == "call_control"
                    else ("dead" if job.attempts >= job.max_attempts else "queued")
                )
                job.lease_owner, job.lease_expires_at = None, None

    def get(self, job_id):
        with self.db.transaction(False) as s:
            return jobdoc(s.get(Job, job_id))
