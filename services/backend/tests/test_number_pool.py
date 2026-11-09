"""R9C.8: each dial's caller ID comes from the relay's shared pool; no lease, no dial; ended calls give it back."""

import asyncio
from types import SimpleNamespace

from app.numbers import pool
from conftest import ready, reserve

REAL_LEASE, REAL_RELEASE = pool.lease, pool.release


class FakeRelay:
    def __init__(self, phone="+14155550123", reason=None, down=False):
        self.phone, self.reason, self.down = phone, reason, down
        self.calls = []

    def rpc(self, name, args):
        self.calls.append((name, args))
        relay = self

        async def execute():
            if relay.down:
                raise ConnectionError("relay down")
            if name == "relay_lease_number":
                return SimpleNamespace(
                    data={"phone": relay.phone}
                    if not relay.reason
                    else {"phone": None, "reason": relay.reason}
                )
            return SimpleNamespace(data={})

        return SimpleNamespace(execute=execute)


def principal(ws):
    return {k: ws[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


def start(ws, key):
    packet = reserve(ws).json()
    response = ws["client"].post(
        "/v1/calls/start",
        headers=ws["headers"],
        json={"reservation_id": packet["reservation_id"], "idempotency_key": key},
    )
    return response.json()


def pooled(workspace, app, monkeypatch, relay):
    service = app.state.service
    monkeypatch.setattr(pool, "lease", lambda client, *a, **k: REAL_LEASE(relay, *a, **k))
    monkeypatch.setattr(pool, "release", lambda client, *a, **k: REAL_RELEASE(relay, *a, **k))
    service.configs.write(workspace["tenant_id"], {"number_pool": True})
    return service


def test_a_dial_presents_the_leased_pool_number_and_the_lease_ends_with_the_call(
    workspace, app, carrier, monkeypatch
):
    relay = FakeRelay()
    service = pooled(workspace, app, monkeypatch, relay)
    call = start(workspace, "pool-1")
    assert carrier.dials[-1]["from"] == "+14155550123"
    name, args = relay.calls[0]
    assert (
        name == "relay_lease_number"
        and args["call_ref"] == call["id"]
        and args["areas"][0] == args["prospect"][2:5]
    )
    asyncio.run(service.release_pool_leases(principal(workspace)))
    assert not any(n == "relay_release_number" for n, _ in relay.calls), "a live call keeps its number"
    workspace["client"].post(
        f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"], json={"idempotency_key": "h"}
    )
    service.event(principal(workspace), "pool-hangup", "call.hangup", call["id"], {})
    asyncio.run(service.release_pool_leases(principal(workspace)))
    assert ("relay_release_number", {"call_ref": call["id"]}) in relay.calls


def test_an_empty_or_unreachable_pool_places_nothing_and_names_why(
    workspace, app, carrier, monkeypatch, clock
):
    from datetime import timedelta

    for relay, code in (
        (FakeRelay(reason="POOL_EXHAUSTED"), "NUMBER_POOL_EMPTY"),
        (FakeRelay(down=True), "NUMBER_POOL_UNREACHABLE"),
    ):
        pooled(workspace, app, monkeypatch, relay)
        dials = len(carrier.dials)
        ready(workspace)
        call = start(workspace, f"pool-{code}")
        assert len(carrier.dials) == dials, "no caller ID, no dial"
        assert call.get("state") in {"ENDED", "FINALIZED"} or call.get("detail")
        codes = [
            b["code"]
            for b in workspace["client"].get("/v1/blockers", headers=workspace["headers"]).json()["blockers"]
        ]
        assert code in codes
        clock.now += timedelta(seconds=31)


def test_area_preferences_put_the_prospects_area_first_then_its_state():
    states = {"212": "NY", "718": "NY", "917": "NY", "415": "CA"}
    assert pool.area_preferences("+12125550100", states) == ["212", "718", "917"]
    assert pool.area_preferences("+15555550100", states) == ["555"]
    assert pool.area_preferences("", states) == []
