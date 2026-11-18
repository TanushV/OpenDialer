"""R4.13, G1 and G2: the app can stop at any dial or hang-up step and, after recovery, no lead is dialed twice and no
call is lost: each crashed call still exists and reaches an end."""

import asyncio
from datetime import timedelta

import pytest
from app import ops
from app.calling import dial
from app.platform.db import MODELS
from conftest import ready, reserve
from helpers import bound
from sqlalchemy import select


class Crash(BaseException):
    """The process stopped here: nothing after this point ran."""


def principal(ws):
    return {k: ws[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


def calls_for(service, lead_id):
    model = MODELS["call_attempts"]
    with service.db.transaction(False) as s:
        return [row.data for row in s.scalars(select(model)) if row.data["lead_id"] == lead_id]


def recover_for_a_while(service, ws, clock, rounds=14):
    for _ in range(rounds):
        clock.now += timedelta(seconds=61)
        asyncio.run(ops.recover(service, principal(ws)))


def begin(ws):
    ready(ws)
    packet = reserve(ws).json()
    return packet


def stop_at(point, service, carrier, monkeypatch):
    def crash(*args, **kwargs):
        raise Crash(point)

    async def crash_async(*args, **kwargs):
        raise Crash(point)

    if point == "before_dispatch":
        monkeypatch.setattr(dial, "_dispatch_call", crash_async)
    elif point == "before_carrier_request":
        monkeypatch.setattr(carrier, "dial", crash_async)
    elif point == "after_carrier_accepted":
        original = carrier.dial

        async def accepted_then_crash(payload):
            await original(payload)
            raise Crash(point)

        monkeypatch.setattr(carrier, "dial", accepted_then_crash)
    elif point == "before_receipt_saved":
        monkeypatch.setattr(ops, "_dial_completed", bound(crash))


@pytest.mark.slow
@pytest.mark.parametrize(
    "point", ["before_dispatch", "before_carrier_request", "after_carrier_accepted", "before_receipt_saved"]
)
def test_a_crash_at_any_dial_step_never_dials_twice_and_never_loses_the_call(
    workspace, app, carrier, clock, monkeypatch, point
):
    service, ws = app.state.service, workspace
    packet = begin(ws)
    lead_id = packet["lead"]["id"]
    stop_at(point, service, carrier, monkeypatch)
    with pytest.raises(Crash):
        body = {"reservation_id": packet["reservation_id"], "idempotency_key": "crash-" + point}
        asyncio.run(ops.start_call(service, principal(ws), body))
    monkeypatch.undo()
    if point == "before_dispatch":
        asyncio.run(ops.recover(service, principal(ws)))
        assert not carrier.dials, "Recovery cannot send the abandoned dial even before its deadline"
    recover_for_a_while(service, ws, clock)
    attempts = calls_for(service, lead_id)
    assert len(attempts) == 1, "recovery never makes a second call record"
    dispatched = point in {"after_carrier_accepted", "before_receipt_saved"}
    assert len(carrier.dials) == int(dispatched), (
        "Recovery must never send a dial the rep's request did not send"
    )
    assert len(carrier.calls) == int(dispatched)
    assert attempts[0]["state"] in {"ENDED", "FINALIZED"}, attempts[0]["state"]
    if point == "before_dispatch":
        assert attempts[0]["state"] == "FINALIZED"
        assert attempts[0]["end_reason"] == "dial_not_sent" and attempts[0].get("dial_error")
        client, headers = ws["client"], ws["headers"]
        assert (
            client.post("/v1/calls/start", headers=headers, json=body).json()["end_reason"] == "dial_not_sent"
        )
        assert not carrier.dials, "Repeating the interrupted request cannot send it"
        new_hold = begin(ws)
        fresh = {"reservation_id": new_hold["reservation_id"], "idempotency_key": "pressed-again"}
        placed = client.post("/v1/calls/start", headers=headers, json=fresh)
        assert placed.is_success, placed.text
        assert len(carrier.dials) == 1 and placed.json()["provider_id"]


@pytest.mark.slow
@pytest.mark.parametrize("point", ["before_carrier_hangup", "after_carrier_hangup", "before_hangup_saved"])
def test_a_crash_at_any_hangup_step_still_ends_the_call(workspace, app, carrier, clock, monkeypatch, point):
    service, ws = app.state.service, workspace
    packet = begin(ws)
    started = (
        ws["client"]
        .post(
            "/v1/calls/start",
            headers=ws["headers"],
            json={"reservation_id": packet["reservation_id"], "idempotency_key": "hangup-" + point},
        )
        .json()
    )
    if point == "before_carrier_hangup":

        async def crash(*args):
            raise Crash(point)

        monkeypatch.setattr(carrier, "hangup", crash)
    elif point == "after_carrier_hangup":
        original = carrier.hangup

        async def hung_then_crash(*args):
            await original(*args)
            raise Crash(point)

        monkeypatch.setattr(carrier, "hangup", hung_then_crash)
    else:

        def crash(*args, **kwargs):
            raise Crash(point)

        monkeypatch.setattr(ops, "_hangup_completed", bound(crash))
    with pytest.raises(Crash):
        asyncio.run(ops.hangup(service, principal(ws), started["id"]))
    monkeypatch.undo()
    recover_for_a_while(service, ws, clock)
    (call,) = calls_for(service, packet["lead"]["id"])
    assert call["state"] in {"ENDED", "FINALIZED"}, call["state"]
    assert all(leg["state"] == "hangup" for leg in carrier.calls.values())
