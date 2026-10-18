"""A06/A09: scoped, versioned operator recovery without blind external replay."""

import base64
import json

import pytest
from app.db import ActiveClaim, Job, doc, get, put, update
from conftest import start
from sqlalchemy import select
from test_control_completion import command_for, principal


def uncertain(w, *, missing=False):
    service, p = w["client"].app.state.service, principal(w)
    call = start(w)
    identity = command_for(service, p, call["id"])["id"]
    with service.db.transaction() as s:
        command = get(s, "command_outbox", identity)
        update(
            command,
            status="review_required",
            error="TimeoutError",
            lease_owner=None,
            lease_until=None,
            recovery_reason="Carrier response was not received",
            provider_id=None if missing else call["provider_id"],
        )
        if missing:
            update(get(s, "call_attempts", call["id"]), provider_id=None, state="RECONCILING")
        return call, doc(command)


def act(w, item, action, **extra):
    return w["client"].post(
        "/v1/admin/recovery/commands/" + item["id"],
        headers=w["headers"],
        json={"expected_version": item["version"], "action": action, **extra},
    )


def test_recovery_list_redacts_payloads_and_other_sellers(workspace, app):
    call, item = uncertain(workspace)
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        put(
            s,
            "command_outbox",
            p["tenant_id"],
            "unrelated-seller",
            {
                "kind": "dial",
                "status": "review_required",
                "call_id": "other-call",
                "payload": {"to": "+14155559999"},
            },
        )
    response = workspace["client"].get("/v1/admin/recovery", headers=workspace["headers"])
    assert response.status_code == 200, response.text
    listed = response.json()["commands"]
    assert len(listed) == 1 and listed[0]["id"] == item["id"]
    assert listed[0]["version"] == item["version"]
    assert listed[0]["call_id"] == call["id"]
    assert "payload" not in listed[0] and "+1415555" not in response.text


def test_recovery_actions_reject_stale_version(workspace, app):
    _, item = uncertain(workspace)
    with app.state.db.transaction() as s:
        update(get(s, "command_outbox", item["id"]), recovery_reason="New receipt changed this item")
    assert act(workspace, item, "reconcile").status_code == 409


def test_recovery_cannot_cancel_previously_submitted_effect(workspace):
    _, item = uncertain(workspace)
    assert act(workspace, item, "cancel_unsent").status_code == 409


def test_recovery_reconciles_existing_dial_without_another_call(workspace, carrier):
    _, item = uncertain(workspace)
    result = act(workspace, item, "reconcile")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed"
    assert len(carrier.dials) == 1


@pytest.mark.parametrize("correct", [True, False])
def test_missing_reference_is_verified_against_original_client_state(
    workspace, app, carrier, monkeypatch, correct
):
    call, item = uncertain(workspace, missing=True)
    service = app.state.service
    service.settings.simulation = False
    monkeypatch.setattr(service, "carrier", lambda _: carrier)

    async def receipt(*_):
        return {
            "call_control_id": "operator-reference",
            "is_alive": False,
            "client_state": base64.b64encode(
                json.dumps(
                    {
                        "call_id": call["id"] if correct else "another-call",
                        "tenant_id": workspace["tenant_id"],
                    }
                ).encode()
            ).decode(),
        }

    monkeypatch.setattr(carrier, "reconcile", receipt)
    result = act(workspace, item, "reconcile", provider_id="operator-reference")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == ("completed" if correct else "review_required")
    with service.db.transaction(False) as s:
        saved = get(s, "call_attempts", call["id"])
        assert saved.data["provider_id"] == ("operator-reference" if correct else None)
        assert saved.data["state"] == ("ENDED" if correct else "RECONCILING")
    assert len(carrier.dials) == 1


def test_explicit_stop_keeps_claim_until_terminal_receipt(workspace, app, carrier, monkeypatch):
    call, item = uncertain(workspace)
    service = app.state.service
    service.settings.simulation = False
    monkeypatch.setattr(service, "carrier", lambda _: carrier)
    result = act(workspace, item, "end_call")
    assert result.status_code == 200, result.text
    assert len(carrier.hangups) == 1
    with service.db.transaction(False) as s:
        assert get(s, "call_attempts", call["id"]).data["state"] == "ENDING"
        assert s.scalar(select(ActiveClaim).where(ActiveClaim.call_id == call["id"])) is not None


def test_dead_preparation_retry_reuses_job_and_preserves_attempt_history(workspace, app):
    service, p = app.state.service, principal(workspace)
    job = service.jobs.enqueue(
        "scripts",
        {"seller_id": p["seller_id"], "user_id": p["user_id"], "lead_ids": []},
        "dead-preparation",
        tenant_id=p["tenant_id"],
    )
    with service.db.transaction() as s:
        saved = s.get(Job, job["id"])
        saved.status, saved.attempts, saved.error = "dead", 3, "TimeoutError"
    payload = {"expected_attempts": 3}
    url = "/v1/admin/recovery/jobs/" + job["id"] + "/retry"
    result = workspace["client"].post(url, headers=workspace["headers"], json=payload)
    assert result.status_code == 200, result.text
    assert workspace["client"].post(url, headers=workspace["headers"], json=payload).status_code == 409
    with service.db.transaction(False) as s:
        saved = s.get(Job, job["id"])
        assert saved.status == "queued" and saved.attempts == 3 and saved.max_attempts == 4
        assert len(list(s.scalars(select(Job).where(Job.key == "dead-preparation")))) == 1


