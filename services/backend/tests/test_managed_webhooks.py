"""Managed receipts reuse durable ingress; acknowledgement follows the local commit."""

from datetime import timedelta

import pytest
from app.db import MODELS
from app.jobs.queue import aware
from sqlalchemy import select


@pytest.mark.database
async def test_original_receipt_time_survives_delivery(app, clock):
    service = app.state.service
    original = clock() - timedelta(minutes=4)
    event = {"id": "relay-event", "event_type": "call.hangup", "payload": {"call_control_id": "absent"}}
    first = await service.accept_webhook(
        "test-tenant", event, "same-hash", verified=True, received_at=original
    )
    second = await service.accept_webhook(
        "test-tenant", event, "same-hash", verified=True, received_at=clock()
    )
    assert first["receipt_id"] == second["receipt_id"]
    with service.db.transaction(False) as s:
        receipt = s.scalar(select(MODELS["webhook_inbox"]))
        assert receipt.data["received_at"] == original.isoformat()
        assert receipt.data["raw_expires_at"] == (original + timedelta(seconds=300)).isoformat()


async def test_delivery_does_not_ack_failed_local_commit():
    from app.managed_webhooks import deliver

    class Service:
        async def accept_webhook(self, *args, **kwargs):
            raise RuntimeError("database offline")

    row = {
        "account_id": "account",
        "event_id": "event",
        "hash": "hash",
        "event": {},
        "received_at": "2026-10-29T01:00:00+00:00",
    }
    with pytest.raises(RuntimeError, match="database offline"):
        await deliver(Service(), "tenant", [row])


async def test_session_storage_is_private_and_persistent(tmp_path):
    from app.managed_webhooks import SessionStorage

    path = tmp_path / "session.json"
    storage = SessionStorage(path)
    await storage.set_item("session", "private-refresh-value")
    assert path.stat().st_mode & 0o777 == 0o600
    assert await SessionStorage(path).get_item("session") == "private-refresh-value"
    await storage.remove_item("session")
    assert await SessionStorage(path).get_item("session") is None


@pytest.mark.database
async def test_old_inbound_receipt_creates_review_without_answering(workspace, app, clock, carrier):
    service = app.state.service
    event = {
        "id": "late-inbound",
        "event_type": "call.initiated",
        "payload": {
            "call_control_id": "late-provider",
            "direction": "incoming",
            "to": "+14155550100",
            "from": "+14155550123",
        },
    }
    await service.accept_webhook(
        workspace["tenant_id"], event, "late-hash", verified=True, received_at=clock() - timedelta(minutes=2)
    )
    await service.run_jobs(queues=["webhook"])
    with service.db.transaction(False) as s:
        task = s.scalar(
            select(MODELS["followup_tasks"]).where(
                MODELS["followup_tasks"].external_key == "callback:late-provider"
            )
        )
        assert task.data["inbound_state"] == "ended"
        assert task.data["status"] == "open"
        commands = list(s.scalars(select(MODELS["command_outbox"])))
        assert not any(row.data.get("provider_id") == "late-provider" for row in commands)


def test_probe_receipt_does_not_trigger_another_exchange(app):
    from app.managed_webhooks import ManagedConnection

    connection = ManagedConnection(app.state.service)
    connection.probe = "nonce"
    connection.broadcast({"payload": {"probe": "nonce"}})
    assert connection.last_probe > 0
    assert not connection.status()["connected"]
    assert not connection.wake.is_set()


async def test_failed_import_preserves_existing_refresh_session(app):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.managed_webhooks import ConnectionProfile, ManagedConnection
    from fastapi import HTTPException

    connection = ManagedConnection(app.state.service)
    await connection.storage.set_item("old-session", "must-survive")

    async def make(config, storage=None):
        await (storage or connection.storage).set_item("old-session", "wrong-account")
        return SimpleNamespace(
            auth=SimpleNamespace(
                sign_in_with_password=AsyncMock(
                    return_value=SimpleNamespace(user=SimpleNamespace(id="wrong-id"))
                ),
                close=AsyncMock(),
            ),
            postgrest=SimpleNamespace(aclose=AsyncMock()),
        )

    connection.make_client = make
    profile = ConnectionProfile(
        label="Test",
        url="https://abcdefghijklmnopqrst.supabase.co",
        publishable_key="sb_publishable_test-only",
        workspace_id="d1273570-3838-4900-96b3-c8ff8f9d9471",
        email="test@example.invalid",
        password="synthetic-password",
    )
    with pytest.raises(HTTPException):
        await connection.connect("tenant", profile)
    assert await connection.storage.get_item("old-session") == "must-survive"


