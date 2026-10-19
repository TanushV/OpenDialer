"""A08/A43/A54: permission revocation is scoped, serialized and survives rollback."""

import asyncio
import json

import pytest
from app.compliance.policy import authorize
from app.db import MODELS, doc, get, put
from app.main import create_app
from conftest import start
from sqlalchemy import delete
from test_control_completion import principal


def evidence(w, app, **changes):
    service, p = app.state.service, principal(w)
    with service.db.transaction(False) as s:
        lead = service.scoped(s, "leads", p)[0]
        body = {
            "endpoint_id": lead.data["endpoint_id"],
            "permissions": ["live"],
            "revoked": True,
            "evidence_reference": "Permission withdrawal recorded by the rep",
            "idempotency_key": "consent-change-1",
            **changes,
        }
    return body


def submit(w, body):
    return w["client"].post("/v1/admin/consent", headers=w["headers"], json=body)


def records(service, p):
    with service.db.transaction(False) as s:
        return [doc(r) for r in service.scoped(s, "consent_evidence", p)]


def decision(service, p, endpoint_id, permission="live"):
    with service.db.transaction(False) as s:
        lead = next(r for r in service.scoped(s, "leads", p) if r.data["endpoint_id"] == endpoint_id)
        number = service.scoped(s, "number_inventory", p)[0]
        return authorize(service.context(s, p, lead, number, permission=permission, active=True))


@pytest.mark.parametrize("permission", ["transcription", "recording", "prerecorded", "messaging"])
def test_one_permission_revocation_does_not_revoke_other_permissions(workspace, app, permission):
    body = evidence(workspace, app, permissions=[permission])
    assert submit(workspace, body).status_code == 200
    service, p = app.state.service, principal(workspace)
    assert "CONSENT_REVOKED" not in decision(service, p, body["endpoint_id"]).reasons
    assert "CONSENT_REVOKED" in decision(service, p, body["endpoint_id"], permission).reasons


@pytest.mark.parametrize(
    "changes",
    [
        {"permissions": "live"},
        {"permissions": ["unknown"]},
        {"permissions": []},
        {"revoked": "false"},
        {"evidence_reference": "  "},
        {"idempotency_key": ""},
        {"acquired_at": "2026-10-14T12:00:00"},
        {"acquired_at": "2100-01-01T00:00:00Z"},
        {"permissions": ["live", "live"]},
    ],
)
def test_consent_requires_typed_scope_identity_and_time(workspace, app, changes):
    response = submit(workspace, evidence(workspace, app, **changes))
    assert response.status_code == 422, response.text
    assert not records(app.state.service, principal(workspace))


def test_same_consent_request_is_one_receipt_and_changed_scope_conflicts(workspace, app):
    body = evidence(workspace, app)
    first, second = submit(workspace, body), submit(workspace, body)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(records(app.state.service, principal(workspace))) == 1
    assert submit(workspace, {**body, "revoked": False}).status_code == 409


def test_consent_uses_the_same_authorization_fence_as_call_submission(workspace, app, monkeypatch):
    original = app.state.db.authorize_stream
    scopes = []

    def fenced(session, p):
        scopes.append((p["tenant_id"], p["seller_id"]))
        return original(session, p)

    monkeypatch.setattr(app.state.db, "authorize_stream", fenced)
    assert submit(workspace, evidence(workspace, app)).status_code == 200
    assert scopes == [(workspace["tenant_id"], workspace["seller_id"])]


def test_revocation_journal_is_encrypted_and_restores_a_lost_newer_record(workspace, app):
    service, p = app.state.service, principal(workspace)
    backup = service.backup(p)
    body = evidence(workspace, app)
    receipt = submit(workspace, body)
    assert receipt.status_code == 200
    path = service.settings.data_dir / "consent-journal.enc"
    assert path.exists(), "Revocation needs a journal independent of the database backup"
    assert body["evidence_reference"].encode() not in path.read_bytes()
    assert (path.stat().st_mode & 0o777) == 0o600
    with service.db.transaction() as s:
        s.execute(delete(MODELS["consent_evidence"]))  # Lost writes after the older backup.
    service.restore(p, backup["backup_id"])
    restored = records(service, p)
    assert len(restored) == 1 and restored[0]["id"] == receipt.json()["id"]
    assert restored[0]["revoked"] is True
    assert "CONSENT_REVOKED" in decision(service, p, body["endpoint_id"]).reasons
    service.restore(p, backup["backup_id"])
    assert len(records(service, p)) == 1


