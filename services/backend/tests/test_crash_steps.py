"""R4.13, G1 and G2: the app can stop at any dial or hang-up step and, after recovery, no lead is dialed twice and no
call is lost: each crashed call still exists and reaches an end."""

import asyncio
from datetime import timedelta

import pytest
from app.platform.db import MODELS
from conftest import ready, reserve
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
        asyncio.run(service.recover(principal(ws)))


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
        monkeypatch.setattr(service, "dispatch_call", crash_async)
    elif point == "before_carrier_request":
        monkeypatch.setattr(carrier, "dial", crash_async)
    elif point == "after_carrier_accepted":
        original = carrier.dial

        async def accepted_then_crash(payload):
            await original(payload)
            raise Crash(point)

        monkeypatch.setattr(carrier, "dial", accepted_then_crash)
    elif point == "before_receipt_saved":
        monkeypatch.setattr(service, "_dial_completed", crash)


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
        asyncio.run(service.start_call(principal(ws), body))
    monkeypatch.undo()
    recover_for_a_while(service, ws, clock)
    attempts = calls_for(service, lead_id)
    assert len(attempts) == 1, "recovery never makes a second call record"
    assert len(carrier.calls) <= 1 and len(carrier.dials) <= 1, "the carrier placed at most one call"
    assert attempts[0]["state"] in {"ENDED", "FINALIZED"}, attempts[0]["state"]


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

        monkeypatch.setattr(service, "_hangup_completed", crash)
    with pytest.raises(Crash):
        asyncio.run(service.hangup(principal(ws), started["id"]))
    monkeypatch.undo()
    recover_for_a_while(service, ws, clock)
    (call,) = calls_for(service, packet["lead"]["id"])
    assert call["state"] in {"ENDED", "FINALIZED"}, call["state"]
    assert all(leg["state"] == "hangup" for leg in carrier.calls.values())
