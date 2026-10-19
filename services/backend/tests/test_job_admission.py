"""E09/C037: leased paid work must start, not sit behind a semaphore."""

import asyncio
import threading

import pytest
from app.db import Job
from sqlalchemy import select
from test_remediation_privacy import principal


@pytest.mark.parametrize(
    "kind", ["health_refresh", "retry_plan_refresh", "learning_refresh", "optimization_refresh"]
)
def test_derived_refresh_source_changes_do_not_consume_failure_attempts(workspace, app, kind):
    service, p = app.state.service, principal(workspace)
    job = service.jobs.enqueue(
        "ml",
        {"kind": kind, "seller_id": p["seller_id"]},
        "derived-coalescing:" + kind,
        tenant_id=p["tenant_id"],
    )
    service.jobs.claim("ml", "current-owner", key_prefix=job["key"])
    service.jobs.coalesce_refresh(job["id"], "current-owner")
    saved = service.jobs.get(job["id"])
    assert saved["status"] == "queued" and saved["attempts"] == 0 and saved["error"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["forecast_refresh", "health_refresh", "learning_refresh", "report_refresh"])
async def test_expired_derived_worker_cannot_publish(workspace, app, clock, monkeypatch, kind):
    from datetime import timedelta

    import app.optimization.conversations as conversations
    import app.optimization.workflows as workflows
    from test_projection_replay import project_pending

    service, p = app.state.service, principal(workspace)
    project_pending(service, p)
    with service.db.transaction() as s:
        for job in s.scalars(select(Job)):
            job.status = "completed"
    if kind == "forecast_refresh":
        target, name = workflows, "capacity_forecast"
    elif kind == "learning_refresh":
        target, name = conversations, "conversation_associations"
    else:
        target, name = service, "inventory_evidence" if kind == "health_refresh" else "optimization_report"
    original = getattr(target, name)

    def expires(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.now += timedelta(seconds=61)
        return result

    monkeypatch.setattr(target, name, expires)
    service.jobs.enqueue(
        "ml",
        {
            "kind": kind,
            "seller_id": p["seller_id"],
            "user_id": p["user_id"],
            "campaign_id": workspace["campaign_id"],
            "role": "admin",
        },
        "expired-derived:" + kind,
        tenant_id=p["tenant_id"],
    )
    await service.run_jobs(p["tenant_id"], ["ml"])
    with service.db.transaction(False) as s:
        if kind == "learning_refresh":
            assert not service.required(s, "campaigns", workspace["campaign_id"], p).data.get(
                "conversation_report"
            )
        else:
            table = "number_health" if kind == "health_refresh" else "report_projections"
            assert service.scoped(s, table, p) == []
        if kind == "forecast_refresh":
            assert service.scoped(s, "scheduling_plans", p) == []


@pytest.mark.parametrize("action", ["complete", "heartbeat", "coalesce_refresh"])
def test_job_owner_expires_at_the_deadline(workspace, app, clock, action):
    from datetime import timedelta

    service, p = app.state.service, principal(workspace)
    job = service.jobs.enqueue(
        "ml",
        {"kind": "report_refresh", "seller_id": p["seller_id"]},
        "expiry-boundary:" + action,
        tenant_id=p["tenant_id"],
    )
    service.jobs.claim("ml", "boundary-owner", lease_seconds=30, key_prefix=job["key"])
    clock.now += timedelta(seconds=30)
    with pytest.raises(ValueError, match="[Ll]ease"):
        getattr(service.jobs, action)(job["id"], "boundary-owner")


def test_completion_cannot_lose_a_concurrent_new_generation(workspace, app, clock, monkeypatch):
    """Force the finish/enqueue race on real PostgreSQL, not a mock lock assertion."""
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    service, p = app.state.service, principal(workspace)
    if service.db.sqlite:
        pytest.skip("SQLite has one serialized writer; row-lock interleaving requires PostgreSQL")
    payload = {"kind": "packet_refresh", "seller_id": p["seller_id"], "lead_ids": [], "desired_generation": 1}
    job = service.jobs.enqueue("scripts", payload, "finish-generation-race", tenant_id=p["tenant_id"])
    service.jobs.claim("scripts", "finishing-owner", key_prefix=job["key"])
    loaded, release = threading.Event(), threading.Event()
    original = Session.get

    def held_get(session, entity, identity, **kwargs):
        result = original(session, entity, identity, **kwargs)
        if entity is Job and identity == job["id"] and threading.current_thread().name.startswith("finisher"):
            loaded.set()
            assert release.wait(5), "Test failed to release completing owner"
        return result

    monkeypatch.setattr(Session, "get", held_get)

    def next_generation():
        with service.db.transaction() as s:
            s.execute(text("SET LOCAL lock_timeout = '200ms'"))
            service.jobs.coalesce_live_in(
                s,
                "scripts",
                payload,
                job["key"],
                tenant_id=p["tenant_id"],
                priority=1,
                available_at=clock.now,
            )

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="finisher") as pool:
        future = pool.submit(service.jobs.complete, job["id"], "finishing-owner")
        blocked = False
        try:
            assert loaded.wait(5)
            try:
                next_generation()
            except OperationalError as error:
                assert "lock timeout" in str(error)
                blocked = True
        finally:
            release.set()
        future.result(timeout=5)
    if blocked:
        next_generation()
    current = service.jobs.get(job["id"])
    assert current["payload"]["desired_generation"] == 2
    assert current["status"] == "queued", "Finishing an older snapshot lost the new packet generation"
    assert current["lease_owner"] is None


