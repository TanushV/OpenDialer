"""Call control at the application boundary, with no real destinations or credentials."""

import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Event

import httpx
import pytest
from app import ops
from app.calling.state import TERMINAL
from app.main import create_app
from app.managed_webhooks import deliver
from app.providers.telnyx import TelnyxCarrier
from conftest import ready, reserve
from fastapi.testclient import TestClient
from helpers import disposition_body

from scripts import check
from services.backend.tests.carrier_http import CarrierHTTP


@pytest.mark.slow
def test_packaged_calling_smoke_drives_a_fresh_authority(client):
    check.packaged_calls(client, "test-local-secret")
    assert client.get("/health").json()["initialized"] is True


@pytest.fixture
def carrier_http(workspace, app):
    transport = CarrierHTTP()
    http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    carrier = TelnyxCarrier("test-transport-only", "call-control-fixture", http=http)
    app.state.service.carriers = lambda *_args, **_kwargs: carrier
    app.state.service.configs.write(
        workspace["tenant_id"],
        {
            "sip_username": "fixture-rep",
            "telephony_credential_id": "fixture-credential",
        },
    )
    yield transport, carrier
    workspace["client"].portal.call(http.aclose)


def api(workspace, method, path, data=None):
    response = workspace["client"].request(method, path, headers=workspace["headers"], json=data)
    assert response.is_success, response.text
    return response.json()


def receipt(workspace, clock, carrier, leg, kind, **fields):
    """Deliver the carrier-shaped event through the normal relay inbox and worker."""
    key = f"{leg}-{kind}"
    event = {
        "id": key,
        "event_type": kind,
        "payload": {**carrier.dials[leg], "call_control_id": leg, **fields},
    }
    client = workspace["client"]
    client.portal.call(
        deliver,
        client.app.state.service,
        workspace["tenant_id"],
        [
            {
                "event": event,
                "event_id": key,
                "hash": key,
                "account_id": "fixture",
                "received_at": clock.now.isoformat(),
            }
        ],
    )
    api(workspace, "POST", "/v1/admin/jobs/run")


def dial(workspace, clock, carrier, key):
    agent = api(workspace, "POST", "/v1/telephony/agent")["agent_call_control_id"]
    receipt(workspace, clock, carrier, agent, "call.answered")
    assert api(workspace, "GET", f"/v1/telephony/agent/{agent}")["state"] == "answered"
    ready(workspace, agent_call_control_id=agent)
    hold = api(workspace, "POST", "/v1/queue/next", {"campaign_id": workspace["campaign_id"]})
    request = {"reservation_id": hold["reservation_id"], "idempotency_key": key}
    call = api(workspace, "POST", "/v1/calls/start", request)
    assert api(workspace, "POST", "/v1/calls/start", request)["id"] == call["id"]
    return call, hold, agent, request


@contextmanager
def reopened_authority(workspace, app, clock, adapter):
    restarted = create_app(app.state.settings, clock=clock, carrier=adapter)
    restarted.state.service.require_prepared_scripts = False
    try:
        with TestClient(restarted) as client:
            signed_in = client.post("/v1/local/session", headers={"X-Local-Token": "test-local-secret"})
            assert signed_in.is_success, signed_in.text
            yield {
                **workspace,
                "client": client,
                "headers": {"Authorization": "Bearer " + signed_in.json()["access_token"]},
            }
    finally:
        restarted.state.db.engine.dispose()


@pytest.mark.slow
def test_two_calls_use_two_legs_each_and_require_new_rep_intent(workspace, clock, carrier_http):
    carrier, _ = carrier_http
    snapshots = []
    for outcome in ("voicemail", "not_interested"):
        current, hold, agent, _ = dial(workspace, clock, carrier, outcome)
        call_id, leg = current["id"], current["provider_id"]
        path = f"/v1/calls/{call_id}"
        assert carrier.dials[leg]["link_to"] == agent
        assert carrier.dials[leg]["bridge_on_answer"] is True
        assert carrier.dials[leg]["to"] == hold["lead"]["phone"]
        assert carrier.dials[leg]["from"] == hold["number"]["phone"]
        assert carrier.dials[leg]["command_id"] == call_id
        assert current["is_test"] is False
        receipt(workspace, clock, carrier, leg, "call.answered")
        if outcome == "voicemail":
            receipt(workspace, clock, carrier, leg, "call.machine.premium.detection.ended", result="machine")
            receipt(
                workspace, clock, carrier, leg, "call.machine.premium.greeting.ended", result="beep_detected"
            )
            clock.now += timedelta(seconds=12)
            api(workspace, "POST", "/v1/admin/recovery")
            assert api(workspace, "GET", path)["state"] == "VOICEMAIL_READY"
            assert leg in carrier.live
            api(workspace, "POST", path + "/hangup", {"mark_voicemail": True})
        else:
            receipt(
                workspace,
                clock,
                carrier,
                leg,
                "call.machine.premium.detection.ended",
                result="human_residence",
            )
            api(workspace, "POST", path + "/hangup")
        carrier.live.discard(agent)
        receipt(
            workspace,
            clock,
            carrier,
            leg,
            "call.hangup",
            hangup_source="caller",
            hangup_cause="normal_clearing",
        )
        receipt(workspace, clock, carrier, agent, "call.hangup", hangup_source="caller")
        if outcome != "voicemail":
            body = disposition_body(workspace["client"], workspace["headers"], call_id, outcome=outcome)
            api(workspace, "POST", path + "/disposition", body)
        settled = api(workspace, "GET", path)
        assert settled["state"] == "FINALIZED" and settled["outcome"] == outcome
        snapshots.append((call_id, agent))
        assert not carrier.live
        api(workspace, "POST", "/v1/admin/recovery")
        carrier.dial_budget(len(snapshots))
    assert len(set(snapshots)) == 2
    controls = Counter(
        body["command_id"]
        for method, path, body in carrier.requests
        if method == "POST" and "/actions/" in path
    )
    assert all(count == 1 for count in controls.values()), carrier.requests
    assert all(controls[call_id + ":hangup"] == 1 for call_id, _ in snapshots)


