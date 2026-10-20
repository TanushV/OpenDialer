"""A04/A05: prompt durable admission and one atomic reducer/receipt boundary."""

import asyncio
import base64
import json
from contextlib import contextmanager
from datetime import timedelta

import pytest
from app.db import Job, get, put, rows, update
from app.privacy import event_metadata
from conftest import start
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import event as sql_event
from sqlalchemy import select


@pytest.mark.parametrize("present", [False, True])
@pytest.mark.sqlite_only
def test_provider_call_lookup_has_bounded_sql_work(workspace, app, clock, present):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as session:
        for i in range(600):
            put(
                session,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {"provider_id": f"historical-{i}", "state": "FINALIZED"},
                now=clock.now,
            )
        target = put(
            session,
            "call_attempts",
            p["tenant_id"],
            p["seller_id"],
            {"provider_id": "actual-target", "state": "DIALING"},
            now=clock.now,
        )
        target_id = target.id
    steps = [0]
    with service.db.transaction(False) as session:
        connection = session.connection().connection.driver_connection

        def tick():
            steps[0] += 1
            return 0

        connection.set_progress_handler(tick, 1)
        try:
            call = service._webhook_call(
                session, p["tenant_id"], {"call_control_id": "actual-target" if present else "missing"}
            )
            assert (call.id if call else None) == (target_id if present else None)
        finally:
            connection.set_progress_handler(None, 0)
    assert steps[0] < 200, "A tenant's call history must not be scanned for each event"


def test_ambiguous_provider_call_cannot_apply_event_to_arbitrary_call(workspace, app, clock):
    from fastapi import HTTPException
    from sqlalchemy import inspect

    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as session:
        calls = [
            put(
                session,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {"provider_id": "duplicate-provider-id", "state": "DIALING"},
                now=clock.now,
            )
            for _ in range(2)
        ]
        target = calls[0].id
    payload = {
        "call_control_id": "duplicate-provider-id",
        "client_state": base64.b64encode(json.dumps({"call_id": target}).encode()).decode(),
    }
    with service.db.transaction(False) as session, pytest.raises(HTTPException) as error:
        service._webhook_call(session, p["tenant_id"], payload)
    assert error.value.status_code == 409
    assert "ambiguous" in str(error.value.detail).lower()
    assert "ix_call_attempts_provider_identity" in {
        row["name"] for row in inspect(service.db.engine).get_indexes("call_attempts")
    }


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
    from conftest import LoopResponsiveness

    service = app.state.service
    original = service.db.transaction
    loop = LoopResponsiveness()

    @contextmanager
    def delayed(*args, **kwargs):
        # Storage stays blocked until the control loop has served other work.
        loop.wait_for_loop()
        with original(*args, **kwargs) as s:
            yield s

    monkeypatch.setattr(service.db, "transaction", delayed)
    await service.accept_webhook(
        workspace["tenant_id"],
        {"id": "slow-store", "event_type": "call.hangup", "payload": {"call_control_id": "unknown"}},
        "slow-hash",
    )
    loop.assert_responsive()


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


def test_late_raw_publication_cannot_replace_a_newer_committed_revision(app):
    raw = app.state.service.raw
    raw.put("speech:test", {"revision": 2, "text": "newer"}, scope="call", revision=2)
    raw.put("speech:test", {"revision": 1, "text": "older"}, scope="call", revision=1)
    assert raw.get("speech:test")["text"] == "newer"


@pytest.mark.asyncio
async def test_raw_payload_is_available_when_its_job_becomes_visible(workspace, app):
    service = app.state.service
    call = start(workspace)
    observations = []

    def committed(session):
        receipts = [
            r for r in session.identity_map.values() if getattr(r, "__tablename__", "") == "webhook_inbox"
        ]
        for receipt in receipts:
            observations.append(service.raw.get("webhook:" + receipt.id) is not None)

    sql_event.listen(service.db.sessions, "after_commit", committed)
    try:
        await service.accept_webhook(
            workspace["tenant_id"],
            {"id": "publish-before-job", "event_type": "call.transcription", "payload": speech(call)},
            "payload-hash",
        )
    finally:
        sql_event.remove(service.db.sessions, "after_commit", committed)
    assert observations and all(observations), observations


