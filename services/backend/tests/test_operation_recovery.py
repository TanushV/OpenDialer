"""Recover the original non-call effect without feeding it to call-control retries."""

import asyncio
from datetime import timedelta

import pytest
from app.db import doc, get, put, update
from fastapi import HTTPException
from sqlalchemy import inspect, text
from test_number_reconciliation import configure_fixture
from test_recovery_actions import act, uncertain


def configuration(workspace, app, *, status="uncertain"):
    service, p, number_id = configure_fixture(workspace, app)
    intent, _ = service._claim_number_configuration(p, number_id, "desired")
    with service.db.transaction() as s:
        row = get(s, "command_outbox", intent["id"])
        update(row, status=status, lease_until=None, lease_owner=None)
        number = get(s, "number_inventory", number_id)
        return (
            service,
            p,
            doc(row),
            {
                "id": "owned",
                "phone_number": number.data["phone"],
                "connection_id": "desired",
                "status": "active",
                "call_recording_enabled": False,
            },
        )


def original_get(monkeypatch, service, carrier, receipt):
    seen = []

    async def retrieve(identity):
        seen.append(identity)
        return {"data": receipt}

    async def forbidden(*_):
        pytest.fail("Configuration recovery must never submit a call, purchase or configuration")

    monkeypatch.setattr(service, "carrier", lambda _: carrier)
    monkeypatch.setattr(carrier, "get_number", retrieve)
    monkeypatch.setattr(carrier, "configure_number", forbidden)
    monkeypatch.setattr(carrier, "reconcile", forbidden)
    return seen


