"""Crash boundaries discovered while repairing the original acceptance cases."""

from datetime import timedelta

import pytest
from app.db import update
from app.providers.inference import InferenceResult
from conftest import start


def principal(w):
    return {k: w[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


@pytest.mark.parametrize(
    "event_type,payload", [("call.dtmf.received", {"digit": "9"}), ("call.gather.ended", {"digits": "9"})]
)
def test_optout_is_retried_if_suppression_write_fails(workspace, app, monkeypatch, event_type, payload):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        update(service.required(s, "call_attempts", call["id"], p), voicemail_asset="approved")
    original = service.suppress

    def unavailable(*args, **kwargs):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(service, "suppress", unavailable)
    with pytest.raises(RuntimeError):
        service.event(p, "press-nine", event_type, call["id"], payload)
    monkeypatch.setattr(service, "suppress", original)
    service.event(p, "press-nine", event_type, call["id"], payload)
    with service.db.transaction(False) as s:
        lead = service.required(s, "leads", call["lead_id"], p)
        assert service.suppressed(s, p, lead.data["phone"])


@pytest.mark.asyncio
async def test_unfinished_webhook_recovers_without_another_provider_delivery(
    workspace, app, clock, monkeypatch
):
    call = start(workspace)
    service = app.state.service
    original = service._accept_transcript

    def unavailable(*args, **kwargs):
        raise RuntimeError("processing interrupted")

    monkeypatch.setattr(service, "_accept_transcript", unavailable)
    event = {
        "id": "worker-retry",
        "event_type": "call.transcription",
        "payload": {
            "call_control_id": call["provider_id"],
            "transcription_data": {"track": "inbound", "is_final": True, "transcript": "We need help."},
        },
    }
    receipt = await service.accept_webhook(workspace["tenant_id"], event, "body-hash")
    assert receipt["status"] == "received"
    assert (await service.run_jobs(tenant_id=workspace["tenant_id"], queues=["webhook"]))["processed"] == 0
    monkeypatch.setattr(service, "_accept_transcript", original)
    clock.now += timedelta(seconds=11)
    await service.maintenance_tick()
    await service.run_jobs(tenant_id=workspace["tenant_id"], queues=["webhook"])
    saved = workspace["client"].get("/v1/calls/" + call["id"], headers=workspace["headers"]).json()
    assert len(saved["transcript"]) == 1


def test_missing_billing_cost_keeps_uncertain_hold_not_zero_charge(workspace, app):
    service, p = app.state.service, principal(workspace)
    service.reserve_optional_budget(p, "unknown", 0.02, object_id="call", work_kind="live_coach")
    service.usage(
        p,
        "openrouter",
        "live_coach",
        InferenceResult({}, {"tokens": 50}, "receipt"),
        object_id="call",
        request_key="unknown",
    )
    with service.db.transaction(False) as s:
        entries = [r.data for r in service.scoped(s, "usage_ledger", p)]
    assert len(entries) == 1
    assert entries[0]["status"] == "uncertain" and entries[0]["cost_usd"] is None
    assert entries[0]["reserved_usd"] == 0.02


@pytest.mark.asyncio
async def test_unexpired_control_owner_is_not_replayed(workspace, app, carrier):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    with service.db.transaction() as s:
        command = service.control_intent(s, p, call["id"], "answer", {})
        update(
            command,
            status="sending",
            lease_owner="live-worker",
            sent_at=service.clock().isoformat(),
            lease_until=(service.clock() + timedelta(seconds=30)).isoformat(),
        )
        identifier = command.id
    await service.recover(p)
    await service.dispatch_controls(p)
    with service.db.transaction(False) as s:
        assert service.required(s, "command_outbox", identifier, p).data["lease_owner"] == "live-worker"


@pytest.mark.asyncio
async def test_transcript_retry_finishes_optout_after_receipt_write(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    segment = {
        "segment_id": "retry-optout",
        "revision": 1,
        "side": "remote",
        "is_final": True,
        "text": "Do not call me again.",
    }
    original = service.suppress

    def unavailable(*args, **kwargs):
        raise RuntimeError("suppression commit interrupted")

    monkeypatch.setattr(service, "suppress", unavailable)
    with pytest.raises(RuntimeError):
        await service.transcript(p, call["id"], segment)
    monkeypatch.setattr(service, "suppress", original)
    await service.transcript(p, call["id"], segment)
    with service.db.transaction(False) as s:
        lead = service.required(s, "leads", call["lead_id"], p)
        assert service.suppressed(s, p, lead.data["phone"])


def test_old_provider_setting_cannot_enable_non_zdr_coaching(workspace):
    response = workspace["client"].put(
        "/v1/admin/settings", headers=workspace["headers"], json={"coach_provider": "groq"}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_completed_transcript_retry_does_not_repeat_coaching_after_cooldown(
    workspace, app, clock, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    seen = []

    async def coach(*args, **kwargs):
        seen.append(call["id"])
        return {"action": "NO_ACTION"}

    monkeypatch.setattr(service, "coach_turn", coach)
    segment = {
        "segment_id": "retry-completed",
        "revision": 1,
        "side": "remote",
        "is_final": True,
        "text": "We need a useful explanation.",
    }
    await service.transcript(p, call["id"], segment)
    clock.now += timedelta(seconds=60)
    await service.transcript(p, call["id"], segment)
    assert seen == [call["id"]]
