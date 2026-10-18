"""A06/A09: one keypad gesture has one durable, short-lived carrier intent."""

import asyncio
from datetime import timedelta

import pytest
from app.db import doc, get, update
from app.providers.simulated import UncertainCarrierResult
from conftest import start
from test_control_completion import principal


def send(w, call, digits="42#", key="keypad-request"):
    return w["client"].post(
        f"/v1/calls/{call['id']}/dtmf",
        headers=w["headers"],
        json={"digits": digits, "idempotency_key": key},
    )


def commands(service, p):
    with service.db.transaction(False) as s:
        return [doc(r) for r in service.scoped(s, "command_outbox", p) if r.data.get("action") == "send_dtmf"]


def tones(carrier):
    return [r for r in carrier.actions if r["action"] == "send_dtmf"]


def test_duplicate_keypad_request_is_one_persisted_effect(workspace, app, carrier):
    call = start(workspace)
    first, second = send(workspace, call), send(workspace, call)
    assert first.status_code == second.status_code == 200
    assert len(tones(carrier)) == 1
    saved = commands(app.state.service, principal(workspace))
    assert len(saved) == 1 and saved[0]["status"] == "completed"
    assert first.json()["command_id"] == second.json()["command_id"] == saved[0]["id"]
    assert saved[0]["target"] == call["provider_id"]
    assert saved[0]["payload"]["command_id"] == tones(carrier)[0]["payload"]["command_id"]


def test_reusing_keypad_identity_with_changed_digits_is_rejected(workspace, carrier):
    call = start(workspace)
    assert send(workspace, call).status_code == 200
    assert send(workspace, call, "9").status_code == 409
    assert len(tones(carrier)) == 1


def test_distinct_gestures_may_intentionally_repeat_the_same_digit(workspace, carrier):
    call = start(workspace)
    assert send(workspace, call, "1", "press-one").status_code == 200
    assert send(workspace, call, "1", "press-two").status_code == 200
    assert len(tones(carrier)) == 2
    assert tones(carrier)[0]["payload"]["command_id"] != tones(carrier)[1]["payload"]["command_id"]


@pytest.mark.parametrize(
    "body",
    [
        {"digits": "1"},
        {"digits": "1", "idempotency_key": ""},
        {"digits": "1", "idempotency_key": "bad/key"},
        {"digits": "1", "idempotency_key": "ok", "provider_id": "other"},
    ],
)
def test_keypad_requires_bounded_identity_and_rejects_target_overrides(workspace, carrier, body):
    call = start(workspace)
    response = workspace["client"].post(
        f"/v1/calls/{call['id']}/dtmf", headers=workspace["headers"], json=body
    )
    assert response.status_code == 422
    assert not tones(carrier)


@pytest.mark.parametrize("state", ["ENDING", "ENDED", "FINALIZED", "FAILED"])
def test_keypad_cannot_start_on_a_stopping_or_terminal_call(workspace, app, carrier, state):
    call = start(workspace)
    with app.state.db.transaction() as s:
        update(get(s, "call_attempts", call["id"]), state=state)
    assert send(workspace, call).status_code == 409
    assert not tones(carrier)


def test_keypad_cannot_control_detached_prospect(workspace, app, carrier):
    call = start(workspace)
    with app.state.db.transaction() as s:
        update(get(s, "call_attempts", call["id"]), detached=True)
    assert send(workspace, call).status_code == 409
    assert not tones(carrier)


def test_lost_carrier_response_retains_original_intent_and_local_status(workspace, app, carrier, monkeypatch):
    call = start(workspace)

    async def timeout(target, action, payload):
        carrier.actions.append({"provider_id": target, "action": action, "payload": payload})
        raise UncertainCarrierResult("lost response")

    monkeypatch.setattr(carrier, "action", timeout)
    first = send(workspace, call)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "uncertain" and first.json()["sent"] is False
    second = send(workspace, call)
    assert second.status_code == 200 and second.json()["command_id"] == first.json()["command_id"]
    status = workspace["client"].get(
        f"/v1/calls/{call['id']}/dtmf/keypad-request", headers=workspace["headers"]
    )
    assert status.status_code == 200 and status.json()["status"] == "uncertain"
    assert len(tones(carrier)) == 1, "UI retry or status GET must not replay an uncertain effect"
    assert len(commands(app.state.service, principal(workspace))) == 1


