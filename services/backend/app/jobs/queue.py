from datetime import timedelta

from sqlalchemy import select

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
                available_at=self.clock(),
            )
            s.add(job)
            s.flush()
            return jobdoc(job)

    def claim(self, queue, worker, lease_seconds=60, tenant_id=None):
        with self.db.transaction() as s:
            query = (
                select(Job)
                .where(Job.queue == queue, Job.status == "queued", Job.available_at <= self.clock())
                .order_by(Job.priority.desc(), Job.available_at, Job.id)
                .limit(1)
            )
            if tenant_id:
                query = query.where(Job.tenant_id == tenant_id)
            if not self.db.sqlite:
                query = query.with_for_update(skip_locked=True)
            job = s.scalar(query)
            if not job:
                return None
            job.status, job.lease_owner = "running", worker
            job.lease_expires_at = self.clock() + timedelta(seconds=lease_seconds)
            job.attempts += 1
            return jobdoc(job)

    def complete(self, job_id, worker):
        self._finish(job_id, worker)

    def fail(self, job_id, worker, error, retry_seconds=10):
        self._finish(job_id, worker, error, retry_seconds)

    def _finish(self, job_id, worker, error=None, retry_seconds=0):
        with self.db.transaction() as s:
            job = s.get(Job, job_id)
            if (
                not job
                or job.status != "running"
                or job.lease_owner != worker
                or aware(job.lease_expires_at) < self.clock()
            ):
                raise ValueError("Job lease is not owned by this worker")
            job.error = error[:500] if error else None
            job.status = (
                "completed" if not error else ("dead" if job.attempts >= job.max_attempts else "queued")
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
