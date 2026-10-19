"""A08/A40/A54: opt-outs survive interrupted writes and stop active work."""

import pytest
from app.db import MODELS, ActiveClaim, get, put, update
from app.main import create_app
from conftest import start
from fastapi import HTTPException
from sqlalchemy import delete, select
from test_context_reads import count_decrypted
from test_control_completion import principal


def test_wrapup_denial_and_receipt_cannot_commit_before_suppression(workspace, app, monkeypatch):
    from uuid import uuid4

    service, p, call = app.state.service, principal(workspace), start(workspace)
    c, h = workspace["client"], workspace["headers"]
    ended = c.post(f"/v1/calls/{call['id']}/hangup", headers=h).json()
    body = {"request_id": str(uuid4()), "expected_version": ended["version"], "outcome": "opt_out"}
    original = service.suppress

    def interrupted(*args, **kwargs):
        raise RuntimeError("synthetic crash before denial persistence")

    monkeypatch.setattr(service, "suppress", interrupted)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        service.save_disposition(p, call["id"], body)
    with service.db.transaction(False) as s:
        current = get(s, "call_attempts", call["id"])
        assert current.data.get("outcome") != "opt_out", "Wrap-up cannot commit ahead of its denial"
        assert service._by_key(s, p, "outcomes", "edit:disposition:" + body["request_id"]) is None
    monkeypatch.setattr(service, "suppress", original)
    saved = service.save_disposition(p, call["id"], body)
    assert saved["outcome"] == "opt_out"
    with service.db.transaction(False) as s:
        phone = get(s, "leads", call["lead_id"]).data["phone"]
        assert service.suppressed(s, p, phone)


def test_wrapup_crash_after_denial_journal_recovers_before_new_call(workspace, app, monkeypatch):
    from uuid import uuid4

    service, p, call = app.state.service, principal(workspace), start(workspace)
    c, h = workspace["client"], workspace["headers"]
    ended = c.post(f"/v1/calls/{call['id']}/hangup", headers=h).json()
    body = {"request_id": str(uuid4()), "expected_version": ended["version"], "outcome": "opt_out"}

    def interrupted(*args, **kwargs):
        raise RuntimeError("synthetic crash before wrap-up receipt")

    monkeypatch.setattr(service, "local_edit_receipt", interrupted)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        service.save_disposition(p, call["id"], body)
    restarted = create_app(service.settings, clock=service.clock)
    try:
        with restarted.state.db.transaction(False) as s:
            phone = get(s, "leads", call["lead_id"]).data["phone"]
            assert restarted.state.service.suppressed(s, p, phone), (
                "Durable denial survives incomplete wrap-up"
            )
    finally:
        restarted.state.db.engine.dispose()


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


@pytest.mark.asyncio
async def test_restart_denial_blocks_preexisting_reservation_and_future_selection(workspace, app):
    from conftest import ready

    service, p = app.state.service, principal(workspace)
    ready(workspace)
    reservation = service.reserve(p, workspace["campaign_id"])
    phone = reservation["lead"]["phone"]
    # Crash after flushing the independent denial but before the business commit.
    with pytest.raises(RuntimeError, match="interrupted commit"):
        with service.db.transaction() as session:
            service.suppress(p, phone, "opt_out", event_id="reserved-denial", session=session)
            raise RuntimeError("interrupted commit")
    restarted = create_app(service.settings, clock=service.clock, carrier=service.simulated_carrier)
    target = restarted.state.service
    try:
        with pytest.raises(HTTPException):
            await target.start_call(
                p,
                {"reservation_id": reservation["reservation_id"], "idempotency_key": "after-restart-denial"},
            )
        with target.db.transaction() as session:
            for lead in target.scoped(session, "leads", p):
                if lead.data["phone"] != phone:
                    update(lead, status="needs_review")
            assert target.suppressed(session, p, phone)
        with pytest.raises(HTTPException):
            target.reserve(p, workspace["campaign_id"])
        assert target.simulated_carrier.dials == []
    finally:
        target.db.engine.dispose()


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
    assert call["provider_id"] in [r["provider_id"] for r in carrier.hangups]


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


def test_nested_optout_failure_preserves_a_hold_after_the_outer_transaction_rolls_back(
    workspace, app, monkeypatch
):
    import os

    service, p = app.state.service, principal(workspace)
    original = os.fsync
    failed = False

    def interrupted(fd):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("Synthetic nested flush failure")
        return original(fd)

    monkeypatch.setattr(os, "fsync", interrupted)
    with pytest.raises(HTTPException) as error:
        with service.db.transaction() as session:
            service.suppress(p, "+14155550775", "opt_out", event_id="nested-optout", session=session)
    assert error.value.status_code == 503
    with service.db.transaction(False) as s:
        assert get(s, "sellers", p["seller_id"]).data.get("journal_recovery_required") is True


@pytest.mark.asyncio
async def test_restore_release_requires_original_missing_suppression_receipt(workspace, app, monkeypatch):
    import os

    service, p = app.state.service, principal(workspace)
    original = os.fsync
    failed = False

    def interrupted(fd):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("Synthetic missing durable receipt")
        return original(fd)

    monkeypatch.setattr(os, "fsync", interrupted)
    try:
        service.suppress(p, "+14155550776", "opt_out", event_id="missing-optout")
    except (OSError, HTTPException):
        pass
    path = service.settings.data_dir / "suppression-journal.enc"
    path.unlink()
    with pytest.raises(HTTPException) as error:
        await service.reconcile_restore(p, "Synthetic review cannot replace missing denial bytes")
    assert error.value.status_code == 409


