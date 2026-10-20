"""W1: an idle authority never takes the writer; real work still starts promptly."""

import asyncio
import collections
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.db import Job
from sqlalchemy import event, select
from test_control_completion import principal

LANES = ["webhook", "live_coach", "postcall", "maintenance", "research", "scripts", "ml"]


@contextmanager
def write_transactions(service, monkeypatch):
    """Count every authority-owned write transaction and every SQLite writer admission."""
    db, writes, begins = service.db, [], []
    original = db.transaction

    def counted(write=True, *, session=None):
        if write and session is None:
            writes.append(sys._getframe(1).f_code.co_qualname)
        return original(write, session=session)

    def admitted(connection, cursor, statement, *rest):
        if statement.lstrip().upper().startswith("BEGIN IMMEDIATE"):
            begins.append(statement)

    monkeypatch.setattr(db, "transaction", counted)
    event.listen(db.engine, "before_cursor_execute", admitted)
    try:
        yield writes, begins
    finally:
        event.remove(db.engine, "before_cursor_execute", admitted)
        monkeypatch.setattr(db, "transaction", original)


def defer_existing_jobs(service, clock):
    """Seeded work stays queued, but none of it is due during the measured window."""
    with service.db.transaction() as s:
        for job in s.scalars(select(Job).where(Job.status.in_(["queued", "running"]))):
            job.status, job.lease_owner, job.lease_expires_at = "queued", None, None
            job.available_at = clock() + timedelta(hours=1)


def quiet_schedulers(service, monkeypatch):
    # Sixty-second schedulers outside W1 create due work by design; they are not idle polling.
    monkeypatch.setattr(service, "_next_optimization", float("inf"), raising=False)
    monkeypatch.setattr(service, "_next_preparation", float("inf"), raising=False)


@pytest.mark.asyncio
async def test_idle_lanes_and_maintenance_open_no_write_transaction(workspace, app, clock, monkeypatch):
    service = app.state.service
    defer_existing_jobs(service, clock)
    quiet_schedulers(service, monkeypatch)
    with write_transactions(service, monkeypatch) as (writes, begins):
        for _ in range(20):
            await service.run_jobs()
            await service.maintenance_tick()
    assert writes == [], collections.Counter(writes)
    assert begins == []


