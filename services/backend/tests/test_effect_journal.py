"""E11/C045: losing the main store cannot erase a submitted external operation."""

import pytest
from conftest import reserve, start
from test_remediation_privacy import principal


@pytest.mark.parametrize("failure", ["none", "fsync", "partial", "disk_full"])
def test_lost_store_and_flush_failure(workspace, app, carrier, monkeypatch, failure):
    service, p = app.state.service, principal(workspace)
    writes = []
    original = service.vault.append_journal

    def append(name, record):
        if name == "effects":
            writes.append(record)
            if failure != "none":
                raise OSError(f"Synthetic {failure} before durable effect intent")
        return original(name, record)

    monkeypatch.setattr(service.vault, "append_journal", append)
    packet = reserve(workspace)
    assert packet.status_code == 200
    response = workspace["client"].post(
        "/v1/calls/start",
        headers=workspace["headers"],
        json={"reservation_id": packet.json()["reservation_id"], "idempotency_key": "journaled-dial"},
    )
    if failure != "none":
        assert response.status_code in {409, 503}, response.text
        assert not carrier.dials
        assert writes, "A dial must attempt the independent journal before the network"
        return
    assert response.status_code == 200, response.text
    records = service.vault.read_journal("effects")
    assert records, "Current database outbox rows alone cannot recover a lost store"
    assert {r["phase"] for r in records} >= {"intent", "submitted", "receipt"}
    assert all(r["tenant_id"] == p["tenant_id"] and r["seller_id"] == p["seller_id"] for r in records)
    assert len({r["operation_id"] for r in records}) == 1
    assert response.json()["provider_id"] in str(records)
    assert "journaled-dial" in str(records)
    assert not any(key in str(records) for key in ("transcript", "api_key", "access_token"))


def test_journal_failure_cannot_block_stopping_an_existing_leg(workspace, app, carrier, monkeypatch):
    call = start(workspace)

    def fail(*args, **kwargs):
        raise OSError("Synthetic disk full")

    monkeypatch.setattr(app.state.service.vault, "append_journal", fail)
    response = workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    assert response.status_code == 200, response.text
    assert any(row["provider_id"] == call["provider_id"] for row in carrier.hangups)


def test_lost_newer_database_rows_recover_from_effect_journal(workspace, app, carrier):
    """An older allowed-data backup plus independent intent receipts keeps liabilities."""
    from app.db import get

    service, p = app.state.service, principal(workspace)
    backup = service.backup(p)
    call = start(workspace)
    # This is only the guarded disposable authority. Simulate loss of rows newer
    # than the backup while retaining its independent protected journal.
    with service.db.transaction() as s:
        for table in ("call_attempts", "command_outbox", "usage_ledger"):
            for row in service.scoped(s, table, p):
                if (
                    row.id == call["id"]
                    or row.data.get("call_id") == call["id"]
                    or row.data.get("object_id") == call["id"]
                ):
                    s.delete(row)
    service.restore(p, backup["backup_id"])
    with service.db.transaction(False) as s:
        recovered = get(s, "call_attempts", call["id"])
        assert recovered is not None
        assert recovered.data["provider_id"] == call["provider_id"]
        commands = [
            r.data for r in service.scoped(s, "command_outbox", p) if r.data.get("call_id") == call["id"]
        ]
        assert commands and all(r.get("status") != "pending" for r in commands if r.get("kind") == "dial")
        assert any(r.data.get("object_id") == call["id"] for r in service.scoped(s, "usage_ledger", p))
    assert len(carrier.dials) == 1, "Disaster recovery cannot originate a replacement call"


@pytest.mark.parametrize("damage", ["truncated", "wrong_key", "sequence_gap"])
def test_damaged_effect_journal_holds_new_effects(workspace, app, monkeypatch, carrier, damage):
    from app.main import create_app
    from fastapi.testclient import TestClient

    service = app.state.service
    original = type(service.vault).read_journal

    def read(vault, name):
        if name == "effects":
            raise ValueError(f"Synthetic {damage} in authenticated effect journal")
        return original(vault, name)

    monkeypatch.setattr(type(service.vault), "read_journal", read)
    restarted = create_app(service.settings, clock=service.clock, carrier=carrier)
    with TestClient(restarted) as client:
        resumed = {**workspace, "client": client}
        packet = reserve(resumed)
        if packet.status_code == 200:
            response = client.post(
                "/v1/calls/start",
                headers=workspace["headers"],
                json={
                    "reservation_id": packet.json()["reservation_id"],
                    "idempotency_key": "damaged-journal",
                },
            )
            assert response.status_code in {409, 503}
        else:
            assert packet.status_code in {409, 503}
    assert not carrier.dials
