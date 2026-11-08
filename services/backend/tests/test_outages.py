"""R4.14 and G5: an outage affects only what depends on it (section 5 outage table).

Rows covered here: Telnyx API, relay, transcription, live helper and microphone. The ChatGPT, DDGS, HubSpot and Treg
rows are tested with the code they belong to (R9.13, R9B.5, R9B.14).
"""

import asyncio
from datetime import timedelta
from time import monotonic

import httpx
import pytest
from conftest import ready, reserve


def principal(ws):
    return {k: ws[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


def blockers(ws):
    return [b["code"] for b in ws["client"].get("/v1/blockers", headers=ws["headers"]).json()["blockers"]]


def start(ws, key):
    packet = reserve(ws).json()
    response = ws["client"].post(
        "/v1/calls/start",
        headers=ws["headers"],
        json={"reservation_id": packet["reservation_id"], "idempotency_key": key},
    )
    return response


def down(*args, **kwargs):
    raise httpx.ConnectError("Telnyx unreachable")


async def down_async(*args, **kwargs):
    down()


def test_telnyx_down_at_dial_never_dials_twice_and_the_call_settles(
    workspace, app, carrier, clock, monkeypatch
):
    service = app.state.service
    monkeypatch.setattr(carrier, "dial", down_async)
    call = start(workspace, "outage-dial").json()
    assert call["state"] == "RECONCILING"  # Uncertain: never resent blindly.
    monkeypatch.undo()
    for _ in range(8):
        clock.now += timedelta(seconds=61)
        asyncio.run(service.recover(principal(workspace)))
    current = workspace["client"].get(f"/v1/calls/{call['id']}", headers=workspace["headers"]).json()
    assert current["state"] in {"ENDED", "FINALIZED"} and current["review_required"] is True
    assert carrier.dials == []


def test_telnyx_down_at_hangup_still_ends_the_call_when_it_returns(
    workspace, app, carrier, clock, monkeypatch
):
    service = app.state.service
    call = start(workspace, "outage-hangup").json()
    monkeypatch.setattr(carrier, "hangup", down_async)
    asyncio.run(service.hangup(principal(workspace), call["id"]))
    assert all(leg["state"] != "hangup" for leg in carrier.calls.values())
    monkeypatch.undo()
    for _ in range(3):
        clock.now += timedelta(seconds=61)
        asyncio.run(service.recover(principal(workspace)))
    assert all(leg["state"] == "hangup" for leg in carrier.calls.values())


def test_a_relay_outage_names_itself_and_never_blocks_hanging_up(workspace, app, carrier):
    service = app.state.service
    ready(workspace)
    call = start(workspace, "outage-relay").json()
    managed = service.managed
    managed.config = {"tenant_id": workspace["tenant_id"]}
    managed.state, managed.caught_up, managed.last_probe = "reconnecting", False, 0.0
    assert "CONNECTION_RECONNECTING" in blockers(workspace)
    asyncio.run(service.hangup(principal(workspace), call["id"]))
    assert all(leg["state"] == "hangup" for leg in carrier.calls.values())
    managed.config = {}


def test_missing_speech_is_a_warning_and_never_a_blocker(workspace, app):
    managed = app.state.service.managed
    managed.config = {"tenant_id": workspace["tenant_id"]}
    managed.caught_up, managed.last_probe, managed.speech_failed = True, monotonic(), True
    try:
        ready(workspace)
        assert managed.status()["speech_warning"] and blockers(workspace) == []
    finally:
        managed.config, managed.speech_failed = {}, False


def test_a_live_helper_outage_never_stops_a_call(workspace, app, monkeypatch):
    def helper_down(p):
        raise httpx.ConnectError("OpenRouter unreachable")

    monkeypatch.setattr(app.state.service, "configured_coach", helper_down)
    response = start(workspace, "outage-helper")
    assert response.status_code == 200, response.text
    assert blockers(workspace) == []


@pytest.mark.parametrize("audio", [{"audio_ready": False}, {"telephony_ready": False}])
def test_a_lost_microphone_names_itself_and_calling_returns_with_it(workspace, audio):
    ready(workspace, **audio)
    assert blockers(workspace) == ["REP_AUDIO_NOT_READY"]
    ready(workspace)
    assert blockers(workspace) == []
