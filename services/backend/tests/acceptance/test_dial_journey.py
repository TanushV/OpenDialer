"""Call control at the application boundary, with no real destinations or credentials."""

import asyncio
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
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


@pytest.mark.slow
def test_packaged_calling_smoke_drives_a_fresh_authority(client):
    check.packaged_calls(client, "test-local-secret")
    assert client.get("/health").json()["initialized"] is True


class CarrierHTTP:
    """Record the actual adapter's requests and control the carrier's replies."""

    def __init__(self):
        self.requests = []
        self.dials = {}
        self.live = set()
        self.lose_dial_response = False

    def __call__(self, request):
        path = request.url.path.removeprefix("/v2")
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, path, body))
        if path == "/telephony_credentials/fixture-credential":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "resource_id": "connection:12345",
                        "sip_username": "fixture-rep",
                        "expired": False,
                    }
                },
            )
        if path == "/credential_connections/12345":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "sip_uri_calling_preference": "internal",
                        "outbound": {"outbound_voice_profile_id": None},
                    }
                },
            )
        if (request.method, path) == ("POST", "/calls"):
            identity = f"fixture-leg-{len(self.dials) + 1}"
            self.dials[identity] = body
            self.live.add(identity)
            if self.lose_dial_response and not body["to"].startswith("sip:"):
                raise httpx.ReadTimeout("accepted; response lost", request=request)
            return httpx.Response(200, json={"data": {"call_control_id": identity}})
        method, identity, *action = path.removeprefix("/").split("/")
        assert method == "calls" and identity in self.dials, (request.method, path)
        if request.method == "GET":
            return httpx.Response(
                200, json={"data": {"call_control_id": identity, "is_alive": identity in self.live}}
            )
        assert action in (
            ["actions", "hangup"],
            ["actions", "transcription_start"],
            ["actions", "transcription_stop"],
        )
        if action[-1] == "hangup":
            self.live.discard(identity)
        return httpx.Response(200, json={"data": {"result": "ok"}})

    def dial_budget(self, count):
        # Count requests before carrier-side deduplication. An extra POST is an extra attempted effect.
        dials = [body for method, path, body in self.requests if (method, path) == ("POST", "/calls")]
        rep_legs = [body for body in dials if body["to"].startswith("sip:")]
        prospects = [body for body in dials if not body["to"].startswith("sip:")]
        assert (len(rep_legs), len(prospects)) == (count, count), self.requests
        assert len({body["command_id"] for body in dials}) == count * 2
        return prospects


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
def test_lost_dial_response_restart_late_answer_and_stop_stay_on_the_original_attempt(
    workspace, app, clock, carrier_http
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
    restarted = create_app(app.state.settings, clock=clock, carrier=adapter)
    with TestClient(restarted) as client:
        signed_in = client.post("/v1/local/session", headers={"X-Local-Token": "test-local-secret"})
        assert signed_in.is_success, signed_in.text
        reopened = {
            **workspace,
            "client": client,
            "headers": {
                "Authorization": "Bearer " + signed_in.json()["access_token"],
            },
        }
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
