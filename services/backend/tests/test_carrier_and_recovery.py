import base64
from datetime import timedelta

import pytest
from app.providers.telnyx import verify_webhook
from app.telephony.reducer import reduce_state
from conftest import start
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


@pytest.mark.parametrize(
    "state,event,expected",
    [
        ("DIALING", "call.ringing", "RINGING"),
        ("RINGING", "call.answered", "ANSWERED_UNCLASSIFIED"),
        ("ENDED", "call.ringing", "ENDED"),
        ("FINALIZED", "call.answered", "FINALIZED"),
        ("HUMAN", "call.machine.detection.ended:machine", "HUMAN"),
        ("ANSWERED_UNCLASSIFIED", "call.machine.detection.ended:human", "HUMAN"),
        ("ANSWERED_UNCLASSIFIED", "call.machine.detection.ended:machine", "VOICEMAIL_GREETING"),
        ("VOICEMAIL_GREETING", "call.machine.greeting.ended:beep_detected", "VOICEMAIL_READY"),
        ("VOICEMAIL_GREETING", "call.machine.greeting.ended:no_beep_detected", "VOICEMAIL_GREETING"),
        ("SCREENING", "call.answered", "SCREENING"),
        ("HUMAN", "call.hangup", "ENDED"),
    ],
)
def test_monotonic_call_reducer(state, event, expected):
    assert reduce_state(state, event) == expected


def signed_payload(clock):
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    payload = b'{"data":{"id":"signed-event","event_type":"call.answered","payload":{}}}'
    timestamp = str(int(clock.now.timestamp()))
    signature = base64.b64encode(private.sign(timestamp.encode() + b"|" + payload)).decode()
    return public, payload, timestamp, signature


def test_webhook_verifies_raw_body_and_rejects_replay_or_tamper(clock):
    public, payload, timestamp, signature = signed_payload(clock)
    assert verify_webhook(payload, signature, timestamp, public, now=clock.now)
    assert not verify_webhook(payload + b" ", signature, timestamp, public, now=clock.now)
    assert not verify_webhook(payload, signature, timestamp, public, now=clock.now + timedelta(minutes=6))
    assert not verify_webhook(payload, "bad", timestamp, public, now=clock.now)


def test_unsigned_live_webhook_rejected_even_when_simulation_available(workspace):
    c = workspace["client"]
    assert c.post("/webhooks/telnyx", json={"data": {"id": "forged"}}).status_code == 401


