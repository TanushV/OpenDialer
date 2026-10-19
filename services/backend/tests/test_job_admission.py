"""E09/C037: leased paid work must start, not sit behind a semaphore."""

import asyncio

import pytest
from app.db import Job
from sqlalchemy import select
from test_remediation_privacy import principal


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