@pytest.mark.slow
@pytest.mark.parametrize("evidence_delayed", [False, True], ids=["before-timeout", "marked-unconfirmed"])
def test_lost_dial_response_restart_late_answer_and_stop_stay_on_the_original_attempt(
    workspace, app, clock, carrier_http, evidence_delayed
):
    carrier, adapter = carrier_http
    carrier.lose_dial_response = True
    current, _, _, request = dial(workspace, clock, carrier, "uncertain-first-attempt")
    assert current["state"] == "RECONCILING"
    [prospect] = carrier.dial_budget(1)
    (leg,) = [key for key, body in carrier.dials.items() if body is prospect]
    path = f"/v1/calls/{current['id']}"
    assert api(workspace, "POST", path + "/hangup")["state"] == "ENDING"
    assert len(carrier.live) == 2

    # Drop the in-memory authority/session. Reopen the same SQLite store while the carrier still has the call.
    with reopened_authority(workspace, app, clock, adapter) as reopened:
        if evidence_delayed:
            clock.now += timedelta(seconds=150)
            api(reopened, "POST", "/v1/admin/recovery")
            assert api(reopened, "GET", path)["state"] == "DIAL_UNCONFIRMED"
        assert api(reopened, "POST", "/v1/calls/start", request)["id"] == current["id"]
        for _ in range(2):
            receipt(reopened, clock, carrier, leg, "call.answered")
        assert api(reopened, "GET", path)["state"] == "ENDING"
        for _ in range(2):
            clock.now += timedelta(seconds=31)
            api(reopened, "POST", "/v1/admin/recovery")
        assert leg not in carrier.live
        receipt(
            reopened,
            clock,
            carrier,
            leg,
            "call.hangup",
            hangup_source="caller",
            hangup_cause="normal_clearing",
        )
        assert api(reopened, "GET", path)["state"] == "ENDED"
        carrier.dial_budget(1)
        assert [
            body["command_id"]
            for method, target, body in carrier.requests
            if method == "POST" and target.endswith("/actions/hangup")
        ] == [current["id"] + ":hangup"]


