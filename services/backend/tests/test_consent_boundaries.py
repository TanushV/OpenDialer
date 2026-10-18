"""Additional denial recovery boundaries found while tracing rollback and publication."""

import asyncio
import os
import stat

import pytest
from app.db import get, put
from conftest import reserve, start
from test_consent_journal import decision, evidence, records, submit
from test_control_completion import principal


def test_preserved_revocation_recreates_its_endpoint_missing_from_old_backup(workspace, app):
    service, p = app.state.service, principal(workspace)
    backup = service.backup(p)
    with service.db.transaction() as s:
        endpoint = put(
            s,
            "phone_endpoints",
            p["tenant_id"],
            p["seller_id"],
            {"phone": "+14155550197", "format_status": "valid"},
            key="+14155550197",
        )
        identity = endpoint.id
    assert submit(workspace, evidence(workspace, app, endpoint_id=identity)).status_code == 200
    service.restore(p, backup["backup_id"])
    with service.db.transaction(False) as s:
        assert get(s, "phone_endpoints", identity), (
            "A retained revocation must not become an orphan after restore"
        )


def test_restore_does_not_reenable_capture_on_an_older_call_snapshot(workspace, app):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    backup = service.backup(p)
    assert (
        submit(
            workspace,
            evidence(workspace, app, endpoint_id=call["endpoint_id"], permissions=["transcription"]),
        ).status_code
        == 200
    )
    service.restore(p, backup["backup_id"])
    result = asyncio.run(
        service.transcript(
            p,
            call["id"],
            {
                "segment_id": "restored-late",
                "revision": 1,
                "text": "No capture authorization",
                "is_final": True,
                "side": "remote",
            },
            defer_coach=True,
        )
    )
    assert result.get("unavailable") == "permission_revoked"


def test_late_chunks_on_a_new_call_respect_prior_transcription_revocation(workspace, app):
    service, p = app.state.service, principal(workspace)
    selection = reserve(workspace)
    assert selection.status_code == 200, selection.text
    with service.db.transaction(False) as s:
        reservation = get(s, "reservations", selection.json()["reservation_id"])
        endpoint_id = reservation.data["endpoint_id"]
    body = evidence(workspace, app, endpoint_id=endpoint_id, permissions=["transcription"])
    assert submit(workspace, body).status_code == 200
    response = workspace["client"].post(
        "/v1/calls/start",
        headers=workspace["headers"],
        json={"reservation_id": selection.json()["reservation_id"], "idempotency_key": "new-call"},
    )
    assert response.status_code == 200, response.text
    call = response.json()
    assert call["endpoint_id"] == body["endpoint_id"]
    result = asyncio.run(
        service.transcript(
            p,
            call["id"],
            {
                "segment_id": "never-authorized",
                "revision": 1,
                "text": "Should not enter the buffer",
                "is_final": True,
                "side": "remote",
            },
            defer_coach=True,
        )
    )
    assert result.get("unavailable") == "permission_revoked"