@pytest.mark.database
def test_two_second_window_does_not_slide_with_continuous_speech(app, clock):
    from types import SimpleNamespace

    from app.db import Job

    service = app.state.service
    call = SimpleNamespace(
        id="streamed-call", data={"rep_id": "rep", "transcript_version": 1, "transcription_source": "direct"}
    )
    p = {"tenant_id": "tenant", "seller_id": "seller"}
    began = clock()
    with service.db.transaction() as s:
        service._defer_help(s, p, call, clock() + timedelta(milliseconds=300), "final")
    for i in range(1, 10):
        clock.now = began + timedelta(milliseconds=200 * i)
        call.data["transcript_version"] += 1
        with service.db.transaction() as s:
            service._defer_help(s, p, call, clock() + timedelta(milliseconds=300), "final")
    with service.db.transaction(False) as s:
        job = s.scalar(select(Job).where(Job.key == "coach:streamed-call"))
        assert aware(job.available_at) == began + timedelta(seconds=2)
        assert job.payload["state_version"] == 10
    assert not service.jobs.claim_many("live_coach", "worker", limit=1)
    clock.now = began + timedelta(seconds=2)
    assert len(service.jobs.claim_many("live_coach", "worker", limit=1)) == 1


@pytest.mark.database
def test_two_second_admission_spacing_preserves_direct_path_defaults(app, clock):
    from types import SimpleNamespace

    service = app.state.service
    p = {"tenant_id": "tenant", "seller_id": "seller"}
    call = SimpleNamespace(
        id="streamed-call", data={"rep_id": "rep", "transcript_version": 2, "transcription_source": "direct"}
    )
    state = SimpleNamespace(
        data={"automatic_admissions": [{"at": (clock() - timedelta(seconds=1)).isoformat()}]}
    )
    with service.db.transaction() as s:
        assert service._automatic_admissions_in(s, p, call, state) is None
        call.data["transcription_source"] = "webhook"
        assert len(service._automatic_admissions_in(s, p, call, state)) == 1


def test_connection_status_is_available_without_secret_fields(workspace):
    r = workspace["client"].get("/v1/managed-connection", headers=workspace["headers"])
    assert r.status_code == 200
    assert r.json()["configured"] is False
    assert not {"password", "access_token", "refresh_token", "telnyx_api_key"} & r.json().keys()


@pytest.mark.database
def test_disconnected_managed_workspace_blocks_new_calls(workspace, app):
    from conftest import reserve

    reservation = reserve(workspace).json()
    app.state.service.managed.config = {
        "tenant_id": workspace["tenant_id"],
        "label": "Test",
        "workspace_id": "test",
    }
    r = workspace["client"].post(
        "/v1/calls/start",
        headers=workspace["headers"],
        json={"reservation_id": reservation["reservation_id"], "idempotency_key": "offline-managed"},
    )
    assert r.status_code == 409
    assert "reconnect" in r.text.lower()


@pytest.mark.database
def test_managed_call_uses_direct_transcription_only(workspace, app, clock):
    from time import monotonic

    from app.db import get
    from conftest import start

    service = app.state.service
    service.managed.config = {"tenant_id": workspace["tenant_id"], "workspace_id": "test"}
    service.managed.last_probe = monotonic()
    service.managed.caught_up = True
    service.managed.numbers = {"+14155550100"}
    call = start(workspace)
    p = {k: workspace[k] for k in ["tenant_id", "seller_id", "user_id"]}
    with service.db.transaction() as s:
        row = get(s, "call_attempts", call["id"], p["tenant_id"])
        assert row.data["transcription_source"] == "direct"
        service.start_transcription_in(s, p, row)
        commands = list(s.scalars(select(MODELS["command_outbox"])))
        assert not any(x.data.get("action") == "transcription_start" for x in commands)
        assert row.data["transcription_layout"] == "direct_tracks"