@pytest.mark.parametrize("change", ["ended", "detached", "revoked", "deleted", "moved", "target", "expired"])
def test_queued_keypad_rechecks_call_requester_target_and_deadline(
    workspace, app, carrier, monkeypatch, clock, change
):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    dispatch = service.dispatch_controls

    async def no_dispatch(*args, **kwargs):
        return {"processed": 0}

    # Capture the durable intent without submitting it, just as before a worker crash.
    monkeypatch.setattr(service, "dispatch_controls", no_dispatch)
    first = send(workspace, call)
    assert first.status_code == 200
    saved = commands(service, p)
    assert len(saved) == 1 and not tones(carrier)
    with service.db.transaction() as s:
        if change == "revoked":
            update(get(s, "users", p["user_id"]), disabled=True)
        elif change == "deleted":
            s.delete(get(s, "users", p["user_id"]))
        elif change == "moved":
            get(s, "users", p["user_id"]).seller_id = "different-seller"
        elif change != "expired":
            update(
                get(s, "call_attempts", call["id"]),
                **{
                    "ended": {"state": "ENDED"},
                    "detached": {"detached": True},
                    "target": {"provider_id": "changed-leg"},
                }[change],
            )
    if change == "expired":
        clock.now += timedelta(seconds=11)
    monkeypatch.setattr(service, "dispatch_controls", dispatch)
    asyncio.run(dispatch(p))
    assert not tones(carrier)
    assert commands(service, p)[0]["status"] in {"cancelled", "review_required"}


def test_uncertain_keypad_recovery_never_uses_a_new_command_identity(workspace, app, carrier, monkeypatch):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    original = carrier.action

    async def timeout(target, action, payload):
        await original(target, action, payload)
        raise UncertainCarrierResult()

    monkeypatch.setattr(carrier, "action", timeout)
    assert send(workspace, call).status_code == 200
    saved = commands(service, p)[0]
    monkeypatch.setattr(carrier, "action", original)
    asyncio.run(service.reconcile_command(p, saved["id"]))
    asyncio.run(service.dispatch_controls(p))
    assert len(tones(carrier)) == 2
    assert len({r["payload"]["command_id"] for r in tones(carrier)}) == 1
    assert commands(service, p)[0]["status"] == "completed"


def test_old_uncertain_keypad_is_review_work_not_a_new_tone(workspace, app, carrier, monkeypatch, clock):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)

    async def timeout(*args):
        raise UncertainCarrierResult()

    monkeypatch.setattr(carrier, "action", timeout)
    assert send(workspace, call).status_code == 200
    saved = commands(service, p)[0]
    clock.now += timedelta(seconds=61)
    asyncio.run(service.reconcile_command(p, saved["id"]))
    asyncio.run(service.dispatch_controls(p))
    assert not tones(carrier)
    assert commands(service, p)[0]["status"] == "review_required"


def test_concurrent_keypad_retry_submits_one_effect(workspace, app, carrier, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    call = start(workspace)
    original = carrier.action

    async def delayed(*args):
        await asyncio.sleep(0.05)
        return await original(*args)

    monkeypatch.setattr(carrier, "action", delayed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: send(workspace, call), range(2)))
    assert all(r.status_code == 200 for r in responses)
    assert len(tones(carrier)) == 1
    assert len(commands(app.state.service, principal(workspace))) == 1


@pytest.mark.parametrize("other_seller", [False, True])
def test_other_rep_cannot_send_or_inspect_keypad(workspace, app, carrier, clock, other_seller):
    from app.db import put
    from app.security import issue_session

    call = start(workspace)
    assert send(workspace, call).status_code == 200
    with app.state.db.transaction() as s:
        user = put(
            s,
            "users",
            workspace["tenant_id"],
            "different-seller" if other_seller else workspace["seller_id"],
            {"role": "rep"},
        )
        credentials = issue_session(s, app.state.settings, user, clock())
    headers = {"Authorization": f"Bearer {credentials['access_token']}"}
    response = workspace["client"].get(f"/v1/calls/{call['id']}/dtmf/keypad-request", headers=headers)
    assert response.status_code == 404
    response = workspace["client"].post(
        f"/v1/calls/{call['id']}/dtmf",
        headers=headers,
        json={"digits": "1", "idempotency_key": "stolen-call"},
    )
    assert response.status_code == 404
    assert len(tones(carrier)) == 1