def test_claim_probe_honors_due_filters_without_taking_the_writer(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    defer_existing_jobs(service, clock)
    jobs, tenant = service.jobs, p["tenant_id"]
    with service.db.transaction() as s:
        jobs.enqueue_in(
            s,
            "research",
            {"seller_id": p["seller_id"]},
            "future-work",
            tenant_id=tenant,
            available_at=clock() + timedelta(minutes=5),
        )
    training = jobs.enqueue("ml", {"seller_id": p["seller_id"]}, "operational-train:due", tenant_id=tenant)
    other = jobs.enqueue("scripts", {"seller_id": p["seller_id"]}, "other-prefix:due", tenant_id=tenant)
    running = jobs.enqueue("postcall", {"seller_id": p["seller_id"]}, "postcall-running", tenant_id=tenant)
    assert jobs.claim("postcall", "busy-owner", key_prefix="postcall-running")["id"] == running["id"]
    waiting = jobs.enqueue("postcall", {"seller_id": p["seller_id"]}, "postcall-waiting", tenant_id=tenant)
    with write_transactions(service, monkeypatch) as (writes, begins):
        assert jobs.claim("research", "probe") is None, "Future work is not due"
        assert jobs.claim("ml", "probe", execution_class="maintenance") is None, "Only training is due"
        assert jobs.claim("scripts", "probe", key_prefix="wanted:") is None
        assert jobs.claim("scripts", "probe", tenant_id="another-tenant") is None
        assert jobs.claim("webhook", "probe", capacity=8) is None, "Empty queue"
        assert jobs.claim("postcall", "probe", capacity=1) is None, "Full pool"
    assert writes == [] and begins == [], collections.Counter(writes)
    assert jobs.get(waiting["id"])["status"] == "queued"
    low = jobs.enqueue("scripts", {"seller_id": p["seller_id"]}, "wanted:low", tenant_id=tenant, priority=1)
    high = jobs.enqueue("scripts", {"seller_id": p["seller_id"]}, "wanted:high", tenant_id=tenant, priority=9)
    assert jobs.claim("scripts", "worker", key_prefix="wanted:")["id"] == high["id"]
    assert jobs.claim("scripts", "worker", key_prefix="wanted:")["id"] == low["id"]
    assert jobs.claim("scripts", "worker", tenant_id=tenant)["id"] == other["id"]
    assert jobs.claim("ml", "worker", execution_class="training")["id"] == training["id"]


def test_enqueue_of_existing_work_does_not_take_the_writer(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    first = service.jobs.enqueue(
        "ml", {"seller_id": p["seller_id"]}, "periodic-slot", tenant_id=p["tenant_id"]
    )
    with write_transactions(service, monkeypatch) as (writes, begins):
        again = service.jobs.enqueue(
            "ml", {"seller_id": p["seller_id"]}, "periodic-slot", tenant_id=p["tenant_id"]
        )
    assert again == first
    assert writes == [] and begins == []


def test_recovery_releases_only_lapsed_unstarted_claims(workspace, app, clock, monkeypatch):
    from app.db import ActiveClaim

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        for name, call_id, expires in (
            ("lapsed", None, clock() - timedelta(seconds=1)),
            ("current", None, clock() + timedelta(seconds=60)),
            ("started", "call-in-progress", clock() - timedelta(seconds=1)),
        ):
            s.add(
                ActiveClaim(
                    tenant_id=p["tenant_id"],
                    seller_id=p["seller_id"],
                    endpoint_id="endpoint-" + name,
                    lead_id="lead-" + name,
                    rep_id="rep-" + name,
                    number_id="number",
                    reservation_id="reservation-" + name,
                    call_id=call_id,
                    expires_at=expires,
                )
            )
    service._recovery_snapshot(p)
    with service.db.transaction(False) as s:
        remaining = {claim.reservation_id for claim in s.scalars(select(ActiveClaim))}
    assert remaining == {"reservation-current", "reservation-started"}
    with write_transactions(service, monkeypatch) as (writes, begins):
        service._recovery_snapshot(p)
    assert writes == [] and begins == [], "Nothing lapsed: the snapshot stays read-only"


def concurrent_claims(service, claimants, **kwargs):
    """Both claimants pass the read probe before either one takes the writer."""
    barrier = threading.Barrier(claimants)
    original = service.db.transaction

    @contextmanager
    def probed(write=True, *, session=None):
        with original(write, session=session) as s:
            yield s
        if not write and threading.current_thread().name.startswith("claimant"):
            try:
                barrier.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass

    service.db.transaction = probed
    try:
        with ThreadPoolExecutor(max_workers=claimants, thread_name_prefix="claimant") as pool:
            return list(
                pool.map(lambda i: service.jobs.claim(worker=f"claimant-{i}", **kwargs), range(claimants))
            )
    finally:
        del service.db.transaction


def test_concurrent_claimers_take_a_due_job_exactly_once(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    defer_existing_jobs(service, clock)
    job = service.jobs.enqueue("scripts", {"seller_id": p["seller_id"]}, "race:one", tenant_id=p["tenant_id"])
    results = concurrent_claims(service, 2, queue="scripts", key_prefix="race:")
    claimed = [r for r in results if r]
    assert [r["id"] for r in claimed] == [job["id"]]
    assert service.jobs.get(job["id"])["attempts"] == 1


def test_concurrent_claimers_respect_pool_capacity(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    defer_existing_jobs(service, clock)
    for i in range(3):
        service.jobs.enqueue("research", {"seller_id": p["seller_id"]}, f"pool:{i}", tenant_id=p["tenant_id"])
    results = concurrent_claims(
        service, 4, queue="research", capacity=2, shared_queues=("research", "scripts"), key_prefix="pool:"
    )
    assert len([r for r in results if r]) == 2
    with service.db.transaction(False) as s:
        running = list(s.scalars(select(Job).where(Job.status == "running", Job.key.startswith("pool:"))))
    assert len(running) == 2


@pytest.mark.asyncio
async def test_idle_lanes_wait_for_work_instead_of_polling():
    from app.runtime import AuthorityRuntime

    calls = collections.Counter()

    class Service:
        settings = SimpleNamespace(worker_enabled=True)

        async def maintenance_tick(self):
            calls["maintenance"] += 1

        async def run_jobs(self, *, queues):
            calls[queues[0]] += 1

    runtime = AuthorityRuntime(Service(), queues=LANES)
    await runtime.start()
    try:
        await asyncio.sleep(2.2)
    finally:
        await runtime.close()
    assert set(calls) == set(LANES), calls
    # Fallback probes only: ~1 s for live lanes and maintenance, ~2 s for background lanes.
    assert calls["webhook"] <= 4 and calls["live_coach"] <= 4, calls
    assert calls["maintenance"] <= 4, calls
    assert all(calls[lane] <= 2 for lane in ("postcall", "research", "scripts", "ml")), calls


@pytest.mark.asyncio
async def test_enqueued_live_job_starts_promptly_in_process(workspace, app, clock, monkeypatch):
    from app.runtime import AuthorityRuntime

    service, p = app.state.service, principal(workspace)
    defer_existing_jobs(service, clock)
    started = asyncio.Event()
    seen = {}

    async def coach_turn(principal, call_id):
        seen.setdefault("at", time.perf_counter())
        started.set()

    monkeypatch.setattr(service, "coach_turn", coach_turn)
    runtime = AuthorityRuntime(service, queues=["live_coach", "webhook", "ml", "maintenance"])
    quiet_schedulers(service, monkeypatch)
    await runtime.start()
    try:
        await asyncio.sleep(0.4)  # Every lane has finished its first pass and is waiting.
        latencies = []
        for turn in range(3):
            started.clear()
            seen.clear()
            began = time.perf_counter()
            await asyncio.to_thread(
                service.jobs.enqueue,
                "live_coach",
                {"call_id": f"wake-{turn}", "seller_id": p["seller_id"], "user_id": p["user_id"]},
                f"live-wake:{turn}",
                tenant_id=p["tenant_id"],
                priority=100,
            )
            await asyncio.wait_for(started.wait(), 3)
            latencies.append(seen["at"] - began)
            await asyncio.sleep(0.3)
    finally:
        await runtime.close()
    assert min(latencies) < 0.25, latencies
    assert sorted(latencies)[1] < 0.25, f"In-process wakeup, not a polling interval: {latencies}"


@pytest.mark.asyncio
async def test_debounced_live_job_starts_when_due_not_at_the_fallback_probe(
    workspace, app, clock, monkeypatch
):
    from app.runtime import AuthorityRuntime

    service, p = app.state.service, principal(workspace)
    # Wall time elapses for a debounce; no commit announces that the job became due.
    monkeypatch.setattr(type(clock), "__call__", lambda self: datetime.now(UTC))
    defer_existing_jobs(service, clock)
    quiet_schedulers(service, monkeypatch)
    started, seen = asyncio.Event(), {}

    async def coach_turn(principal, call_id):
        seen.setdefault("at", time.perf_counter())
        started.set()

    monkeypatch.setattr(service, "coach_turn", coach_turn)
    runtime = AuthorityRuntime(service, queues=["live_coach"])
    await runtime.start()
    try:
        await asyncio.sleep(0.3)
        latencies = []
        for turn in range(3):
            started.clear()
            seen.clear()

            def debounced(turn=turn):
                with service.db.transaction() as s:
                    service.jobs.enqueue_in(
                        s,
                        "live_coach",
                        {
                            "call_id": f"debounced-{turn}",
                            "seller_id": p["seller_id"],
                            "user_id": p["user_id"],
                        },
                        f"live-debounced:{turn}",
                        tenant_id=p["tenant_id"],
                        priority=100,
                        available_at=clock() + timedelta(milliseconds=400),
                    )
                return time.perf_counter() + 0.4

            due = await asyncio.to_thread(debounced)
            await asyncio.wait_for(started.wait(), 3)
            latencies.append(seen["at"] - due)
            await asyncio.sleep(0.2)
    finally:
        await runtime.close()
    assert sorted(latencies)[1] < 0.25, f"A known due time arms the lane timer: {latencies}"


@pytest.mark.asyncio
async def test_source_write_wakes_its_derived_projection_lane(workspace, app, clock, monkeypatch):
    """Projection units are added by a flush hook; their commit still wakes the lane."""
    from app.db import update
    from app.runtime import AuthorityRuntime

    service, p = app.state.service, principal(workspace)
    defer_existing_jobs(service, clock)
    quiet_schedulers(service, monkeypatch)
    claimed, original_claim = [], service.jobs.claim

    def counted(queue, worker, *args, **kwargs):
        job = original_claim(queue, worker, *args, **kwargs)
        if job and job["key"].startswith("projection:"):
            claimed.append(time.perf_counter())
        return job

    monkeypatch.setattr(service.jobs, "claim", counted)
    runtime = AuthorityRuntime(service, queues=["ml"])
    await runtime.start()
    try:
        await asyncio.sleep(0.4)
        latencies = []
        for _ in range(3):
            claimed.clear()

            def correct():
                with service.db.transaction() as s:
                    lead = service.scoped(s, "leads", p)[0]
                    update(lead, status="needs_review" if lead.data["status"] == "eligible" else "eligible")
                return time.perf_counter()

            began = await asyncio.to_thread(correct)
            for _ in range(300):
                if claimed:
                    break
                await asyncio.sleep(0.01)
            assert claimed, "The correction's projection work was never claimed"
            latencies.append(claimed[0] - began)
            await asyncio.sleep(0.5)
    finally:
        await runtime.close()
    assert sorted(latencies)[1] < 0.25, f"Derived work waits for a fallback probe: {latencies}"


def ml_refresh(service, p, key, kind="home_refresh"):
    return service.jobs.enqueue(
        "ml",
        {"kind": kind, "seller_id": p["seller_id"], "user_id": p["user_id"], "role": "admin"},
        key,
        tenant_id=p["tenant_id"],
    )


@pytest.mark.asyncio
async def test_persistently_superseded_refresh_backs_off_without_spinning(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    defer_existing_jobs(service, clock)
    claims, original_claim = [], service.jobs.claim

    def counted(queue, worker, *args, **kwargs):
        job = original_claim(queue, worker, *args, **kwargs)
        if job and job["key"] == "superseded-refresh":
            claims.append(clock())
        return job

    monkeypatch.setattr(service.jobs, "claim", counted)
    monkeypatch.setattr(service, "rebuild_home_metrics", lambda *a, **k: {"status": "superseded"})
    job = ml_refresh(service, p, "superseded-refresh")
    for _ in range(5):
        await service.run_jobs(p["tenant_id"], ["ml"])
    # One pass re-claims a superseded refresh at most once; two immediate retries, then backoff.
    assert len(claims) == 3, f"A superseded refresh was claimed {len(claims)} times without progress"
    saved = service.jobs.get(job["id"])
    assert saved["status"] == "queued" and saved["attempts"] == 0 and saved["error"] is None
    delays = []
    for _ in range(7):
        due = datetime.fromisoformat(service.jobs.get(job["id"])["available_at"])
        delays.append((due - clock()).total_seconds())
        clock.now = due
        await service.run_jobs(p["tenant_id"], ["ml"])
    assert delays == [5, 10, 20, 40, 60, 60, 60], "Bounded doubling; the maximum thereafter"
    assert len(claims) == 10
    assert service.jobs.get(job["id"])["attempts"] == 0, "Coalescing never consumes failure attempts"
    clock.now = datetime.fromisoformat(service.jobs.get(job["id"])["available_at"])
    monkeypatch.setattr(service, "rebuild_home_metrics", lambda *a, **k: {"status": "ready"})
    await service.run_jobs(p["tenant_id"], ["ml"])
    assert service.jobs.get(job["id"])["status"] == "completed"
    with service.db.transaction() as s:  # A producer requeues the same completed identity.
        s.get(Job, job["id"]).status = "queued"
    monkeypatch.setattr(service, "rebuild_home_metrics", lambda *a, **k: {"status": "superseded"})
    for _ in range(2):
        await service.run_jobs(p["tenant_id"], ["ml"])
    due = datetime.fromisoformat(service.jobs.get(job["id"])["available_at"])
    assert (due - clock()).total_seconds() == 5, "Completion resets the coalescing backoff"


@pytest.mark.asyncio
async def test_paging_refresh_that_made_progress_continues_without_backoff(
    workspace, app, clock, monkeypatch
):
    import app.optimization.projections as projections

    service, p = app.state.service, principal(workspace)
    defer_existing_jobs(service, clock)
    pages = []

    def cleanup_page(service, p, payload, *, lease=None):
        pages.append(lease)
        return {"status": "pending" if len(pages) < 3 else "ready", "processed": 500}

    monkeypatch.setattr(projections, "cleanup_page", cleanup_page)
    job = ml_refresh(service, p, "cleanup-pages", kind="metric_cleanup")
    await service.run_jobs(p["tenant_id"], ["ml"])
    assert len(pages) == 3, "Each processed page is progress; the next page is due immediately"
    assert service.jobs.get(job["id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_heartbeat_failure_in_one_job_does_not_cancel_its_siblings(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    defer_existing_jobs(service, clock)
    real_sleep, finished = asyncio.sleep, []

    async def fast_heartbeat_sleep(delay, *args, **kwargs):
        return await real_sleep(0.02 if delay == 20 else delay, *args, **kwargs)

    async def analyze_postcall(principal, call_id):
        # The failing job finishes first; its error must not cancel the slower sibling.
        await real_sleep(0.1 if call_id == "flaky" else 0.3)
        finished.append(call_id)

    jobs = [
        service.jobs.enqueue(
            "postcall",
            {"call_id": name, "seller_id": p["seller_id"], "user_id": p["user_id"]},
            "heartbeat-sibling:" + name,
            tenant_id=p["tenant_id"],
        )
        for name in ("flaky", "healthy")
    ]
    original_heartbeat = service.jobs.heartbeat

    def heartbeat(job_id, worker, seconds=60):
        if job_id == jobs[0]["id"]:
            raise TimeoutError("Synthetic heartbeat transport failure")
        return original_heartbeat(job_id, worker, seconds)

    monkeypatch.setattr(asyncio, "sleep", fast_heartbeat_sleep)
    monkeypatch.setattr(service.jobs, "heartbeat", heartbeat)
    monkeypatch.setattr(service, "analyze_postcall", analyze_postcall)
    result = await service.run_jobs(p["tenant_id"], ["postcall"])
    assert sorted(finished) == ["flaky", "healthy"], "A sibling's heartbeat failure cancelled this job"
    assert result["processed"] == 2
    assert [service.jobs.get(j["id"])["status"] for j in jobs] == ["completed", "completed"]


@pytest.mark.asyncio
async def test_maintenance_runs_each_seller_step_once_per_tick(workspace, app, monkeypatch):
    import app.compliance.screening as screening

    service, client = app.state.service, workspace["client"]
    for i in range(2):
        created = client.post(
            "/v1/admin/users",
            headers=workspace["headers"],
            json={
                "email": f"maintenance-rep-{i}@example.test",
                "password": f"rep-{i}-password-123",
                "role": "rep",
            },
        )
        assert created.status_code == 200, created.text
    monkeypatch.setattr(service, "_next_optimization", float("inf"), raising=False)
    monkeypatch.setattr(service, "_next_preparation", 0, raising=False)
    counts = collections.Counter()

    def counted(owner, name):
        original = getattr(owner, name)
        if asyncio.iscoroutinefunction(original):

            async def wrapper(*args, **kwargs):
                counts[name] += 1
                return await original(*args, **kwargs)

        else:

            def wrapper(*args, **kwargs):
                counts[name] += 1
                return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, wrapper)

    for name in (
        "recover",
        "dispatch_controls",
        "reconcile_inbound",
        "inbound_watchdog",
        "_recovery_snapshot",
        "_expired_agents",
        "prepare_ahead",
        "queue_unfinished_webhooks",
    ):
        counted(service, name)
    counted(service.jobs, "recover_expired")
    counted(screening, "schedule_refresh")
    result = await service.maintenance_tick()
    assert result["status"] == "passed", result
    assert dict(counts) == {
        "recover": 1,
        "dispatch_controls": 1,
        "reconcile_inbound": 1,
        "inbound_watchdog": 1,
        "_recovery_snapshot": 1,
        "_expired_agents": 1,
        "prepare_ahead": 1,
        "queue_unfinished_webhooks": 1,
        "recover_expired": 1,
        "schedule_refresh": 1,
    }
