"""E11/C045: losing the main store cannot erase a submitted external operation."""

import pytest
from conftest import reserve, start
from test_batch_recovery import batch  # noqa: F401 - pytest fixture
from test_dnc_recovery import screening  # noqa: F401 - pytest fixture
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


@pytest.mark.parametrize("kind", ["live_coach", "live_coach_review", "dnc_screen", "script_batch"])
def test_optional_admission_retains_independent_liability(workspace, app, kind):
    service, p = app.state.service, principal(workspace)
    assert service.reserve_optional_budget(p, "request-" + kind, 0.01, object_id="object", work_kind=kind)
    records = service.vault.read_journal("effects")
    assert any(r["kind"] == kind and r["phase"] == "intent" for r in records)
    assert any(r["phase"] == "submitted" for r in records)
    # Admission is a conservative submission boundary, not proof the provider was reached.
    assert all(r.get("network_confirmed") is not True for r in records)
    service.release_unsubmitted_budget(p, "request-" + kind)
    assert service.vault.read_journal("effects")[-1]["phase"] == "receipt"


def test_failed_optional_journal_prevents_admission(workspace, app, monkeypatch):
    from fastapi import HTTPException

    service, p = app.state.service, principal(workspace)
    monkeypatch.setattr(service.vault, "append_journal", lambda *_: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(HTTPException) as error:
        service.reserve_optional_budget(p, "new-request", 0.01, object_id="object", work_kind="live_coach")
    assert error.value.status_code == 503
    assert "EFFECT_JOURNAL_RECOVERY_REQUIRED" in service.preflight(p)["blockers"]


def test_backup_contains_verified_journal_checkpoint(workspace, app):
    import json

    service, p = app.state.service, principal(workspace)
    start(workspace)
    backup = service.backup(p)
    path = service.settings.data_dir / "backups" / (backup["backup_id"] + ".backup")
    payload = json.loads(service.vault.cipher.decrypt(path.read_bytes()))
    checkpoints = payload.get("journal_checkpoints", {})
    assert checkpoints.get("effects", {}).get("sequence", 0) >= 3
    assert checkpoints["effects"] == service.vault.journal_checkpoint("effects")


def test_lost_receipt_restores_settled_cost_without_double_charge(workspace, app):
    from app.db import get
    from app.providers.inference import InferenceResult

    service, p = app.state.service, principal(workspace)
    backup = service.backup(p)
    service.reserve_optional_budget(p, "exact-receipt", 0.01, object_id="object", work_kind="live_coach")
    identity = service.usage(
        p,
        "openrouter",
        "live_coach",
        InferenceResult({}, {"cost": 0.002}, "remote"),
        object_id="object",
        request_key="exact-receipt",
    )
    with service.db.transaction() as s:
        s.delete(get(s, "usage_ledger", identity))
    service.restore(p, backup["backup_id"])
    with service.db.transaction(False) as s:
        restored = get(s, "usage_ledger", identity)
        assert restored and restored.data["cost_usd"] == 0.002
        assert restored.data["status"] == "settled" and restored.data["reserved_usd"] == 0


@pytest.mark.asyncio
async def test_control_receipts_never_persist_text_or_digits(workspace, app):
    from app.db import get

    service, p, call = app.state.service, principal(workspace), start(workspace)
    with service.db.transaction() as s:
        command = service.control_intent(s, p, call["id"], "speak", {"text": "PRIVATE_SPEECH_CANARY"})
        identity = command.id
    await service.dispatch_controls(p, command_id=identity)
    records = service.vault.read_journal("effects")
    matched = [r for r in records if r["kind"] == "speak"]
    assert {r["phase"] for r in matched} >= {"intent", "submitted", "receipt"}
    assert "PRIVATE_SPEECH_CANARY" not in str(records)
    with service.db.transaction(False) as s:
        assert get(s, "command_outbox", identity).data["status"] == "completed"


@pytest.mark.asyncio
async def test_other_leg_control_never_replaces_recovered_prospect_identity(workspace, app):
    from app.db import get

    service, p = app.state.service, principal(workspace)
    backup = service.backup(p)
    call = start(workspace)
    with service.db.transaction() as s:
        action = service.control_intent(s, p, call["id"], "answer", {}, target="distinct-agent-leg")
    await service.dispatch_controls(p, command_id=action.id)
    with service.db.transaction() as s:
        s.delete(get(s, "call_attempts", call["id"]))
    service.restore(p, backup["backup_id"])
    with service.db.transaction(False) as s:
        assert get(s, "call_attempts", call["id"]).data["provider_id"] == call["provider_id"]
    journal, _ = service._restore_journal(p)
    assert journal[call["id"]]["provider_id"] == call["provider_id"]


def test_repaired_journal_can_release_its_explicit_hold(workspace, app, monkeypatch):
    from fastapi import HTTPException

    service, p = app.state.service, principal(workspace)
    original = service.vault.append_journal
    monkeypatch.setattr(service.vault, "append_journal", lambda *_: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(HTTPException):
        service.reserve_optional_budget(p, "interrupted", 0.01, object_id="object", work_kind="live_coach")
    monkeypatch.setattr(service.vault, "append_journal", original)
    response = workspace["client"].post(
        "/v1/admin/restore/reconcile",
        headers=workspace["headers"],
        json={"review_reference": "Recovered original local journal storage"},
    )
    assert response.status_code == 200, response.text
    assert "EFFECT_JOURNAL_RECOVERY_REQUIRED" not in service.preflight(p)["blockers"]
    assert service.vault.read_journal("effects"), "Repair must persist the original pending metadata"


def test_original_paid_intents_are_journaled_for_configuration_and_batch(workspace, app, batch):  # noqa: F811
    from test_batch_recovery import submit
    from test_number_reconciliation import configure_fixture

    service, p, number = configure_fixture(workspace, app)
    config, _ = service._claim_number_configuration(p, number, "desired")
    assert submit(workspace, batch).status_code == 503
    entries = service.vault.read_journal("effects")
    assert any(r["kind"] == "number_configure" and r["phase"] == "submitted" for r in entries)
    commands = [e for r in entries for e in r["entities"] if e["table"] == "command_outbox"]
    assert any(e["id"] == config["id"] for e in commands)
    assert any(e["data"]["kind"] == "script_batch" and e["data"].get("request_digest") for e in commands)


def test_prepaid_screen_journal_keeps_exact_attempt_without_its_secret(workspace, screening):  # noqa: F811
    from test_dnc_recovery import screen

    service, p, *_ = screening
    assert screen(workspace).status_code == 200
    entries = service.vault.read_journal("effects")
    assert any(
        e["table"] == "registry_snapshots" and e["data"].get("request_key")
        for r in entries
        for e in r["entities"]
    )
    assert "synthetic-private-key" not in str(entries)