@pytest.mark.asyncio
async def test_claim_only_running_capacity(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    entered, release, started = asyncio.Event(), asyncio.Event(), []

    async def held_prepare(p, lead_ids):
        started.extend(lead_ids)
        if len(started) == 2:
            entered.set()
        await release.wait()

    monkeypatch.setattr(service, "prepare", held_prepare)
    for i in range(24):
        service.jobs.enqueue(
            "scripts",
            {"seller_id": p["seller_id"], "user_id": p["user_id"], "lead_ids": [str(i)]},
            f"capacity-{i}",
            tenant_id=p["tenant_id"],
        )
    task = asyncio.create_task(service.run_jobs(p["tenant_id"], queues=["scripts"]))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.sleep(0.05)
        with service.db.transaction(False) as s:
            jobs = list(s.scalars(select(Job).where(Job.queue == "scripts")))
            running = [job for job in jobs if job.status == "running"]
            assert len(running) == len(started) == 2, (
                "Unstarted work must keep queued state and no execution lease"
            )
            assert all(job.lease_expires_at is None for job in jobs if job.status == "queued")
    finally:
        release.set()
        await asyncio.wait_for(task, 10)


@pytest.mark.asyncio
async def test_two_workers_share_job_identity_and_global_capacity(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    active, peak, seen = 0, 0, []

    async def prepare(p, lead_ids):
        nonlocal active, peak
        seen.extend(lead_ids)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.03)
        active -= 1

    monkeypatch.setattr(service, "prepare", prepare)
    for i in range(12):
        service.jobs.enqueue(
            "scripts",
            {"seller_id": p["seller_id"], "user_id": p["user_id"], "lead_ids": [str(i)]},
            f"two-workers-{i}",
            tenant_id=p["tenant_id"],
        )
    await asyncio.gather(*(service.run_jobs(p["tenant_id"], queues=["scripts"]) for _ in range(2)))
    assert sorted(seen) == sorted(str(i) for i in range(12))
    assert peak <= 2, (
        "Script generation capacity is authority-wide, not a fresh semaphore for every worker tick"
    )


@pytest.mark.asyncio
async def test_research_and_script_ticks_share_company_job_capacity(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    active, peak = 0, 0
    seen = []

    async def prepare(p, lead_ids):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        seen.extend(lead_ids)
        await asyncio.sleep(0.05)
        active -= 1

    monkeypatch.setattr(service, "prepare", prepare)
    for queue in ("research", "scripts"):
        for i in range(4):
            service.jobs.enqueue(
                queue,
                {
                    "seller_id": p["seller_id"],
                    "user_id": p["user_id"],
                    "lead_ids": [f"{queue}:{i}"],
                },
                f"lanes:{queue}:{i}",
                tenant_id=p["tenant_id"],
            )
    await asyncio.gather(*(service.run_jobs(p["tenant_id"], queues=[q]) for q in ("research", "scripts")))
    assert len(seen) == len(set(seen)) == 8
    assert peak <= service.settings.runtime_limits.company_units


@pytest.mark.asyncio
async def test_legacy_live_backlog_coalesces_without_leasing_waiting_requests(workspace, app, monkeypatch):
    from conftest import start

    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    seen = []

    async def coach(p, call_id):
        seen.append(call_id)

    monkeypatch.setattr(service, "coach_turn", coach)
    for i in range(18):
        service.jobs.enqueue(
            "live_coach",
            {
                "call_id": call["id"],
                "seller_id": p["seller_id"],
                "user_id": p["user_id"],
                "state_version": i,
            },
            f"legacy-admission:{i}",
            tenant_id=p["tenant_id"],
        )
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    assert seen == [call["id"]]
    with service.db.transaction(False) as s:
        jobs = list(s.scalars(select(Job).where(Job.queue == "live_coach")))
        assert all(j.status == "completed" for j in jobs)
        assert sum(j.attempts for j in jobs) == 1, (
            "Superseded unsent contexts are coalesced without execution leases"
        )


@pytest.mark.asyncio
async def test_separate_runners_share_database_admission(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    other = type(service)(service.db, service.settings, service.clock)
    active, peak, seen = 0, 0, []

    async def prepare(p, lead_ids):
        nonlocal active, peak
        seen.extend(lead_ids)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1

    for runner in (service, other):
        monkeypatch.setattr(runner, "prepare", prepare)
    for i in range(8):
        service.jobs.enqueue(
            "scripts",
            {"seller_id": p["seller_id"], "user_id": p["user_id"], "lead_ids": [str(i)]},
            f"separate-runners:{i}",
            tenant_id=p["tenant_id"],
        )
    await asyncio.gather(
        *(runner.run_jobs(p["tenant_id"], queues=["scripts"]) for runner in (service, other))
    )
    assert len(seen) == len(set(seen)) == 8
    assert peak <= service.settings.runtime_limits.company_units


def test_rebuild_claim_prefix_cannot_run_training_or_another_seller(app, clock):
    queue = app.state.service.jobs
    queue.enqueue(
        "ml",
        {"seller_id": "other", "rows": ["must not execute"]},
        "operational-train:fixture",
        tenant_id="tenant",
        priority=100,
    )
    queue.enqueue("ml", {"seller_id": "other"}, "projection:other:lead:1", tenant_id="tenant", priority=100)
    expected = queue.enqueue(
        "ml", {"seller_id": "seller"}, "projection:seller:lead:1", tenant_id="tenant", priority=1
    )
    claimed = queue.claim("ml", "rebuild-fixture", tenant_id="tenant", key_prefix="projection:seller:")
    assert claimed["id"] == expected["id"]
    assert queue.claim("ml", "rebuild-fixture", tenant_id="tenant", key_prefix="projection:seller:") is None


@pytest.mark.asyncio
async def test_training_and_projection_backlogs_make_independent_progress(workspace, app, monkeypatch):
    from app.optimization import projections

    service, p = app.state.service, principal(workspace)
    other = type(service)(service.db, service.settings, service.clock)
    training_started, projection_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_projection = threading.Event()
    loop = asyncio.get_running_loop()
    active, peak, seen = 0, 0, []

    async def train(p, payload):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        seen.append(payload["fixture"])
        training_started.set()
        await release.wait()
        active -= 1

    def project(*args, **kwargs):
        loop.call_soon_threadsafe(projection_started.set)
        assert release_projection.wait(20), "The test must release its owned projection work"
        return {"status": "ready"}

    for runner in (service, other):
        monkeypatch.setattr(runner, "train_model_job", train)
    monkeypatch.setattr(projections, "refresh_unit", project)
    for i in range(24):
        service.jobs.enqueue(
            "ml",
            {"kind": "metric_refresh", "seller_id": p["seller_id"], "user_id": p["user_id"]},
            f"projection:{p['seller_id']}:fixture:{i}",
            tenant_id=p["tenant_id"],
            priority=30,
        )
    for i in range(2):
        service.jobs.enqueue(
            "ml",
            {"fixture": i, "seller_id": p["seller_id"], "user_id": p["user_id"]},
            f"operational-train:independent-{i}",
            tenant_id=p["tenant_id"],
        )
    tasks = [asyncio.create_task(r.run_jobs(p["tenant_id"], queues=["ml"])) for r in (service, other)]
    try:
        await asyncio.wait_for(training_started.wait(), 10)
        await asyncio.wait_for(projection_started.wait(), 10)
        assert peak == 1, "CPU admission is shared across independent runners"
        assert len(seen) == 1, "A second fit remains queued while the first child runs"
    finally:
        release_projection.set()
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 20)
    assert sorted(seen) == [0, 1]
