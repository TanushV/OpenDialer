"""R2.10: optimization refreshes follow finalized calls, at most once a minute, and stay quiet when idle.

On 1.0.1 a refresh was queued every ten minutes whatever happened: 1,076 of 1,090 `ml` jobs, and the
`optimization.updated` events they emitted refreshed the call screen.
"""

from datetime import timedelta

import pytest
from app.platform.db import Event, Job
from conftest import start
from sqlalchemy import func, select


def refresh_jobs(service):
    with service.db.transaction(False) as s:
        return [
            j
            for j in s.scalars(select(Job).where(Job.queue == "ml"))
            if j.payload.get("kind") == "optimization_refresh"
        ]


def updated_events(service):
    with service.db.transaction(False) as s:
        return s.scalar(select(func.count()).select_from(Event).where(Event.type == "optimization.updated"))


async def settle(service):
    service.schedule_optimization()
    for _ in range(3):
        await service.run_jobs(queues=["ml"])


def finalize_one(workspace, key):
    client, headers = workspace["client"], workspace["headers"]
    call = start(workspace, key=key)
    for event_type in ("call.answered", "call.hangup"):
        client.post(
            "/v1/simulation/event",
            headers=headers,
            json={
                "event_id": f"{key}-{event_type}",
                "event_type": event_type,
                "call_id": call["id"],
                "payload": {},
            },
        )
    from conftest import disposition_body

    body = disposition_body(client, headers, call["id"], outcome="not_interested")
    saved = client.post(f"/v1/calls/{call['id']}/disposition", headers=headers, json=body)
    assert saved.status_code == 200, saved.text


@pytest.mark.slow
async def test_an_idle_workspace_queues_no_refreshes(workspace, app, clock):
    service = app.state.service
    await settle(service)
    before = len(refresh_jobs(service))
    for _ in range(30):
        clock.now += timedelta(minutes=1)
        service.schedule_optimization()
    assert len(refresh_jobs(service)) == before


@pytest.mark.slow
async def test_a_finalized_call_queues_one_refresh_within_the_minute(workspace, app, clock):
    service = app.state.service
    await settle(service)
    before = len(refresh_jobs(service))
    finalize_one(workspace, "churn-call")
    # The next maintenance tick, a minute on, picks it up; a second tick in that minute adds nothing.
    clock.now += timedelta(minutes=1)
    service.schedule_optimization()
    service.schedule_optimization()
    assert len(refresh_jobs(service)) == before + 1
    for _ in range(3):
        await service.run_jobs(queues=["ml"])
    clock.now += timedelta(minutes=5)
    service.schedule_optimization()
    assert len(refresh_jobs(service)) == before + 1


@pytest.mark.slow
async def test_a_refresh_that_changes_nothing_emits_no_update(workspace, app, clock):
    service = app.state.service
    await settle(service)
    p = {
        "tenant_id": workspace["tenant_id"],
        "seller_id": workspace["seller_id"],
        "user_id": workspace["user_id"],
        "role": "owner",
    }
    service.refresh_optimization_job(p)  # The first refresh records the first learning snapshot.
    quiet = updated_events(service)
    service.refresh_optimization_job(p)
    assert updated_events(service) == quiet
