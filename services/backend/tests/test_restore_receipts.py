"""A54: a reviewed restore still needs the exact original carrier receipt."""

import asyncio

import pytest
from app.db import get, update
from conftest import start
from fastapi import HTTPException
from test_control_completion import principal


def held_call(workspace, app):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        update(get(s, "sellers", p["seller_id"]), restore_hold=True)
    return service, p, call


def held(service, p, call):
    with service.db.transaction(False) as s:
        assert get(s, "sellers", p["seller_id"]).data["restore_hold"] is True
        assert get(s, "call_attempts", call["id"]).data["state"] == call["state"]


@pytest.mark.parametrize(
    "receipt",
    [
        {"call_control_id": "unrelated", "is_alive": False},
        {"is_alive": False},
        {"call_control_id": "original", "is_alive": "false", "state": "ended"},
        {"call_control_id": "original", "is_alive": True, "state": "ended"},
    ],
)
def test_restore_rejects_unrelated_or_untyped_terminal_evidence(
    workspace, app, carrier, monkeypatch, receipt
):
    service, p, call = held_call(workspace, app)
    value = dict(receipt)
    if value.get("call_control_id") == "original":
        value["call_control_id"] = call["provider_id"]

    async def reconcile(*args):
        return value

    monkeypatch.setattr(carrier, "reconcile", reconcile)
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.reconcile_restore(p, "Reviewed original carrier statement"))
    assert error.value.status_code == 409
    held(service, p, call)


def test_restore_cannot_replace_the_saved_carrier_reference(workspace, app, carrier, monkeypatch):
    service, p, call = held_call(workspace, app)
    requested = []

    async def reconcile(call_id, provider_id):
        requested.append(provider_id)
        return {"call_control_id": provider_id, "is_alive": False}

    monkeypatch.setattr(carrier, "reconcile", reconcile)
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.reconcile_restore(p, "Reviewed reference", {call["id"]: "different-leg"}))
    assert error.value.status_code == 409
    assert "different-leg" not in requested
    held(service, p, call)


def test_new_journal_entry_during_restore_keeps_outbound_held(workspace, app, carrier, monkeypatch):
    service, p, call = held_call(workspace, app)

    async def reconcile(*args):
        with service.db.transaction() as s:
            service.journal_effect_in(s, p, "dial", "new-pending-call", "submitted")
        return {"call_control_id": call["provider_id"], "is_alive": False}

    monkeypatch.setattr(carrier, "reconcile", reconcile)
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.reconcile_restore(p, "Reviewed old journal"))
    assert error.value.status_code == 409
    with service.db.transaction(False) as s:
        assert get(s, "sellers", p["seller_id"]).data["restore_hold"] is True


def test_restore_terminal_receipt_is_fenced_against_changed_call_identity(
    workspace, app, carrier, monkeypatch
):
    service, p, call = held_call(workspace, app)

    async def reconcile(*args):
        with service.db.transaction() as s:
            update(get(s, "call_attempts", call["id"]), provider_id="newer-leg")
        return {"call_control_id": call["provider_id"], "is_alive": False}

    monkeypatch.setattr(carrier, "reconcile", reconcile)
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.reconcile_restore(p, "Reviewed original leg"))
    assert error.value.status_code == 409
    held(service, p, call)


@pytest.mark.asyncio
async def test_verified_restore_reconciles_original_leg_off_the_live_loop(
    workspace, app, carrier, monkeypatch
):
    from test_background_event_loop import async_sql

    service, p, call = held_call(workspace, app)

    async def reconcile(call_id, provider_id):
        assert (call_id, provider_id) == (call["id"], call["provider_id"])
        return {"call_control_id": provider_id, "is_alive": False}

    monkeypatch.setattr(carrier, "reconcile", reconcile)
    with async_sql(app) as queries:
        result = await service.reconcile_restore(p, "Checked original carrier record")
    assert result["reconciled"]
    assert not queries, f"Restore ran {len(queries)} SQL statements on the live loop"
    with service.db.transaction(False) as s:
        assert get(s, "sellers", p["seller_id"]).data["restore_hold"] is False
        assert get(s, "call_attempts", call["id"]).data["state"] == "ENDED"


@pytest.mark.parametrize("change", ["disabled", "role", "seller"])
def test_restore_rechecks_operator_scope_before_releasing_hold(workspace, app, carrier, monkeypatch, change):
    service, p, call = held_call(workspace, app)

    async def reconcile(*args):
        with service.db.transaction() as s:
            user = get(s, "users", p["user_id"])
            if change == "seller":
                user.seller_id = "another-seller"
            else:
                update(user, **({"disabled": True} if change == "disabled" else {"role": "rep"}))
        return {"call_control_id": call["provider_id"], "is_alive": False}

    monkeypatch.setattr(carrier, "reconcile", reconcile)
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.reconcile_restore(p, "Reviewed carrier record"))
    assert error.value.status_code in {403, 404, 409}
    with service.db.transaction(False) as s:
        assert get(s, "sellers", p["seller_id"]).data["restore_hold"] is True