@pytest.mark.slow
@pytest.mark.parametrize(
    "carrier_already_ended", [False, True], ids=["accepted-stop-reply-lost", "old-end-first"]
)
def test_late_stop_after_recovery_timeout_does_not_touch_the_next_call(
    workspace, app, clock, carrier_http, carrier_already_ended
):
    carrier, adapter = carrier_http
    carrier.lose_dial_response = True
    original, held, original_agent, request = dial(workspace, clock, carrier, "outage-original")
    assert original["state"] == "RECONCILING"
    original_path = f"/v1/calls/{original['id']}"
    (original_leg,) = carrier.live - {original_agent}
    assert api(workspace, "POST", original_path + "/hangup")["state"] == "ENDING"
    api(workspace, "POST", "/v1/rep/presence", {"status": "paused"})
    carrier.live.remove(original_agent)  # The rep disconnects while the carrier still has the prospect.

    with reopened_authority(workspace, app, clock, adapter) as reopened:
        clock.now += timedelta(seconds=150)
        api(reopened, "POST", "/v1/admin/recovery")
        assert api(reopened, "GET", original_path)["state"] == "DIAL_UNCONFIRMED"
        clock.now += timedelta(seconds=301)
        api(reopened, "POST", "/v1/admin/recovery")
        stopped = api(reopened, "GET", original_path)
        assert (stopped["state"], stopped["end_reason"]) == ("ENDED", "recovery_timeout")
        assert api(reopened, "POST", "/v1/calls/start", request)["id"] == original["id"]
        carrier.dial_budget(1)

        carrier.lose_dial_response = False
        next_call, next_hold, next_agent, _ = dial(reopened, clock, carrier, "after-outage")
        assert next_hold["lead"]["phone"] != held["lead"]["phone"]
        next_leg = next_call["provider_id"]
        next_path = f"/v1/calls/{next_call['id']}"
        receipt(reopened, clock, carrier, next_leg, "call.answered")
        assert api(reopened, "GET", next_path)["state"] == "ANSWERED_UNCLASSIFIED"

        carrier.lose_hangup_response_for = original_leg
        if carrier_already_ended:
            carrier.live.remove(original_leg)
            receipt(reopened, clock, carrier, original_leg, "call.hangup", hangup_cause="normal_clearing")
        # The relay returns with a beep, a duplicated answer, a delayed ring and the dead rep leg.
        receipt(
            reopened,
            clock,
            carrier,
            original_leg,
            "call.machine.premium.greeting.ended",
            result="beep_detected",
        )
        for _ in range(2):
            receipt(reopened, clock, carrier, original_leg, "call.answered")
        receipt(reopened, clock, carrier, original_leg, "call.ringing")
        receipt(reopened, clock, carrier, original_agent, "call.hangup")
        for _ in range(3):
            api(reopened, "POST", "/v1/admin/recovery")
            clock.now += timedelta(seconds=31)

        assert carrier.live == {next_leg, next_agent}
        assert api(reopened, "GET", original_path)["state"] == "ENDED"
        assert api(reopened, "GET", next_path)["state"] == "ANSWERED_UNCLASSIFIED"
        assert api(reopened, "POST", "/v1/calls/start", request)["id"] == original["id"]
        carrier.dial_budget(2)
        stops = [
            (path, body["command_id"])
            for method, path, body in carrier.requests
            if method == "POST" and path.endswith("/actions/hangup")
        ]
        assert stops == (
            []
            if carrier_already_ended
            else [(f"/calls/{original_leg}/actions/hangup", original["id"] + ":hangup")]
        )
        recovery = api(reopened, "GET", "/v1/admin/recovery")
        assert not [item for item in recovery["commands"] if item["call_id"] == original["id"]]

        api(reopened, "POST", next_path + "/hangup")
        receipt(reopened, clock, carrier, next_leg, "call.hangup", hangup_source="caller")
        carrier.live.remove(next_agent)
        receipt(reopened, clock, carrier, next_agent, "call.hangup")
        body = disposition_body(
            reopened["client"], reopened["headers"], next_call["id"], outcome="not_interested"
        )
        saved = api(reopened, "POST", next_path + "/disposition", body)
        assert (saved["state"], saved["outcome"]) == ("FINALIZED", "not_interested")
        assert not carrier.live
        carrier.dial_budget(2)


@pytest.mark.parametrize("action", ["hangup", "pause", "opt_out"])
def test_stopping_before_carrier_submission_cancels_the_pending_dial(workspace, carrier, monkeypatch, action):
    client, headers = workspace["client"], workspace["headers"]
    reservation = reserve(workspace)
    assert reservation.is_success, reservation.text
    data = {"reservation_id": reservation.json()["reservation_id"], "idempotency_key": "held-dial"}
    dispatch = ops.dispatch_call
    entered, resume = Event(), Event()

    async def held_dispatch(service, principal, call_id):
        entered.set()
        assert await asyncio.to_thread(resume.wait, 15)
        await dispatch(service, principal, call_id)

    monkeypatch.setattr(ops, "dispatch_call", held_dispatch)
    with ThreadPoolExecutor(max_workers=1) as executor:
        starting = executor.submit(client.post, "/v1/calls/start", headers=headers, json=data)
        try:
            assert entered.wait(15)
            calls = client.get("/v1/workspace/calls", headers=headers).json()["items"]
            [call] = calls
            assert call["state"] == "AUTHORIZED"
            if action == "hangup":
                stop = client.post(f"/v1/calls/{call['id']}/hangup", headers=headers)
                assert stop.json()["end_reason"] == "unsent_intent_cancelled"
            elif action == "pause":
                stop = client.post(
                    "/v1/rep/presence", headers=headers, json={"status": "paused", "telephony_ready": False}
                )
            else:
                stop = client.post(
                    "/v1/suppressions",
                    headers=headers,
                    json={"phone": reservation.json()["lead"]["phone"], "reason": "opt_out"},
                )
            assert stop.is_success, stop.text
        finally:
            resume.set()
        result = starting.result(timeout=15)

    assert result.is_success, result.text
    assert not carrier.dials, f"{action} arrived before the carrier request was claimed"
    assert result.json()["state"] in TERMINAL
    assert result.json()["provider_id"] is None
    assert client.post("/v1/calls/start", headers=headers, json=data).json()["id"] == call["id"]
    assert not carrier.dials
