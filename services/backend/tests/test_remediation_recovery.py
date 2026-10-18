"""Crash boundaries discovered while repairing the original acceptance cases."""

from datetime import timedelta

import pytest
from app.db import update
from app.providers.inference import InferenceResult
from conftest import start


def principal(w):
    return {k: w[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


def test_optout_is_retried_if_suppression_write_fails(workspace, app, monkeypatch):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        update(service.required(s, "call_attempts", call["id"], p), voicemail_asset="approved")
    original = service.suppress

    def unavailable(*args, **kwargs):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(service, "suppress", unavailable)
    with pytest.raises(RuntimeError):
        service.event(p, "press-nine", "call.dtmf.received", call["id"], {"digit": "9"})
    monkeypatch.setattr(service, "suppress", original)
    service.event(p, "press-nine", "call.dtmf.received", call["id"], {"digit": "9"})
    with service.db.transaction(False) as s:
        lead = service.required(s, "leads", call["lead_id"], p)
        assert service.suppressed(s, p, lead.data["phone"])


@pytest.mark.asyncio
async def test_unfinished_webhook_recovers_without_another_provider_delivery(workspace, app, monkeypatch):
    call = start(workspace)
    service = app.state.service
    original = service.transcript

    async def unavailable(*args, **kwargs):
        raise RuntimeError("processing interrupted")

    monkeypatch.setattr(service, "transcript", unavailable)
    event = {
        "id": "worker-retry",
        "event_type": "call.transcription",
        "payload": {
            "call_control_id": call["provider_id"],
            "transcription_data": {"track": "inbound", "is_final": True, "transcript": "We need help."},
        },
    }
    with pytest.raises(RuntimeError):
        await service.accept_webhook(workspace["tenant_id"], event, "body-hash")
    monkeypatch.setattr(service, "transcript", original)
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