def test_restarted_authority_replays_newer_revocations_before_accepting_work(workspace, app, clock):
    body = evidence(workspace, app)
    assert submit(workspace, body).status_code == 200
    with app.state.db.transaction() as s:
        s.execute(delete(MODELS["consent_evidence"]))
    restarted = create_app(app.state.settings, clock=clock)
    try:
        assert (
            "CONSENT_REVOKED"
            in decision(restarted.state.service, principal(workspace), body["endpoint_id"]).reasons
        )
    finally:
        restarted.state.db.engine.dispose()


def test_grant_does_not_override_a_recorded_withdrawal(workspace, app):
    body = evidence(workspace, app)
    assert submit(workspace, body).status_code == 200
    assert submit(workspace, {**body, "revoked": False, "idempotency_key": "later-grant"}).status_code == 200
    assert "CONSENT_REVOKED" in decision(app.state.service, principal(workspace), body["endpoint_id"]).reasons


def test_disk_failure_does_not_acknowledge_revocation_and_holds_outbound(workspace, app, monkeypatch):
    import os

    def disk_full(fd):
        raise OSError("Synthetic disk full")

    body = evidence(workspace, app)
    monkeypatch.setattr(os, "fsync", disk_full)
    response = submit(workspace, body)
    assert response.status_code == 503, response.text
    assert (
        "JOURNAL_RECOVERY_REQUIRED"
        in decision(app.state.service, principal(workspace), body["endpoint_id"]).reasons
    )
    preflight = workspace["client"].get("/v1/admin/preflight", headers=workspace["headers"]).json()
    assert "JOURNAL_RECOVERY_REQUIRED" in preflight["blockers"]


def test_journal_is_durable_before_the_consent_write_commits(workspace, app, monkeypatch, clock):
    from sqlalchemy import event

    def crash(session, *_):
        if any(isinstance(r, MODELS["consent_evidence"]) for r in session.new):
            raise RuntimeError("Synthetic consent database interruption")

    body = evidence(workspace, app)
    event.listen(app.state.db.sessions.class_, "before_flush", crash)
    try:
        try:
            response = submit(workspace, body)
        except RuntimeError:
            pass
        else:
            assert response.status_code == 503
    finally:
        event.remove(app.state.db.sessions.class_, "before_flush", crash)
    restarted = create_app(app.state.settings, clock=clock)
    try:
        assert (
            "CONSENT_REVOKED"
            in decision(restarted.state.service, principal(workspace), body["endpoint_id"]).reasons
        )
    finally:
        restarted.state.db.engine.dispose()


def test_malformed_journal_cannot_release_a_restore_hold(workspace, app):
    service, p = app.state.service, principal(workspace)
    backup = service.backup(p)
    service.restore(p, backup["backup_id"])
    path = service.settings.data_dir / "consent-journal.enc"
    path.write_bytes(b"corrupt-or-partial-entry\n")
    result = workspace["client"].post(
        "/v1/admin/restore/reconcile",
        headers=workspace["headers"],
        json={"review_reference": "Reviewed empty carrier state"},
    )
    assert result.status_code == 409
    with service.db.transaction(False) as s:
        assert get(s, "sellers", p["seller_id"]).data["restore_hold"] is True


def test_revocation_of_an_active_capture_stops_it_and_rejects_late_chunks(workspace, app, carrier):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        lead = get(s, "leads", call["lead_id"])
        endpoint = lead.data["endpoint_id"]
    asyncio.run(
        service.transcript(
            p,
            call["id"],
            {
                "segment_id": "before",
                "revision": 1,
                "text": "Temporary permissioned speech",
                "is_final": True,
                "side": "remote",
            },
            defer_coach=True,
        )
    )
    with service.db.transaction(False) as s:
        assert service.transcript_rows(s, p, call["id"])
    assert (
        submit(
            workspace, evidence(workspace, app, endpoint_id=endpoint, permissions=["transcription"])
        ).status_code
        == 200
    )
    asyncio.run(service.dispatch_controls(p))
    assert any(
        a["action"] == "transcription_stop" and a["provider_id"] == call["provider_id"]
        for a in carrier.actions
    )
    response = asyncio.run(
        service.transcript(
            p,
            call["id"],
            {
                "segment_id": "late",
                "revision": 1,
                "text": "Do not retain this late chunk",
                "is_final": True,
                "side": "remote",
            },
            defer_coach=True,
        )
    )
    assert response.get("unavailable") == "permission_revoked"
    with service.db.transaction(False) as s:
        assert not service.transcript_rows(s, p, call["id"])
        assert get(s, "call_attempts", call["id"]).data["state"] == call["state"]