def test_duplicate_and_late_events_are_durable_and_idempotent(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    body = {"event_id": "event-ended", "event_type": "call.hangup", "call_id": call["id"]}
    a = c.post("/v1/simulation/event", headers=h, json=body)
    b = c.post("/v1/simulation/event", headers=h, json=body)
    assert a.status_code == b.status_code == 200
    assert b.json()["duplicate"] is True
    c.post(
        "/v1/simulation/event",
        headers=h,
        json={**body, "event_id": "late-ringing", "event_type": "call.ringing"},
    )
    assert c.get(f"/v1/calls/{call['id']}", headers=h).json()["state"] == "ENDED"


def test_same_event_id_with_different_payload_is_not_silently_accepted(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    body = {"event_id": "collision", "event_type": "call.ringing", "call_id": call["id"]}
    assert c.post("/v1/simulation/event", headers=h, json=body).status_code == 200
    assert (
        c.post("/v1/simulation/event", headers=h, json={**body, "event_type": "call.hangup"}).status_code
        == 409
    )


def test_backup_restore_does_not_resurrect_optouts(workspace, tmp_path):
    c, h = workspace["client"], workspace["headers"]
    backup = c.post("/v1/admin/backup", headers=h).json()
    r = c.post("/v1/suppressions", headers=h, json={"phone": "+14155550101", "reason": "opt_out"})
    assert r.status_code == 200
    r = c.post("/v1/admin/restore", headers=h, json={"backup_id": backup["backup_id"]})
    assert r.status_code == 200, r.text
    leads = c.get("/v1/workspace", headers=h).json()["leads"]
    assert next(x for x in leads if x["phone"] == "+14155550101")["suppressed"]
    assert c.get("/v1/admin/preflight", headers=h).json()["integrity_ok"]


def test_voicemail_cannot_run_over_greeting_or_without_separate_permission(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    r = c.post(
        f"/v1/calls/{call['id']}/voicemail", headers=h, json={"asset_id": "approved-vm", "detach": False}
    )
    assert r.status_code == 409
    assert "MAILBOX_NOT_READY" in r.text


def test_monitored_voicemail_waits_for_start_confirmation(workspace, carrier):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    for i, event in enumerate(
        ["call.answered", "call.machine.detection.ended:machine", "call.machine.greeting.ended:beep_detected"]
    ):
        c.post(
            "/v1/simulation/event",
            headers=h,
            json={"event_id": f"vm-{i}", "event_type": event, "call_id": call["id"]},
        )
    c.post(
        f"/v1/admin/policies/{workspace['policy_id']}/publish",
        headers=h,
        json={
            "approved": True,
            "permissions": ["live", "prerecorded", "transcription"],
            "voicemail_enabled": True,
            "voicemail_optout_supported": True,
            "approved_assets": ["approved-vm"],
            "reason": "Synthetic permission fixture",
        },
    )
    r = c.post(
        f"/v1/calls/{call['id']}/voicemail", headers=h, json={"asset_id": "approved-vm", "detach": False}
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "VM_PENDING"
    assert carrier.playbacks[-1]["target_legs"] == "self"
    assert carrier.playbacks[-1]["media_name"] == "approved-vm"
    assert (
        c.post("/v1/queue/next", headers=h, json={"campaign_id": workspace["campaign_id"]}).status_code == 409
    )


def test_detachment_requires_proven_topology_and_recovery_destination(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    r = c.post(
        f"/v1/calls/{call['id']}/voicemail", headers=h, json={"asset_id": "approved-vm", "detach": True}
    )
    assert r.status_code in (403, 409)


def test_watchdog_ends_orphan_without_resubmitting_dial(workspace, carrier, clock):
    call = start(workspace)
    clock.now += timedelta(hours=5)
    c, h = workspace["client"], workspace["headers"]
    r = c.post("/v1/admin/recovery", headers=h)
    assert r.status_code == 200
    assert c.get(f"/v1/calls/{call['id']}", headers=h).json()["state"] == "ENDED"
    assert len(carrier.dials) == 1


def test_restore_holds_new_outbound_until_authorization_journal_is_reconciled(workspace):
    c, h = workspace["client"], workspace["headers"]
    backup = c.post("/v1/admin/backup", headers=h).json()
    c.post("/v1/admin/restore", headers=h, json={"backup_id": backup["backup_id"]})
    check = c.get("/v1/admin/preflight", headers=h).json()
    assert "RESTORE_RECONCILIATION_REQUIRED" in check["blockers"]
    result = c.post(
        "/v1/admin/restore/reconcile",
        headers=h,
        json={"review_reference": "Reviewed synthetic empty carrier state"},
    )
    assert result.status_code == 200, result.text


def test_failed_hangup_keeps_live_claim_until_provider_terminal_event(workspace, carrier, monkeypatch):
    from app.db import ActiveClaim
    from sqlalchemy import select

    call = start(workspace)

    async def unavailable(*args):
        raise TimeoutError("carrier unreachable")

    monkeypatch.setattr(carrier, "hangup", unavailable)
    c, h = workspace["client"], workspace["headers"]
    result = c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    assert result.status_code == 200
    assert result.json()["state"] == "ENDING"
    with c.app.state.db.transaction(False) as s:
        assert s.scalar(select(ActiveClaim).where(ActiveClaim.call_id == call["id"])) is not None
    c.post(
        "/v1/simulation/event",
        headers=h,
        json={"event_id": "terminal-after-failed-hangup", "event_type": "call.hangup", "call_id": call["id"]},
    )
    with c.app.state.db.transaction(False) as s:
        assert s.scalar(select(ActiveClaim).where(ActiveClaim.call_id == call["id"])) is None


def test_playback_completion_is_not_proof_that_carrier_leg_ended(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(
        "/v1/simulation/event",
        headers=h,
        json={
            "event_id": "playback-complete-open-leg",
            "event_type": "call.playback.ended",
            "call_id": call["id"],
        },
    )
    assert c.get(f"/v1/calls/{call['id']}", headers=h).json()["state"] != "ENDED"