@pytest.mark.asyncio
async def test_unmatched_receipt_retries_same_job_then_reports_review(workspace, app, clock):
    service = app.state.service
    result = await service.accept_webhook(
        workspace["tenant_id"],
        {"id": "missing-call", "event_type": "call.hangup", "payload": {"call_control_id": "not-created"}},
        "missing-call-hash",
    )
    for _ in range(4):
        await service.run_jobs(tenant_id=workspace["tenant_id"], queues=["webhook"])
        service.queue_unfinished_webhooks()
        clock.now += timedelta(seconds=31)
    with service.db.transaction(False) as s:
        receipt = get(s, "webhook_inbox", result["receipt_id"])
        jobs = list(s.scalars(select(Job).where(Job.queue == "webhook")))
        assert len(jobs) == 1 and jobs[0].status == "dead" and jobs[0].attempts == 3
        assert receipt.data["status"] == "review_required"
        assert receipt.data.get("error")


@pytest.mark.asyncio
async def test_legacy_completed_job_with_unfinished_receipt_resumes_once(workspace, app):
    service = app.state.service
    call = start(workspace)
    identity = pending(workspace, "call.hangup", {"call_control_id": call["provider_id"]})
    with service.db.transaction() as s:
        job = service.jobs.enqueue_in(
            s, "webhook", {"receipt_id": identity}, "webhook:" + identity, tenant_id=workspace["tenant_id"]
        )
        row = s.get(Job, job["id"])
        row.status, row.attempts = "completed", 1
    service.queue_unfinished_webhooks()
    await service.run_jobs(tenant_id=workspace["tenant_id"], queues=["webhook"])
    with service.db.transaction(False) as s:
        assert get(s, "webhook_inbox", identity).data["status"] == "processed"
        assert s.get(Job, job["id"]).attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_lost_unprocessed_speech_is_visible_on_the_call(workspace, app, clock, restart):
    service = app.state.service
    call = start(workspace)
    receipt = await service.accept_webhook(
        workspace["tenant_id"],
        {"id": "lost-speech", "event_type": "call.transcription", "payload": speech(call)},
        "lost-body",
    )
    if restart:
        service.raw.clear()
    else:
        clock.now += timedelta(seconds=301)
    await service.process_webhook(workspace["tenant_id"], receipt["receipt_id"])
    current = workspace["client"].get("/v1/calls/" + call["id"], headers=workspace["headers"]).json()
    assert current["raw_evidence_status"] == ("unavailable_after_restart" if restart else "expired")
    assert current["unprocessed_speech_events"] == 1
    assert current["analysis_incomplete"] is True


@pytest.mark.asyncio
@pytest.mark.database
async def test_concurrent_admission_and_processing_apply_the_event_once(workspace, app):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    event = {"id": "raced-delivery", "event_type": "call.transcription", "payload": speech(call)}
    receipts = await asyncio.gather(
        *(service.accept_webhook(workspace["tenant_id"], event, "identical-body") for _ in range(4))
    )
    identities = {r["receipt_id"] for r in receipts}
    assert len(identities) == 1
    await asyncio.gather(
        *(service.process_webhook(workspace["tenant_id"], next(iter(identities))) for _ in range(4))
    )
    with service.db.transaction(False) as s:
        assert len(service.scoped(s, "provider_events", p)) == 1
        assert len(service.scoped(s, "transcript_segments", p)) == 1
        assert len(list(s.scalars(select(Job).where(Job.queue == "live_coach")))) == 1


@pytest.mark.asyncio
async def test_receipt_hash_collision_does_not_replace_original_buffer(workspace, app):
    from fastapi import HTTPException

    service = app.state.service
    call = start(workspace)
    receipt = await service.accept_webhook(
        workspace["tenant_id"],
        {"id": "collision-raw", "event_type": "call.transcription", "payload": speech(call)},
        "original",
    )
    with pytest.raises(HTTPException) as exc:
        await service.accept_webhook(
            workspace["tenant_id"],
            {
                "id": "collision-raw",
                "event_type": "call.transcription",
                "payload": speech(call, "Conflicting text."),
            },
            "altered",
        )
    assert exc.value.status_code == 409
    assert (
        service.raw.get("webhook:" + receipt["receipt_id"])["transcription_data"]["transcript"]
        == "We need a clearer explanation."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_event_identity_cannot_resolve_to_the_wrong_or_unknown_leg(workspace, app, missing):
    service = app.state.service
    call = start(workspace)
    if missing:
        with service.db.transaction() as s:
            update(get(s, "call_attempts", call["id"]), provider_id=None)
        payload = {"direction": "outgoing"}
    else:
        payload = {
            "call_control_id": "different-call",
            "client_state": base64.b64encode(json.dumps({"call_id": call["id"]}).encode()).decode(),
        }
    identity = pending(workspace, "call.hangup", payload)
    result = await service.process_webhook(workspace["tenant_id"], identity)
    assert result["status"] == "unmatched"
    with service.db.transaction(False) as s:
        assert get(s, "call_attempts", call["id"]).data["state"] == call["state"]