def test_concurrent_repeated_optout_writes_one_receipt(workspace, app):
    from concurrent.futures import ThreadPoolExecutor

    service, p = app.state.service, principal(workspace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(
            pool.map(
                lambda _: service.suppress(p, "+14155550779", "opt_out", event_id="concurrent-optout"),
                range(2),
            )
        )
    assert values[0]["id"] == values[1]["id"]
    assert len((service.settings.data_dir / "suppression-journal.enc").read_bytes().splitlines()) == 1


@pytest.mark.asyncio
async def test_failed_suppression_receipt_can_be_repaired_and_reviewed(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    original = service.vault.append_journal

    def unavailable(*args):
        raise OSError("Synthetic unavailable journal")

    monkeypatch.setattr(service.vault, "append_journal", unavailable)
    with pytest.raises(HTTPException) as error:
        service.suppress(p, "+14155550780", "opt_out", event_id="repair-optout")
    assert error.value.status_code == 503
    monkeypatch.setattr(service.vault, "append_journal", original)
    with pytest.raises(HTTPException):
        service.suppress(p, "+14155550781", "opt_out", event_id="repair-optout")
    service.suppress(p, "+14155550780", "opt_out", event_id="repair-optout")
    await service.reconcile_restore(p, "Original opt-out retried after repairing journal storage")
    with service.db.transaction(False) as s:
        assert service.suppressed(s, p, "+14155550780")
        assert not get(s, "sellers", p["seller_id"]).data["journal_recovery_required"]


def test_replaying_a_detached_optout_cannot_stop_or_release_the_reps_new_call(workspace, app):
    service, p, old = app.state.service, principal(workspace), start(workspace)
    with service.db.transaction() as s:
        phone = get(s, "leads", old["lead_id"]).data["phone"]
        update(get(s, "call_attempts", old["id"]), state="VM_PLAYING", detached=True)
        s.execute(delete(ActiveClaim).where(ActiveClaim.call_id == old["id"]))
    new = start(workspace, key="new-prospect-after-detach")
    service.suppress(p, phone, "opt_out", event_id="detached-optout")
    service.suppress(p, phone, "opt_out", event_id="detached-optout")
    with service.db.transaction(False) as s:
        assert get(s, "call_attempts", old["id"]).data["state"] == "ENDING"
        assert get(s, "call_attempts", new["id"]).data["state"] == new["state"]
        assert s.scalar(select(ActiveClaim).where(ActiveClaim.call_id == new["id"])) is not None


def test_failure_after_nested_optout_returns_still_preserves_the_denial_hold(workspace, app):
    service, p = app.state.service, principal(workspace)
    with pytest.raises(RuntimeError, match="Later event processing failed"):
        with service.db.transaction() as s:
            service.suppress(p, "+14155550782", "opt_out", event_id="outer-rollback", session=s)
            raise RuntimeError("Later event processing failed")
    with service.db.transaction(False) as s:
        assert get(s, "sellers", p["seller_id"]).data.get("journal_recovery_required") is True
    assert (p["tenant_id"], p["seller_id"]) in service.__dict__.get("_journal_faults", set())


def test_repaired_missing_suppression_is_held_if_the_enclosing_transaction_fails(workspace, app):
    service, p = app.state.service, principal(workspace)
    phone = "+14155550783"
    service.suppress(p, phone, "opt_out", event_id="replayed-rollback")
    with service.db.transaction() as s:
        s.execute(delete(MODELS["suppressions"]))
    with pytest.raises(RuntimeError, match="Later event processing failed"):
        with service.db.transaction() as s:
            service.suppress(p, phone, "opt_out", event_id="replayed-rollback", session=s)
            raise RuntimeError("Later event processing failed")
    with service.db.transaction(False) as s:
        assert get(s, "sellers", p["seller_id"]).data.get("journal_recovery_required") is True


def test_temporary_hold_does_not_downgrade_a_retained_permanent_optout(workspace, app):
    service, p = app.state.service, principal(workspace)
    phone = "+14155550784"
    original = service.suppress(p, phone, "opt_out", event_id="permanent-denial")
    with service.db.transaction() as s:
        update(get(s, "suppressions", original["id"]), active=False)
    service.suppress(p, phone, "temporary_hold", event_id="later-temporary-hold")
    with service.db.transaction(False) as s:
        row = get(s, "suppressions", original["id"])
        assert row.data["active"] is True
        assert row.data["permanent"] is True
        assert row.data["reason"] == "opt_out"


def test_partial_journal_cannot_be_extended_by_an_optout_retry(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    path = service.settings.data_dir / "suppression-journal.enc"
    original = service.vault.append_journal

    def partial(*args):
        path.write_bytes(b"synthetic-partial-receipt")
        raise OSError("Synthetic partial append")

    monkeypatch.setattr(service.vault, "append_journal", partial)
    with pytest.raises(HTTPException):
        service.suppress(p, "+14155550785", "opt_out", event_id="partial-retry")
    before = path.read_bytes()
    monkeypatch.setattr(service.vault, "append_journal", original)
    with pytest.raises(HTTPException) as error:
        service.suppress(p, "+14155550785", "opt_out", event_id="partial-retry")
    assert error.value.status_code in {409, 503}
    assert path.read_bytes() == before, "Recover the damaged receipt before appending new bytes"
