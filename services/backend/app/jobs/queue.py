import hashlib
from datetime import datetime, timedelta

from sqlalchemy import func, select, text

from app.db import Job, uid


def aware(value):
    from datetime import UTC

    return value.replace(tzinfo=UTC) if value and not value.tzinfo else value


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

    def enqueue(self, queue, payload, key, *, tenant_id, priority=0, max_attempts=3):
        with self.db.transaction() as s:
            return self.enqueue_in(
                s, queue, payload, key, tenant_id=tenant_id, priority=priority, max_attempts=max_attempts
            )

    def enqueue_in(self, s, queue, payload, key, *, tenant_id, priority=0, max_attempts=3, available_at=None):
        """Commit follow-on work in the same transaction as the change that requires it."""
        previous = s.scalar(select(Job).where(Job.tenant_id == tenant_id, Job.key == key))
        if previous:
            return jobdoc(previous)
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
    ):
        with self.db.transaction() as s:
            # This fence covers only admission to one execution pool, never the
            # job's work or other writers. It also covers independent runners.
            pool = tuple(sorted(shared_queues or (queue,)))
            if capacity is not None:
                if capacity < 1 or queue not in pool:
                    raise ValueError("Invalid execution pool capacity")
                if not self.db.sqlite:
                    key = int.from_bytes(
                        hashlib.sha256(("jobs:capacity:" + ":".join(pool)).encode()).digest()[:8],
                        signed=True,
                    )
                    if not s.scalar(text("SELECT pg_try_advisory_xact_lock(:pool)"), {"pool": key}):
                        return None
                running = s.scalar(
                    select(func.count()).select_from(Job).where(Job.queue.in_(pool), Job.status == "running")
                )
                if running >= capacity:
                    return None
            query = (
                select(Job)
                .where(Job.queue == queue, Job.status == "queued", Job.available_at <= self.clock())
                .order_by(Job.priority.desc(), Job.available_at, Job.id)
                .limit(100 if queue == "live_coach" else 1)
            )
            if tenant_id:
                query = query.where(Job.tenant_id == tenant_id)
            if key_prefix is not None:
                query = query.where(Job.key.startswith(key_prefix, autoescape=True))
            if not self.db.sqlite:
                query = query.with_for_update(skip_locked=True)
            candidates = list(s.scalars(query))
            if queue == "live_coach":
                candidates = self._coalesce_queued(s, candidates)
            job = next(iter(candidates), None)
            if not job:
                return None
            job.status, job.lease_owner = "running", worker
            if "desired_generation" in job.payload:
                job.payload = {**job.payload, "claimed_generation": job.payload["desired_generation"]}
            job.lease_expires_at = self.clock() + timedelta(seconds=lease_seconds)
            job.attempts += 1
            return jobdoc(job)

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

    def coalesce_refresh(self, job_id, worker):
        """A changed metrics source is newer work, not a failed provider attempt."""
        self._finish(job_id, worker, coalesced=True)

    def _finish(self, job_id, worker, error=None, retry_seconds=0, *, coalesced=False):
        with self.db.transaction() as s:
            job = s.get(Job, job_id)
            if (
                not job
                or job.status != "running"
                or job.lease_owner != worker
                or aware(job.lease_expires_at) < self.clock()
            ):
                raise ValueError("Job lease is not owned by this worker")
            if coalesced:
                if job.queue != "ml" or job.payload.get("kind") not in {
                    "home_refresh",
                    "forecast_refresh",
                    "metric_refresh",
                    "metric_dependents",
                    "metric_rebuild",
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
                return
            job.error = error[:500] if error else None
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

    def heartbeat(self, job_id, worker, seconds=60):
        with self.db.transaction() as s:
            job = s.get(Job, job_id)
            if (
                not job
                or job.lease_owner != worker
                or job.status != "running"
                or aware(job.lease_expires_at) < self.clock()
            ):
                raise ValueError("Lost job lease")
            job.lease_expires_at = self.clock() + timedelta(seconds=seconds)

    def recover_expired(self):
        with self.db.transaction() as s:
            jobs = s.scalars(select(Job).where(Job.status == "running", Job.lease_expires_at < self.clock()))
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
