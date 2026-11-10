"""Phase 0 regressions (docs/REBUILD_PLAN.md R0.2 to R0.4), shaped by the recorded real calls.

Each test fails on 1.0.1 and names the false block or failed job the owner saw.
"""

import json
from pathlib import Path
from time import monotonic

import pytest
from app import ops
from app.platform.db import MODELS, Job, get
from conftest import ready, reserve, start
from sqlalchemy import select

TIMELINES = Path(__file__).parent / "fixtures" / "carrier_timelines"


def connected(service, workspace):
    """A managed relay that is signed in, caught up and recently probed."""
    service.managed.config = {"tenant_id": workspace["tenant_id"], "workspace_id": "test"}
    service.managed.last_exchange = monotonic()
    service.managed.caught_up = True
    service.managed.numbers = {"+14155550100"}


def simulate(workspace, call_id, event_type, **payload):
    r = workspace["client"].post(
        "/v1/simulation/event",
        headers=workspace["headers"],
        json={
            "event_id": f"{call_id}-{event_type}",
            "event_type": event_type,
            "call_id": call_id,
            "payload": payload,
        },
    )
    assert r.status_code == 200, r.text


def status(workspace):
    r = workspace["client"].get("/v1/managed-connection", headers=workspace["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def can_dial_next(workspace):
    offered = reserve(workspace)
    assert offered.status_code == 200, offered.text
    r = workspace["client"].post(
        "/v1/calls/start",
        headers=workspace["headers"],
        json={"reservation_id": offered.json()["reservation_id"], "idempotency_key": "next-call"},
    )
    return r


def recorded(name):
    return json.loads((TIMELINES / name).read_text())


def test_recorded_callee_hangups_are_the_case_under_test():
    # The 4 callee hang-ups were all flagged as transcription failures; the 3 caller ones were not.
    sources = [recorded(f"call_0{i}.json")["attempt"]["provider_hangup_source"] for i in range(1, 8)]
    assert sources.count("callee") == 4


@pytest.mark.database
def test_prospect_hangup_with_audio_ending_first_leaves_no_gap_and_no_block(workspace, app):
    """R0.2: the audio track ends a moment before call.hangup, as on every callee hang-up."""
    service = app.state.service
    connected(service, workspace)
    call = start(workspace)
    simulate(workspace, call["id"], "call.answered")
    simulate(workspace, call["id"], "call.hangup", hangup_source="callee", hangup_cause="normal_clearing")
    # The browser's audio ended first; its gap report lands after the hangup event.
    r = workspace["client"].post(f"/v1/calls/{call['id']}/audio-gap", headers=workspace["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ignored"
    body = workspace["client"].get(f"/v1/calls/{call['id']}", headers=workspace["headers"]).json()
    assert not body.get("analysis_incomplete")
    assert status(workspace)["connected"] is True
    next_call = can_dial_next(workspace)
    assert next_call.status_code == 200, next_call.text


@pytest.mark.database
def test_a_real_speech_gap_warns_but_never_blocks_dialing(workspace, app):
    """R0.5: missing speech marks the call and shows a warning; calling continues."""
    service = app.state.service
    connected(service, workspace)
    call = start(workspace)
    simulate(workspace, call["id"], "call.answered")
    r = workspace["client"].post(f"/v1/calls/{call['id']}/audio-gap", headers=workspace["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "recorded"
    simulate(workspace, call["id"], "call.hangup", hangup_source="caller", hangup_cause="normal_clearing")
    current = status(workspace)
    assert current["connected"] is True
    assert current["speech_warning"]
    next_call = can_dial_next(workspace)
    assert next_call.status_code == 200, next_call.text


@pytest.mark.database
async def test_an_ended_agent_leg_keeps_the_rep_ready_for_the_next_call(workspace, app):
    """R0.3: the agent leg ends with every call; only Pause pauses the rep."""
    client, headers = workspace["client"], workspace["headers"]
    created = client.post("/v1/telephony/agent", headers=headers)
    assert created.status_code == 200, created.text
    leg = created.json()["agent_call_control_id"]
    ready(workspace, agent_call_control_id=leg)
    service = app.state.service
    event = {
        "id": "agent-leg-ended",
        "event_type": "call.hangup",
        "payload": {"call_control_id": leg, "hangup_cause": "normal_clearing", "hangup_source": "caller"},
    }
    await ops.accept_webhook(service, workspace["tenant_id"], event, "agent-hash", verified=True)
    await ops.run_jobs(service, queues=["webhook"])
    with service.db.transaction(False) as s:
        presence = get(s, "users", workspace["user_id"], workspace["tenant_id"]).data["presence"]
    assert presence["status"] == "ready"
    assert presence["audio_ready"] is False
    # Audio reconnects with a fresh leg, and the next call starts without Go ready.
    renewed = client.post("/v1/telephony/agent", headers=headers).json()["agent_call_control_id"]
    ready(workspace, agent_call_control_id=renewed)
    next_call = can_dial_next(workspace)
    assert next_call.status_code == 200, next_call.text


@pytest.mark.database
async def test_the_reps_own_sip_leg_finishes_without_a_failed_job(workspace, app):
    """R0.4: 49 of 64 dead webhook jobs were the rep's own SIP leg, which carries none of our state."""
    service = app.state.service
    sip_leg = next(e for e in recorded("call_02.json")["events"] if e["leg"] == "unmatched-1")
    assert "client_state" not in sip_leg
    event = {
        "id": "rep-sip-leg-hangup",
        "event_type": sip_leg["event_type"],
        "payload": {
            "call_control_id": "v3:rep-own-sip-leg",
            "hangup_cause": sip_leg["hangup_cause"],
            "hangup_source": sip_leg["hangup_source"],
        },
    }
    await ops.accept_webhook(service, workspace["tenant_id"], event, "sip-hash", verified=True)
    await ops.run_jobs(service, queues=["webhook"])
    with service.db.transaction(False) as s:
        receipt = s.scalar(select(MODELS["webhook_inbox"]))
        assert receipt.data["status"] == "ignored"
        jobs = list(s.scalars(select(Job).where(Job.queue == "webhook")))
        assert jobs and all(job.status == "completed" for job in jobs), [(j.status, j.error) for j in jobs]


@pytest.mark.database
def test_dial_does_not_park_the_prospect_when_the_rep_leg_ends(workspace, app):
    """R0.8: a parked prospect leg outlives the rep's lost audio and waits in silence for up to 2 hours."""
    service = app.state.service
    start(workspace)
    with service.db.transaction(False) as s:
        dials = [c for c in s.scalars(select(MODELS["command_outbox"])) if c.data.get("kind") == "dial"]
    assert dials
    assert all("park_after_unbridge" not in c.data["payload"] for c in dials)
    assert all(c.data["payload"]["bridge_on_answer"] for c in dials)


@pytest.mark.database
def test_the_call_api_says_who_hung_up_and_for_how_long(workspace, app):
    """R0.10: the Dialer shows "They hung up · 0:23" from these fields."""
    call = start(workspace)
    simulate(workspace, call["id"], "call.answered")
    simulate(workspace, call["id"], "call.hangup", hangup_source="callee", hangup_cause="normal_clearing")
    body = workspace["client"].get(f"/v1/calls/{call['id']}", headers=workspace["headers"]).json()
    assert body["provider_hangup_source"] == "callee"
    assert "connected_seconds" in body
