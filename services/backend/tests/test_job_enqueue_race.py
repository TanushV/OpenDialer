"""Two writers enqueueing the same new job identity both commit (PostgreSQL).

Follow-on work is enqueued inside the originating transaction by select-then-insert. When
two transactions add the same new key, the second insert hit the unique (tenant_id, key)
index and aborted its whole transaction: a rep's hangup or wrap-up answered 409."""

import threading
import time

import pytest
from app.db import Job
from app.optimization.projections import enqueue_units
from sqlalchemy import select


@pytest.mark.postgres
@pytest.mark.parametrize("path", ["enqueue_in", "enqueue_units"])
def test_two_writers_adding_one_new_job_key_both_commit_and_coalesce(workspace, app, path):
    service, tenant, seller = app.state.service, workspace["tenant_id"], workspace["seller_id"]
    if path == "enqueue_in":
        key = "probe:race-identity"

        def enqueue(s):
            # Newer input for the same identity: the second request carries a changed payload.
            n = 2 if inserted.is_set() else 1
            service.jobs.enqueue_in(
                s, "ml", {"kind": "probe", "input_fingerprint": str(n)}, key, tenant_id=tenant, latest=True
            )
    else:
        key = f"projection:{seller}:number:race-number"

        def enqueue(s):
            enqueue_units(s, {(tenant, seller, "number", "race-number")})

    inserted, release, errors = threading.Event(), threading.Event(), []

    def first():
        with service.db.transaction() as s:
            enqueue(s)
            s.flush()
            inserted.set()
            release.wait(10)

    def second():
        inserted.wait(10)
        try:
            with service.db.transaction() as s:
                enqueue(s)
        except Exception as exc:  # The defect: the whole second transaction aborted.
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    time.sleep(0.5)  # The second writer now waits on the first one's uncommitted row.
    release.set()
    for thread in threads:
        thread.join(20)
    assert errors == []
    with service.db.transaction(False) as s:
        rows = list(s.scalars(select(Job).where(Job.tenant_id == tenant, Job.key == key)))
    assert len(rows) == 1
    assert rows[0].payload["desired_generation"] == 2, "The second request coalesces into the first"
