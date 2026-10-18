"""A08/A40/A54: opt-outs survive interrupted writes and stop active work."""

import pytest
from app.db import MODELS, ActiveClaim, get, put, update
from app.main import create_app
from conftest import start
from fastapi import HTTPException
from sqlalchemy import delete, select
from test_context_reads import count_decrypted
from test_control_completion import principal


def test_committed_optout_journal_replays_before_an_authority_restart(workspace, app):
    service, p = app.state.service, principal(workspace)
    phone = "+14155550771"
    service.suppress(p, phone, "opt_out", event_id="durable-optout")
    with service.db.transaction() as s:
        s.execute(delete(MODELS["suppressions"]))  # Simulated loss of newer DB writes, only in tmp authority.
    restarted = create_app(service.settings, clock=service.clock)
    try:
        with restarted.state.db.transaction(False) as s:
            assert restarted.state.service.suppressed(s, p, phone), "Journal replay must precede new calls"
    finally:
        restarted.state.db.engine.dispose()


def test_failed_optout_flush_keeps_outbound_held(workspace, app, monkeypatch):
    import os

    service, p = app.state.service, principal(workspace)
    original = os.fsync
    failed = False

    def interrupted(fd):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("Synthetic opt-out flush failure")
        return original(fd)

    monkeypatch.setattr(os, "fsync", interrupted)
    with pytest.raises(HTTPException) as error:
        service.suppress(p, "+14155550772", "opt_out", event_id="unflushed-optout")
    assert error.value.status_code == 503
    with service.db.transaction(False) as s:
        assert get(s, "sellers", p["seller_id"]).data.get("journal_recovery_required") is True


def test_partial_optout_journal_prevents_silent_restart(workspace, app):
    service = app.state.service
    (service.settings.data_dir / "suppression-journal.enc").write_bytes(b"incomplete-encrypted-receipt")
    with pytest.raises(HTTPException) as error:
        create_app(service.settings, clock=service.clock)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_optout_stops_the_live_call_without_a_second_client_hangup(workspace, app, carrier):
    service, p, call = app.state.service, principal(workspace), start(workspace)
    with service.db.transaction(False) as s:
        phone = get(s, "leads", call["lead_id"]).data["phone"]
    service.suppress(p, phone, "opt_out", event_id="stop-original-call")
    with service.db.transaction(False) as s:
        assert get(s, "call_attempts", call["id"]).data["state"] == "ENDING"
        assert s.scalar(select(ActiveClaim).where(ActiveClaim.call_id == call["id"])) is not None
    await service.dispatch_controls(p)
    assert call["provider_id"] in carrier.hangups


def test_optout_update_does_not_decrypt_unrelated_history(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        for i in range(200):
            put(
                s,
                "suppressions",
                p["tenant_id"],
                p["seller_id"],
                {
                    "phone": f"+1415555{i:04d}",
                    "active": True,
                    "permanent": True,
                },
                key=f"+1415555{i:04d}",
            )
            put(
                s,
                "command_outbox",
                p["tenant_id"],
                p["seller_id"],
                {
                    "kind": "dial",
                    "status": "completed",
                    "call_id": f"historical-{i}",
                },
            )
            put(s, "reservations", p["tenant_id"], p["seller_id"], {"status": "expired"})
    decoded = count_decrypted(app, monkeypatch)
    service.suppress(p, "+14155550999", "opt_out", event_id="bounded-optout")
    assert len(decoded) <= 20, len(decoded)


def test_repeated_optout_identity_is_idempotent_and_cannot_change_endpoint(workspace, app):
    service, p = app.state.service, principal(workspace)
    first = service.suppress(p, "+14155550773", "opt_out", event_id="stable-optout")
    path = service.settings.data_dir / "suppression-journal.enc"
    before = path.read_bytes()
    second = service.suppress(p, "+14155550773", "opt_out", event_id="stable-optout")
    assert first["id"] == second["id"]
    assert path.read_bytes() == before, "Replay cannot grow the journal without bound"
    with pytest.raises(HTTPException) as error:
        service.suppress(p, "+14155550774", "opt_out", event_id="stable-optout")
    assert error.value.status_code == 409


def test_suppression_of_an_unsent_reservation_cannot_release_a_live_call_claim(workspace, app):
    service, p, call = app.state.service, principal(workspace), start(workspace)
    with service.db.transaction() as s:
        phone = get(s, "leads", call["lead_id"]).data["phone"]
        update(get(s, "reservations", call["reservation_id"]), status="reserved")
    service.suppress(p, phone, "opt_out", event_id="retained-live-claim")
    with service.db.transaction(False) as s:
        assert s.scalar(select(ActiveClaim).where(ActiveClaim.call_id == call["id"])) is not None