def test_corrupt_revocation_journal_prevents_authority_startup(workspace, app, clock):
    path = app.state.settings.data_dir / "consent-journal.enc"
    path.write_bytes(b"unreadable-record\n")
    with pytest.raises(Exception, match="[Jj]ournal"):
        restarted = create_app(app.state.settings, clock=clock)
        restarted.state.db.engine.dispose()


def test_revocation_stops_a_live_call_without_releasing_its_carrier_obligation(workspace, app, carrier):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        endpoint = get(s, "leads", call["lead_id"]).data["endpoint_id"]
    response = submit(workspace, evidence(workspace, app, endpoint_id=endpoint))
    assert response.status_code == 200
    with service.db.transaction(False) as s:
        row = get(s, "call_attempts", call["id"])
        assert row.data["state"] == "ENDING"
        assert any(
            r.data.get("action") == "hangup" and r.data["status"] == "pending"
            for r in service.scoped(s, "command_outbox", p)
        )
    asyncio.run(service.dispatch_controls(p))
    assert any(item["provider_id"] == call["provider_id"] for item in carrier.hangups)


def test_already_committed_transcript_cannot_republish_raw_after_revocation(workspace, app, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        endpoint = get(s, "leads", call["lead_id"]).data["endpoint_id"]
    entered, resume = Event(), Event()
    original = service._publish_transcripts

    def delayed(writes):
        entered.set()
        assert resume.wait(5)
        original(writes)

    monkeypatch.setattr(service, "_publish_transcripts", delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            service._receive_transcript,
            p,
            call["id"],
            {
                "segment_id": "racing",
                "revision": 1,
                "text": "Must not be republished",
                "is_final": True,
                "side": "remote",
            },
            None,
            True,
        )
        try:
            assert entered.wait(5)
            assert (
                submit(
                    workspace, evidence(workspace, app, endpoint_id=endpoint, permissions=["transcription"])
                ).status_code
                == 200
            )
        finally:
            resume.set()
        pending.result(timeout=5)
    assert service.raw.get(service.raw_key(p, call["id"], "racing")) is None


def test_restored_revocation_keeps_original_phone_identity_when_endpoint_was_lost(workspace, app):
    service, p = app.state.service, principal(workspace)
    backup = service.backup(p)
    with service.db.transaction() as s:
        endpoint = put(
            s,
            "phone_endpoints",
            p["tenant_id"],
            p["seller_id"],
            {"phone": "+14155550198", "format_status": "valid"},
            key="+14155550198",
        )
        endpoint_id = endpoint.id
    response = submit(workspace, evidence(workspace, app, endpoint_id=endpoint_id))
    assert response.status_code == 200
    with service.db.transaction() as s:
        s.execute(delete(MODELS["consent_evidence"]))
    service.restore(p, backup["backup_id"])
    with service.db.transaction(False) as s:
        endpoint = get(s, "phone_endpoints", endpoint_id)
        assert endpoint and endpoint.data["phone"] == "+14155550198"
        assert len(service.scoped(s, "consent_evidence", p)) == 1


def test_other_seller_revocation_is_not_replayed_into_this_restore(workspace, app):
    service, p = app.state.service, principal(workspace)
    backup = service.backup(p)
    body = evidence(workspace, app)
    assert submit(workspace, body).status_code == 200
    path = service.settings.data_dir / "consent-journal.enc"
    assert path.exists()
    row = service.vault.read_journal("consent")[0]
    row["seller_id"] = "different-seller"
    # Import an authentic legacy foreign-seller fixture, not a mutation outside
    # the new chained record envelope (which leaves the actual record unchanged).
    path.write_bytes(service.vault.cipher.encrypt(json.dumps(row).encode()) + b"\n")
    (service.settings.data_dir / "consent-journal.head").unlink(missing_ok=True)
    with service.db.transaction() as s:
        s.execute(delete(MODELS["consent_evidence"]))
    service.restore(p, backup["backup_id"])
    assert not records(service, p)