def test_noncall_outbox_items_do_not_crash_recovery_or_expose_call_actions(workspace, app):
    service, p, item, _ = configuration(workspace, app)
    with service.db.transaction() as s:
        for kind in ("script_batch", "purchase", "future_unknown"):
            put(
                s,
                "command_outbox",
                p["tenant_id"],
                p["seller_id"],
                {"kind": kind, "status": "uncertain", "contexts": {"private": "not returned"}},
                key=kind + ":fixture",
            )
        put(
            s,
            "command_outbox",
            p["tenant_id"],
            "other-seller",
            {"kind": "number_configure", "status": "uncertain"},
            key="other-seller",
        )
    response = workspace["client"].get("/v1/admin/recovery", headers=workspace["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["commands"] == []
    operations = response.json()["operations"]
    assert len(operations) == 4
    selected = next(value for value in operations if value["id"] == item["id"])
    assert selected["connection_id"] == "desired" and selected["can_reconcile"]
    assert not selected.get("can_end") and not selected.get("can_cancel")
    assert "private" not in response.text and "contexts" not in response.text


@pytest.mark.parametrize("kind", ["script_batch", "purchase", "future_unknown"])
@pytest.mark.parametrize("action", ["end_call", "cancel_unsent", "reconcile"])
def test_call_recovery_refuses_noncall_effects(workspace, app, kind, action):
    service, p, _, _ = configuration(workspace, app)
    with service.db.transaction() as s:
        item = doc(
            put(
                s,
                "command_outbox",
                p["tenant_id"],
                p["seller_id"],
                {"kind": kind, "status": "pending"},
                key=kind,
            )
        )
    result = act(workspace, item, action)
    assert result.status_code in {409, 422}, result.text
    with service.db.transaction(False) as s:
        assert get(s, "command_outbox", item["id"]).data["status"] == "pending"


def test_configuration_reconciles_original_route_after_settings_change(workspace, app, carrier, monkeypatch):
    service, p, item, receipt = configuration(workspace, app)
    seen = original_get(monkeypatch, service, carrier, receipt)
    service.vault.write(p["tenant_id"], {"telnyx_connection_id": "next-route"})
    result = act(workspace, item, "reconcile")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed" and seen == ["owned"]
    with service.db.transaction(False) as s:
        number = get(s, "number_inventory", item["number_id"])
        assert number.data["connection_id"] == "desired"
        assert not number.data["identity_configured"] and not number.data["callback_tested"]
        assert number.data["configuration_status"] == "route_changed"
        assert get(s, "command_outbox", item["id"]).data["connection_id"] == "desired"


def test_configuration_recovery_confirms_identity_but_never_a_callback(workspace, app, carrier, monkeypatch):
    service, p, item, receipt = configuration(workspace, app)
    seen = original_get(monkeypatch, service, carrier, receipt)
    result = act(workspace, item, "reconcile")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed" and seen == ["owned"]
    with service.db.transaction(False) as s:
        number = get(s, "number_inventory", item["number_id"])
        assert number.data["identity_configured"]
        assert not number.data["callback_tested"]
        assert number.data["callback_retained"]


@pytest.mark.parametrize(
    "change",
    [
        {"id": "another"},
        {"phone_number": "+14155559999"},
        {"connection_id": "wrong-route"},
        {"status": "port-out-pending"},
        {"call_recording_enabled": True},
        {"phone_number": None},
        {"call_recording_enabled": "false"},
    ],
)
def test_unconfirmed_or_unsafe_number_receipt_stays_reviewable(workspace, app, carrier, monkeypatch, change):
    service, p, item, receipt = configuration(workspace, app)
    service.settings.simulation = False
    seen = original_get(monkeypatch, service, carrier, {**receipt, **change})
    result = act(workspace, item, "reconcile")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "review_required" and seen == ["owned"]
    with service.db.transaction(False) as s:
        number = get(s, "number_inventory", item["number_id"])
        assert not number.data["identity_configured"] and not number.data["callback_tested"]
        current = get(s, "command_outbox", item["id"])
        assert current.data.get("lease_owner") is None
        assert current.data.get("recovery_reason")


@pytest.mark.parametrize("action", ["end_call", "cancel_unsent"])
def test_number_configuration_cannot_be_treated_as_a_call(workspace, app, action):
    _, _, item, _ = configuration(workspace, app)
    assert act(workspace, item, action).status_code == 409


def test_configuration_recovery_rejects_changed_version_and_reference(workspace, app, carrier, monkeypatch):
    service, p, item, receipt = configuration(workspace, app)
    seen = original_get(monkeypatch, service, carrier, receipt)
    assert act(workspace, item, "reconcile", provider_id="different-number").status_code == 409
    with service.db.transaction() as s:
        update(get(s, "command_outbox", item["id"]), recovery_reason="new evidence")
    assert act(workspace, item, "reconcile").status_code == 409
    assert not seen


@pytest.mark.asyncio
async def test_number_reconciliation_blocks_competing_configuration(workspace, app, carrier, monkeypatch):
    service, p, item, receipt = configuration(workspace, app)
    entered, release = asyncio.Event(), asyncio.Event()

    async def retrieve(_):
        entered.set()
        await release.wait()
        return receipt

    monkeypatch.setattr(carrier, "get_number", retrieve)
    task = asyncio.create_task(service.recovery_action(p, item["id"], item["version"], "reconcile"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(HTTPException) as busy:
            await service.configure_owned_number(p, item["number_id"])
        assert busy.value.status_code == 409
    finally:
        release.set()
        result = await task
    assert result["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["inventory", "owner", "lease", "role", "action"])
async def test_number_reconciliation_fences_late_receipts(
    workspace, app, carrier, monkeypatch, clock, change
):
    service, p, item, receipt = configuration(workspace, app)

    async def retrieve(_):
        with service.db.transaction() as s:
            if change == "inventory":
                update(get(s, "number_inventory", item["number_id"]), provider_id="replacement")
            elif change == "owner":
                update(get(s, "command_outbox", item["id"]), lease_owner="new-worker")
            elif change == "role":
                update(get(s, "users", p["user_id"]), role="rep")
            elif change == "action":
                update(get(s, "command_outbox", item["id"]), status="cancelled")
            else:
                clock.now += timedelta(seconds=61)
        return receipt

    monkeypatch.setattr(carrier, "get_number", retrieve)
    with pytest.raises(HTTPException) as rejected:
        await service.recovery_action(p, item["id"], item["version"], "reconcile")
    assert rejected.value.status_code in {403, 409}
    with service.db.transaction(False) as s:
        assert not get(s, "number_inventory", item["number_id"]).data["identity_configured"]
        assert get(s, "command_outbox", item["id"]).data["status"] != "completed"


def test_recovery_maintenance_isolates_operations_from_call_reconciliation(
    workspace, app, carrier, monkeypatch
):
    call, dial = uncertain(workspace)
    service, p, item, receipt = configuration(workspace, app)
    with service.db.transaction() as s:
        put(
            s,
            "command_outbox",
            p["tenant_id"],
            p["seller_id"],
            {"kind": "script_batch", "status": "uncertain"},
            key="script-batch:lost",
        )
    seen = []

    async def unavailable(_):
        seen.append("owned")
        raise TimeoutError("a transient configuration lookup failure")

    monkeypatch.setattr(carrier, "get_number", unavailable)
    result = workspace["client"].post("/v1/admin/recovery", headers=workspace["headers"])
    assert result.status_code == 200, result.text
    with service.db.transaction(False) as s:
        assert get(s, "command_outbox", dial["id"]).data["status"] == "completed"
        assert get(s, "command_outbox", item["id"]).data["status"] == "review_required"
    assert seen == ["owned"] and len(carrier.dials) == 1
    assert workspace["client"].post("/v1/admin/recovery", headers=workspace["headers"]).status_code == 200
    assert seen == ["owned"], "Maintenance respects the original operation's next-check time"


def test_noncall_history_cannot_starve_call_recovery(workspace, app, carrier):
    service, p, _, _ = configuration(workspace, app)
    with service.db.transaction() as s:
        for index in range(110):
            put(
                s,
                "command_outbox",
                p["tenant_id"],
                p["seller_id"],
                {"kind": "script_batch", "status": "uncertain"},
                key=f"script-batch:{index}",
            )
    _, item = uncertain(workspace)
    result = workspace["client"].post("/v1/admin/recovery", headers=workspace["headers"])
    assert result.status_code == 200, result.text
    with service.db.transaction(False) as s:
        assert get(s, "command_outbox", item["id"]).data["status"] == "completed"


def test_recovery_operation_never_crosses_seller(workspace, app):
    _, _, item, _ = configuration(workspace, app)
    with app.state.db.transaction() as s:
        get(s, "command_outbox", item["id"]).seller_id = "other-seller"
    assert act(workspace, item, "reconcile").status_code == 404


def test_operation_kind_is_indexed_and_old_rows_backfill_without_identity_changes(workspace, app):
    service, p, item, _ = configuration(workspace, app)
    inspector = inspect(service.db.engine)
    columns = {column["name"] for column in inspector.get_columns("command_outbox")}
    assert "op_kind" in columns
    assert any(
        index["column_names"][:4] == ["tenant_id", "seller_id", "op_kind", "op_status"]
        for index in inspector.get_indexes("command_outbox")
    )
    with service.db.engine.begin() as connection:
        connection.execute(text("UPDATE command_outbox SET op_kind=NULL, op_index_version=1"))
    service.db.backfill_operational_columns()
    with service.db.transaction(False) as s:
        row = get(s, "command_outbox", item["id"])
        assert row.op_kind == "number_configure" and row.op_index_version == 2
        assert row.version == item["version"] and row.data == {
            k: v
            for k, v in item.items()
            if k not in {"id", "version", "tenant_id", "seller_id", "created_at"}
        }


@pytest.mark.parametrize("status", ["cancelled", "failed", "pending"])
def test_configuration_recovery_does_not_reopen_terminal_or_unsent_work(
    workspace, app, carrier, monkeypatch, status
):
    service, _, item, receipt = configuration(workspace, app, status=status)
    seen = original_get(monkeypatch, service, carrier, receipt)
    assert act(workspace, item, "reconcile").status_code == 409
    assert not seen


@pytest.mark.asyncio
async def test_interrupted_number_check_resumes_after_lease_without_resending(
    workspace, app, carrier, monkeypatch, clock
):
    service, p, item, receipt = configuration(workspace, app)
    entered = asyncio.Event()

    async def interrupted(_):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(carrier, "get_number", interrupted)
    task = asyncio.create_task(service.recovery_action(p, item["id"], item["version"], "reconcile"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with service.db.transaction(False) as s:
        saved = doc(get(s, "command_outbox", item["id"]))
    clock.now += timedelta(seconds=31)
    seen = original_get(monkeypatch, service, carrier, receipt)
    result = await service.recovery_action(p, saved["id"], saved["version"], "reconcile")
    assert result["status"] == "completed" and seen == ["owned"]