def test_missing_failed_journal_entry_cannot_be_reviewed_away(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    original = service.vault.append_journal

    def unavailable(*args):
        raise OSError("Synthetic unwritable journal")

    monkeypatch.setattr(service.vault, "append_journal", unavailable)
    body = evidence(workspace, app)
    assert submit(workspace, body).status_code == 503
    monkeypatch.setattr(service.vault, "append_journal", original)
    response = workspace["client"].post(
        "/v1/admin/restore/reconcile",
        headers=workspace["headers"],
        json={"review_reference": "No carrier activity"},
    )
    assert response.status_code == 409, "A review reference is not the missing revocation receipt"
    assert "JOURNAL_RECOVERY_REQUIRED" in decision(service, p, body["endpoint_id"]).reasons
    # Resubmit the SAME request after storage is repaired, then reconcile its durable entry.
    assert submit(workspace, body).status_code == 200
    response = workspace["client"].post(
        "/v1/admin/restore/reconcile",
        headers=workspace["headers"],
        json={"review_reference": "Recovered original permission withdrawal"},
    )
    assert response.status_code == 200, response.text
    assert "CONSENT_REVOKED" in decision(service, p, body["endpoint_id"]).reasons
    assert "JOURNAL_RECOVERY_REQUIRED" not in decision(service, p, body["endpoint_id"]).reasons
    assert len(records(service, p)) == 1


def test_old_backup_cannot_erase_a_missing_revocation_journal_hold(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    backup = service.backup(p)
    original = service.vault.append_journal

    def unavailable(*args):
        raise OSError("Synthetic disk failure before append")

    monkeypatch.setattr(service.vault, "append_journal", unavailable)
    body = evidence(workspace, app)
    assert submit(workspace, body).status_code == 503
    monkeypatch.setattr(service.vault, "append_journal", original)
    service.restore(p, backup["backup_id"])
    response = workspace["client"].post(
        "/v1/admin/restore/reconcile",
        headers=workspace["headers"],
        json={"review_reference": "Restored the old backup"},
    )
    assert response.status_code == 409, "Rollback must retain the missing durable denial identity"
    assert "JOURNAL_RECOVERY_REQUIRED" in decision(service, p, body["endpoint_id"]).reasons
    assert submit(workspace, body).status_code == 200
    assert (
        workspace["client"]
        .post(
            "/v1/admin/restore/reconcile",
            headers=workspace["headers"],
            json={"review_reference": "Original denial is now durably recovered"},
        )
        .status_code
        == 200
    )


def test_readable_but_unsynced_journal_cannot_release_recovery_hold(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    original = os.fsync

    def unavailable(fd):
        raise OSError("Synthetic fsync unavailable")

    monkeypatch.setattr(os, "fsync", unavailable)
    body = evidence(workspace, app)
    assert submit(workspace, body).status_code == 503
    assert service.vault.read_journal("consent"), "Bytes are readable but not confirmed durable"
    response = workspace["client"].post(
        "/v1/admin/restore/reconcile",
        headers=workspace["headers"],
        json={"review_reference": "Still unable to sync"},
    )
    assert response.status_code in {409, 503}, "Reading cached bytes is not proof storage is repaired"
    assert "JOURNAL_RECOVERY_REQUIRED" in decision(service, p, body["endpoint_id"]).reasons
    monkeypatch.setattr(os, "fsync", original)
    assert (
        workspace["client"]
        .post(
            "/v1/admin/restore/reconcile",
            headers=workspace["headers"],
            json={"review_reference": "Storage is repaired and synced"},
        )
        .status_code
        == 200
    )


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX directory durability; Windows uses the file commit boundary"
)
def test_first_journal_creation_syncs_its_parent_directory(workspace, app, monkeypatch):
    original = os.fsync
    synced = []

    def sync(fd):
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        return original(fd)

    monkeypatch.setattr(os, "fsync", sync)
    assert submit(workspace, evidence(workspace, app)).status_code == 200
    assert True in synced, "A durable new journal requires the directory entry as well as file content"


def test_partial_journal_write_is_not_acknowledged(workspace, app, monkeypatch):
    original = os.write

    def partial(fd, value):
        return original(fd, value[: len(value) // 2])

    monkeypatch.setattr(os, "write", partial)
    body = evidence(workspace, app)
    assert submit(workspace, body).status_code == 503
    assert (
        "JOURNAL_RECOVERY_REQUIRED"
        in decision(app.state.service, principal(workspace), body["endpoint_id"]).reasons
    )
    assert not records(app.state.service, principal(workspace))


@pytest.mark.parametrize("already_received", [False, True])
def test_signed_speech_receipts_do_not_retain_revoked_content_or_report_false_loss(
    workspace, app, already_received
):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    event = {
        "id": "revoked-speech-event",
        "event_type": "call.transcription",
        "payload": {
            "call_control_id": call["provider_id"],
            "transcription_data": {
                "transcript": "Keep this speech out of the cache",
                "is_final": True,
                "transcription_track": "inbound",
            },
        },
    }
    if already_received:
        receipt = asyncio.run(service.accept_webhook(p["tenant_id"], event, "same-body-hash"))
        assert service.raw.get("webhook:" + receipt["receipt_id"])
    assert (
        submit(
            workspace,
            evidence(workspace, app, endpoint_id=call["endpoint_id"], permissions=["transcription"]),
        ).status_code
        == 200
    )
    if not already_received:
        receipt = asyncio.run(service.accept_webhook(p["tenant_id"], event, "same-body-hash"))
    assert service.raw.get("webhook:" + receipt["receipt_id"]) is None
    result = asyncio.run(service.process_webhook(p["tenant_id"], receipt["receipt_id"]))
    assert result["status"] == "processed"
    with service.db.transaction(False) as s:
        current = get(s, "call_attempts", call["id"])
        assert not current.data.get("unprocessed_speech_events")
        assert service.raw_status(current, []) == "permission_revoked"