def test_managed_profile_cannot_change_while_call_active(workspace, app):
    from conftest import start

    start(workspace)
    r = workspace["client"].post(
        "/v1/admin/managed-connection",
        headers=workspace["headers"],
        json={
            "version": 1,
            "label": "Test",
            "url": "https://abcdefghijklmnopqrst.supabase.co",
            "publishable_key": "sb_publishable_test-only",
            "workspace_id": "d1273570-3838-4900-96b3-c8ff8f9d9471",
            "email": "test@example.invalid",
            "password": "synthetic-password",
        },
    )
    assert r.status_code == 409
    assert "call" in r.text.lower()


def test_managed_ingress_checks_current_connection(workspace, app):
    connection = app.state.service.managed
    connection.config = {"tenant_id": workspace["tenant_id"], "workspace_id": "test"}
    r = workspace["client"].post(
        "/v1/admin/ingress-check",
        headers=workspace["headers"],
        json={"request_id": "d1273570-3838-4900-96b3-c8ff8f9d9471", "expected_version": 0},
    )
    assert r.status_code == 200
    assert r.json()["connected"] is False
    assert r.json()["routing"] == "unavailable"


def test_direct_speech_cost_estimates_both_tracks(monkeypatch):
    from decimal import Decimal
    from types import SimpleNamespace

    from app import billing

    monkeypatch.setattr(billing, "account_rate", lambda *args: Decimal("0.01"))
    call = SimpleNamespace(
        data={
            "submitted_at": "2026-10-29T10:00:00+00:00",
            "ended_at": "2026-10-29T10:01:00+00:00",
            "agent_provider_id": "agent",
            "transcription_layout": "direct_tracks",
            "connected_seconds": 60,
        }
    )
    cost, _ = billing.call_estimate(None, None, None, call, 0)
    assert cost == Decimal("0.04")


async def test_delivery_keeps_application_in_the_ordered_webhook_worker():
    from app.managed_webhooks import deliver

    class Service:
        async def accept_webhook(self, *args, **kwargs):
            return {"receipt_id": "durable", "status": "received"}

        async def process_webhook(self, *args):
            raise AssertionError("Relay must not race the ordered worker")

    row = {
        "account_id": "account",
        "event_id": "event",
        "hash": "hash",
        "event": {},
        "received_at": "2026-10-29T01:00:00+00:00",
    }
    assert await deliver(Service(), "tenant", [row]) == [{"account_id": "account", "event_id": "event"}]


async def test_restored_session_initializes_refresh_and_realtime(app, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import supabase

    auth = SimpleNamespace(
        initialize_from_storage=AsyncMock(),
        get_session=AsyncMock(return_value=SimpleNamespace(access_token="synthetic-session")),
    )
    realtime = SimpleNamespace(set_auth=AsyncMock())
    client = SimpleNamespace(auth=auth, realtime=realtime)
    monkeypatch.setattr(supabase, "acreate_client", AsyncMock(return_value=client))
    await app.state.service.managed.make_client(
        {"url": "https://abcdefghijklmnopqrst.supabase.co", "publishable_key": "sb_publishable_test"}
    )
    auth.initialize_from_storage.assert_awaited_once()
    realtime.set_auth.assert_awaited_once_with("synthetic-session")


def test_managed_setup_cannot_switch_during_an_inbound_conversation(workspace, app):
    from app.db import put
    from app.managed_webhooks import require_idle
    from fastapi import HTTPException

    service = app.state.service
    with service.db.transaction() as s:
        put(
            s,
            "followup_tasks",
            workspace["tenant_id"],
            workspace["seller_id"],
            {"status": "open", "inbound_state": "connected"},
        )
    with pytest.raises(HTTPException, match="incoming call"):
        require_idle(service, workspace)


def test_first_import_blocks_dialing_before_profile_is_saved(workspace, app):
    from conftest import reserve

    reservation = reserve(workspace).json()
    app.state.service.managed.configuring = True
    r = workspace["client"].post(
        "/v1/calls/start",
        headers=workspace["headers"],
        json={"reservation_id": reservation["reservation_id"], "idempotency_key": "setup-in-flight"},
    )
    assert r.status_code == 409
    assert "setup" in r.text.lower()
