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