def test_dead_job_retry_cannot_cross_seller_or_run_call_control(workspace, app):
    service, p = app.state.service, principal(workspace)
    for seller, queue, status in [("other-seller", "research", 404), (p["seller_id"], "call_control", 409)]:
        job = service.jobs.enqueue(
            queue, {"seller_id": seller, "user_id": p["user_id"]}, queue, tenant_id=p["tenant_id"]
        )
        with service.db.transaction() as s:
            s.get(Job, job["id"]).status = "dead"
        result = workspace["client"].post(
            "/v1/admin/recovery/jobs/" + job["id"] + "/retry",
            headers=workspace["headers"],
            json={"expected_attempts": 0},
        )
        assert result.status_code == status, result.text


def test_expired_speech_cannot_be_replayed_from_dead_job(workspace, app):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    job = service.jobs.enqueue(
        "postcall",
        {"seller_id": p["seller_id"], "user_id": p["user_id"], "call_id": call["id"]},
        "expired-speech",
        tenant_id=p["tenant_id"],
    )
    with service.db.transaction() as s:
        s.get(Job, job["id"]).status = "dead"
    result = workspace["client"].post(
        "/v1/admin/recovery/jobs/" + job["id"] + "/retry",
        headers=workspace["headers"],
        json={"expected_attempts": 0},
    )
    assert result.status_code == 409, result.text
    assert "raw" in result.text.casefold()


@pytest.mark.parametrize("known", [True, False])
def test_agent_recovery_verifies_reference_without_another_dial(workspace, app, carrier, monkeypatch, known):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        leg = put(
            s,
            "call_legs",
            p["tenant_id"],
            p["seller_id"],
            {
                "kind": "agent",
                "rep_id": p["user_id"],
                "state": "uncertain",
                "provider_id": "agent-receipt" if known else None,
            },
        )
        identity, version = leg.id, leg.version
    service.settings.simulation = False
    monkeypatch.setattr(service, "carrier", lambda _: carrier)

    async def received(*_):
        return {
            "call_control_id": "agent-receipt",
            "is_alive": False,
            "client_state": base64.b64encode(
                json.dumps({"agent_leg_id": identity, "tenant_id": p["tenant_id"]}).encode()
            ).decode(),
        }

    monkeypatch.setattr(carrier, "reconcile", received)
    result = workspace["client"].post(
        "/v1/admin/recovery/agents/" + identity,
        headers=workspace["headers"],
        json={"expected_version": version, "action": "reconcile", "provider_id": "agent-receipt"},
    )
    assert result.status_code == 200, result.text
    with service.db.transaction(False) as s:
        assert get(s, "call_legs", identity).data["state"] == "ended"
    assert not carrier.dials


def test_unsent_cancel_releases_only_its_undialed_claim(workspace, app, carrier, monkeypatch):
    async def do_not_submit(*_):
        return None

    service, p = app.state.service, principal(workspace)
    monkeypatch.setattr(service, "dispatch_call", do_not_submit)
    call = start(workspace)
    item = command_for(service, p, call["id"])
    response = act(workspace, item, "cancel_unsent")
    assert response.status_code == 200, response.text
    with service.db.transaction(False) as s:
        assert get(s, "command_outbox", item["id"]).data["status"] == "cancelled"
        assert s.scalar(select(ActiveClaim).where(ActiveClaim.call_id == call["id"])) is None
    assert not carrier.dials


def test_legacy_job_error_content_is_not_exposed_in_recovery(workspace, app):
    service, p = app.state.service, principal(workspace)
    job = service.jobs.enqueue(
        "research",
        {"seller_id": p["seller_id"], "user_id": p["user_id"], "lead_ids": []},
        "legacy-error",
        tenant_id=p["tenant_id"],
    )
    with service.db.transaction() as s:
        saved = s.get(Job, job["id"])
        saved.status, saved.error = "dead", "The prospect said PRIVATE_SPEECH_EXAMPLE"
    response = workspace["client"].get("/v1/admin/recovery", headers=workspace["headers"])
    assert response.status_code == 200
    assert response.json()["jobs"]
    assert "PRIVATE_SPEECH_EXAMPLE" not in response.text


def test_legacy_command_error_content_is_not_exposed_in_recovery(workspace, app):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    item = command_for(service, p, call["id"])
    with service.db.transaction() as s:
        update(
            get(s, "command_outbox", item["id"]),
            status="review_required",
            error="Provider returned PRIVATE_SPEECH_EXAMPLE",
            recovery_reason=None,
        )
    response = workspace["client"].get("/v1/admin/recovery", headers=workspace["headers"])
    assert response.status_code == 200 and response.json()["commands"]
    assert "PRIVATE_SPEECH_EXAMPLE" not in response.text
