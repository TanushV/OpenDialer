"""Recorded real calls replayed through the production webhook path (R1.4 to R1.6).

Every recorded timeline, every scripted variant and many shuffled orders must reach a terminal
call state, leave at most one active call, never fail a webhook job, and never block calling.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

import pytest
from app.calling.state import TERMINAL
from app.platform.db import MODELS, Job, get
from app.providers import replay_carrier as replay
from app.providers.replay_carrier import ReplayCarrier
from conftest import ready
from sqlalchemy import select

TIMELINES = sorted((Path(__file__).parent / "fixtures" / "carrier_timelines").glob("call_*.json"))
VARIANTS = {
    "recorded": lambda events: events,
    "hangup_during_dial": replay.hangup_during_dial,
    "answer_after_hangup": replay.answer_after_hangup,
    "duplicated": replay.duplicated,
    "reversed": replay.reversed_order,
}


@pytest.fixture
def carrier():
    return ReplayCarrier()


async def play(app, workspace, timeline, variant):
    service = app.state.service
    service.managed.config = {"tenant_id": workspace["tenant_id"], "workspace_id": "test"}
    service.managed.last_exchange, service.managed.caught_up = monotonic(), True
    service.managed.numbers = {"+14155550100"}
    client, headers = workspace["client"], workspace["headers"]
    carrier = service.carrier(workspace["tenant_id"])
    # Real rep audio legs, as the Dialer opens them: one for this call, one per renewal recorded.
    agents = [
        client.post("/v1/telephony/agent", headers=headers).json()["agent_call_control_id"]
        for _ in range(1 + carrier.renewed_legs(timeline))
    ]
    ready(workspace, agent_call_control_id=agents[0])
    offered = client.post("/v1/queue/next", headers=headers, json={"campaign_id": workspace["campaign_id"]})
    assert offered.status_code == 200, offered.text
    started = client.post(
        "/v1/calls/start",
        headers=headers,
        json={"reservation_id": offered.json()["reservation_id"], "idempotency_key": "replay"},
    )
    assert started.status_code == 200, started.text
    call = started.json()
    with service.db.transaction(False) as s:
        row = get(s, "call_attempts", call["id"], workspace["tenant_id"])
        prospect = row.data["provider_id"]
    events = carrier.events(
        timeline,
        call_id=call["id"],
        tenant_id=workspace["tenant_id"],
        prospect_id=prospect,
        agent_ids=agents,
        started_at=datetime(2026, 10, 14, 18, tzinfo=UTC),
    )
    for event in variant(events):
        received = event["received_at"]
        body = {k: v for k, v in event.items() if k != "received_at"}
        await service.accept_webhook(
            workspace["tenant_id"],
            body,
            event["id"],
            verified=True,
            received_at=min(received, service.clock()),
        )
    for _ in range(3):
        await service.run_jobs(queues=["webhook"])
    with service.db.transaction(False) as s:
        row = get(s, "call_attempts", call["id"], workspace["tenant_id"])
        active = [
            c
            for c in s.scalars(select(MODELS["call_attempts"]))
            if c.data["rep_id"] == workspace["user_id"] and c.data["state"] not in TERMINAL
        ]
        webhook_jobs = list(s.scalars(select(Job).where(Job.queue == "webhook")))
    return row.data, active, webhook_jobs, service.managed.status()


def check(data, active, jobs, status, label):
    assert data["state"] in TERMINAL, (label, data["state"])
    assert len(active) <= 1, label
    assert jobs and all(j.status == "completed" for j in jobs), (label, [(j.status, j.error) for j in jobs])
    assert status["connected"], (label, status)


@pytest.mark.slow
@pytest.mark.database
@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("path", TIMELINES, ids=lambda p: p.stem)
async def test_recorded_calls_and_variants_end_cleanly(app, workspace, path, variant):
    timeline = json.loads(path.read_text())
    data, active, jobs, status = await play(app, workspace, timeline, VARIANTS[variant])
    check(data, active, jobs, status, f"{path.stem}/{variant}")
    if variant in {"recorded", "duplicated"}:
        assert data.get("provider_hangup_source") == timeline["attempt"]["provider_hangup_source"]


@pytest.mark.slow
@pytest.mark.database
@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize("path", TIMELINES, ids=lambda p: p.stem)
async def test_any_delivery_order_reaches_a_terminal_state(app, workspace, path, seed):
    """R1.6: shuffled orders always end, with at most one active call."""
    timeline = json.loads(path.read_text())
    data, active, jobs, status = await play(app, workspace, timeline, lambda e: replay.shuffled(e, seed))
    check(data, active, jobs, status, f"{path.stem}/seed {seed}")
