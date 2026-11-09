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
    managed.state, managed.caught_up, managed.last_exchange = "reconnecting", False, 0.0
    assert "CONNECTION_RECONNECTING" in blockers(workspace)
    asyncio.run(service.hangup(principal(workspace), call["id"]))
    assert all(leg["state"] == "hangup" for leg in carrier.calls.values())
    managed.config = {}


def test_missing_speech_is_a_warning_and_never_a_blocker(workspace, app):
    managed = app.state.service.managed
    managed.config = {"tenant_id": workspace["tenant_id"]}
    managed.caught_up, managed.last_exchange, managed.speech_failed = True, monotonic(), True
    try:
        ready(workspace)
        assert managed.status()["speech_warning"] and blockers(workspace) == []
    finally:
        managed.config, managed.speech_failed = {}, False


def test_a_live_helper_outage_never_stops_a_call(workspace, app, clock, monkeypatch):
    """Section 5: with OpenRouter down, the helper pauses and says so; the call and calling go on."""
    from app import live_help

    class Down:
        last_usage, last_request_id = {}, None

        async def converse(self, *args, **kwargs):
            raise httpx.ConnectError("OpenRouter unreachable")  # Before any request reached it.

    monkeypatch.setattr(live_help, "provider", lambda service, p: Down())
    response = start(workspace, "outage-helper")
    assert response.status_code == 200, response.text
    call_id = response.json()["id"]
    c, h = workspace["client"], workspace["headers"]
    for kind in ("call.answered", "human.confirmed"):
        event = {"event_id": kind + call_id, "event_type": kind, "call_id": call_id, "payload": {}}
        assert c.post("/v1/simulation/event", headers=h, json=event).status_code == 200
    c.post(
        f"/v1/calls/{call_id}/transcript",
        headers=h,
        json={"segment_id": "s1", "revision": 1, "side": "remote", "is_final": True, "text": "Who is this?"},
    )
    clock.now += timedelta(seconds=1)
    asyncio.run(app.state.service.run_jobs(queues=["live_coach"]))
    live = c.get(f"/v1/calls/{call_id}", headers=h).json()
    assert live["coach"] is None
    assert live["helper_status"] == {"status": "paused", "reason": "transport_fault"}
    assert live["state"] not in {"ENDED", "FINALIZED"} and blockers(workspace) == []
    # Nothing reached OpenRouter, so the turn's budget hold is released, not left standing.
    from app.platform.db import rows

    with app.state.service.db.transaction(False) as s:
        holds = [r.data for r in rows(s, "usage_ledger") if r.data.get("work_kind") == "live_coach"]
    assert holds and all(h.get("status") == "released" for h in holds), holds


@pytest.mark.parametrize("audio", [{"audio_ready": False}, {"telephony_ready": False}])
def test_a_lost_microphone_names_itself_and_calling_returns_with_it(workspace, audio):
    ready(workspace, **audio)
    assert blockers(workspace) == ["REP_AUDIO_NOT_READY"]
    ready(workspace)
    assert blockers(workspace) == []


def test_a_telnyx_channel_limit_refusal_pauses_only_this_seat_and_is_not_counted(
    workspace, app, carrier, clock, monkeypatch
):
    """R9C.4: over the account's calls-at-once limit, the dial is refused; this seat waits a minute with a named
    blocker, the lead keeps its attempt, and calling resumes by itself."""
    from app.calling.carrier_errors import is_channel_limit

    async def limited(*args, **kwargs):
        request = httpx.Request("POST", "https://api.telnyx.com/v2/calls")
        body = {
            "errors": [
                {
                    "code": "90041",
                    "title": "Channel limit exceeded",
                    "detail": "Concurrent call limit reached",
                }
            ]
        }
        response = httpx.Response(403, json=body, request=request)
        raise httpx.HTTPStatusError("refused", request=request, response=response)

    monkeypatch.setattr(carrier, "dial", limited)
    call = start(workspace, "limit-dial").json()
    current = workspace["client"].get(f"/v1/calls/{call['id']}", headers=workspace["headers"]).json()
    assert current["state"] in {"ENDED", "FINALIZED"}
    assert "TELNYX_CALL_LIMIT_REACHED" in blockers(workspace)
    lead = (
        workspace["client"]
        .get(f"/v1/leads/{call['lead_id']}/packet", headers=workspace["headers"])
        .json()["lead"]
    )
    assert lead["attempt_count"] == 0, "nothing reached the prospect, so the attempt is not counted"
    clock.now += timedelta(seconds=61)
    assert "TELNYX_CALL_LIMIT_REACHED" not in blockers(workspace)
    assert not is_channel_limit(403, {"errors": [{"title": "Invalid caller ID"}]})
    assert not is_channel_limit(503, "channel limit")
