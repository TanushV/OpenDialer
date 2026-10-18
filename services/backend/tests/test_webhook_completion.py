"""A04/A05: prompt durable admission and one atomic reducer/receipt boundary."""

import asyncio
import base64
import json
import time
from contextlib import contextmanager
from datetime import timedelta

import pytest
from app.db import Job, get, put, rows
from app.privacy import event_metadata
from conftest import start
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import select


def principal(w):
    return {k: w[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


def speech(call, text="We need a clearer explanation.", revision=1):
    return {
        "call_control_id": call["provider_id"],
        "transcription_data": {
            "segment_id": "atomic-speech",
            "revision": revision,
            "transcription_track": "inbound",
            "is_final": True,
            "transcript": text,
        },
    }


def pending(w, event_type, payload, event_id="atomic-event"):
    service = w["client"].app.state.service
    with service.db.transaction() as s:
        receipt = put(
            s,
            "webhook_inbox",
            w["tenant_id"],
            "",
            {
                "event_id": event_id,
                "event_type": event_type,
                "payload": event_metadata(payload),
                "hash": event_id,
                "status": "received",
                "received_at": service.clock().isoformat(),
                "raw_expires_at": (service.clock() + timedelta(seconds=300)).isoformat(),
            },
            key=event_id,
            now=service.clock(),
        )
        identity = receipt.id
    if event_type == "call.transcription":
        service.raw.put("webhook:" + identity, payload)
    return identity


def break_completion(service, monkeypatch):
    original = service._finish_webhook

    def fail_once(*args, **kwargs):
        if args[3] == "processed":
            raise RuntimeError("interrupted before completion commit")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_finish_webhook", fail_once)
    return original


@pytest.mark.asyncio
async def test_acceptance_acknowledges_receipt_without_executing_reducer(workspace, app, monkeypatch):
    call = start(workspace)
    service = app.state.service
    invoked = []

    async def slow_reducer(*args, **kwargs):
        invoked.append(True)
        await asyncio.sleep(0.15)
        return {"received": True}

    monkeypatch.setattr(service, "process_webhook", slow_reducer)
    result = await service.accept_webhook(
        workspace["tenant_id"],
        {"id": "ack", "event_type": "call.hangup", "payload": {"call_control_id": call["provider_id"]}},
        "ack-hash",
    )
    assert not invoked, "HTTP acknowledgement must not await reduction"
    with service.db.transaction(False) as s:
        receipt = rows(s, "webhook_inbox", workspace["tenant_id"])[0]
        jobs = list(s.scalars(select(Job).where(Job.queue == "webhook")))
        assert receipt.data["status"] == result["status"] == "received"
        assert len(jobs) == 1 and jobs[0].payload == {"receipt_id": receipt.id}


@pytest.mark.asyncio
async def test_receipt_and_processing_job_are_one_transaction(workspace, app, monkeypatch):
    service = app.state.service

    def fail_job(*args, **kwargs):
        raise RuntimeError("queue insert failed")

    monkeypatch.setattr(service.jobs, "enqueue_in", fail_job)
    with pytest.raises(RuntimeError):
        await service.accept_webhook(
            workspace["tenant_id"],
            {"id": "queue-failure", "event_type": "call.hangup", "payload": {"call_control_id": "unknown"}},
            "queue-hash",
        )
    with service.db.transaction(False) as s:
        assert not rows(s, "webhook_inbox", workspace["tenant_id"]), "No acknowledged unqueued receipt"


@pytest.mark.asyncio
async def test_slow_receipt_storage_does_not_block_control_event_loop(workspace, app, monkeypatch):
    service = app.state.service
    original = service.db.transaction

    @contextmanager
    def delayed(*args, **kwargs):
        time.sleep(0.12)
        with original(*args, **kwargs) as s:
            yield s

    monkeypatch.setattr(service.db, "transaction", delayed)
    delays = []

    async def heartbeat():
        before = time.monotonic()
        await asyncio.sleep(0.01)
        delays.append(time.monotonic() - before)

    await asyncio.gather(
        heartbeat(),
        service.accept_webhook(
            workspace["tenant_id"],
            {"id": "slow-store", "event_type": "call.hangup", "payload": {"call_control_id": "unknown"}},
            "slow-hash",
        ),
    )
    assert max(delays) < 0.08, delays


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["call.hangup", "call.answered", "call.transcription"])
async def test_completion_fault_rolls_back_reducer_transcript_controls_and_jobs(
    workspace, app, clock, monkeypatch, event_type
):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    payload = speech(call) if event_type == "call.transcription" else {"call_control_id": call["provider_id"]}
    identity = pending(workspace, event_type, payload)
    original = break_completion(service, monkeypatch)
    with pytest.raises(RuntimeError):
        await service.process_webhook(workspace["tenant_id"], identity)
    with service.db.transaction(False) as s:
        current = service.required(s, "call_attempts", call["id"], p)
        assert current.data["state"] == call["state"]
        assert current.data.get("transcript_version", 0) == call.get("transcript_version", 0)
        assert not service.scoped(s, "provider_events", p)
        assert not service.transcript_rows(s, p, call["id"])
        assert not [r for r in service.scoped(s, "command_outbox", p) if r.data.get("kind") == "action"]
        assert not list(s.scalars(select(Job).where(Job.queue.in_(["live_coach", "postcall"]))))
    monkeypatch.setattr(service, "_finish_webhook", original)
    clock.now += timedelta(seconds=31)
    result = await service.process_webhook(workspace["tenant_id"], identity)
    assert result["status"] == "processed"
    with service.db.transaction(False) as s:
        assert len(service.scoped(s, "provider_events", p)) == 1
        if event_type == "call.transcription":
            assert len(service.transcript_rows(s, p, call["id"])) == 1


@pytest.mark.asyncio
async def test_failed_correction_does_not_replace_committed_raw_text(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    first = pending(workspace, "call.transcription", speech(call), "first-version")
    await service.process_webhook(workspace["tenant_id"], first)
    corrected = pending(
        workspace, "call.transcription", speech(call, "The corrected constraint.", 2), "correction"
    )
    break_completion(service, monkeypatch)
    with pytest.raises(RuntimeError):
        await service.process_webhook(workspace["tenant_id"], corrected)
    with service.db.transaction(False) as s:
        segments = service.transcript_rows(s, p, call["id"])
        assert segments[0]["revision"] == 1
        assert segments[0]["text"] == "We need a clearer explanation."


@pytest.mark.asyncio
async def test_inbound_routing_rolls_back_with_receipt_completion(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    identity = pending(
        workspace,
        "call.initiated",
        {
            "call_control_id": "atomic-inbound",
            "direction": "incoming",
            "to": "+14155550100",
            "from": "+14155550101",
        },
    )
    break_completion(service, monkeypatch)
    with pytest.raises(RuntimeError):
        await service.process_webhook(workspace["tenant_id"], identity)
    with service.db.transaction(False) as s:
        assert not service.scoped(s, "followup_tasks", p)
        assert not service.scoped(s, "command_outbox", p)


@pytest.mark.asyncio
async def test_agent_state_rolls_back_with_receipt_completion(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        leg = put(
            s,
            "call_legs",
            p["tenant_id"],
            p["seller_id"],
            {"kind": "agent", "provider_id": "atomic-agent", "state": "ringing", "rep_id": p["user_id"]},
        )
        leg_id = leg.id
    identity = pending(workspace, "call.answered", {"call_control_id": "atomic-agent"})
    break_completion(service, monkeypatch)
    with pytest.raises(RuntimeError):
        await service.process_webhook(workspace["tenant_id"], identity)
    with service.db.transaction(False) as s:
        assert get(s, "call_legs", leg_id).data["state"] == "ringing"


@pytest.mark.asyncio
async def test_duplicate_receipts_keep_one_job_and_original_raw_deadline(workspace, app, clock):
    service = app.state.service
    call = start(workspace)
    event = {"id": "same", "event_type": "call.transcription", "payload": speech(call)}
    await service.accept_webhook(workspace["tenant_id"], event, "same-body")
    clock.now += timedelta(seconds=299)
    await service.accept_webhook(workspace["tenant_id"], event, "same-body")
    with service.db.transaction(False) as s:
        receipt = rows(s, "webhook_inbox", workspace["tenant_id"])[0]
        assert len(list(s.scalars(select(Job).where(Job.queue == "webhook")))) == 1
        identity = receipt.id
    clock.now += timedelta(seconds=2)
    result = await service.process_webhook(workspace["tenant_id"], identity)
    assert result["status"] == "expired_raw"
    with service.db.transaction(False) as s:
        assert not service.transcript_rows(s, principal(workspace), call["id"])


def test_unmatched_event_recovery_cannot_grow_unbounded_job_history(workspace, app, clock):
    service = app.state.service
    identity = pending(workspace, "call.hangup", {"call_control_id": "not-known-yet"})
    for _ in range(10):
        service.queue_unfinished_webhooks()
        clock.now += timedelta(seconds=31)
    with service.db.transaction(False) as s:
        jobs = [j for j in s.scalars(select(Job)) if j.payload.get("receipt_id") == identity]
        assert len(jobs) == 1


def test_signed_ingress_stays_successful_when_processing_is_temporarily_unavailable(
    workspace, app, clock, monkeypatch
):
    service = app.state.service
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    service.vault.write(workspace["tenant_id"], {"telnyx_public_key": public})
    body = json.dumps(
        {"data": {"id": "http-ack", "event_type": "call.hangup", "payload": {"call_control_id": "unknown"}}}
    ).encode()
    timestamp = str(int(clock.now.timestamp()))
    headers = {
        "telnyx-timestamp": timestamp,
        "telnyx-signature-ed25519": base64.b64encode(private.sign(timestamp.encode() + b"|" + body)).decode(),
    }

    async def unavailable(*args, **kwargs):
        raise RuntimeError("worker unavailable")

    monkeypatch.setattr(service, "process_webhook", unavailable)
    response = workspace["client"].post("/webhooks/telnyx", content=body, headers=headers)
    assert response.status_code == 200
    with service.db.transaction(False) as s:
        assert rows(s, "webhook_inbox", workspace["tenant_id"])[0].data["status"] == "received"
