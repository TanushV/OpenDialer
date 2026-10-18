"""Owned-number changes are resumable and cannot purchase or silently lose callbacks."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from app.db import update
from app.providers.telnyx import TelnyxCarrier
from fastapi import HTTPException
from test_remediation_privacy import principal


@pytest.mark.asyncio
async def test_inventory_pages_are_complete_or_explicitly_rejected():
    seen = []

    def response(request):
        page = int(request.url.params["page[number]"])
        seen.append(page)
        return httpx.Response(
            200,
            json={
                "data": [{"id": str(page), "phone_number": "+14155550100"}],
                "meta": {"page_number": page, "total_pages": 2},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        assert len(await TelnyxCarrier("fixture", "c", http=http).list_numbers()) == 2
    assert seen == [1, 2]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": [], "meta": {"total_pages": 101}})
        )
    ) as http:
        with pytest.raises(ValueError, match="inventory|pagination"):
            await TelnyxCarrier("fixture", "c", http=http).list_numbers()


def configure_fixture(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        number = service.scoped(s, "number_inventory", p)[0]
        update(number, provider_id="owned", connection_id="old")
        identifier = number.id
    service.vault.write(p["tenant_id"], {"telnyx_connection_id": "desired"})
    return service, p, identifier


def test_lost_configuration_response_reconciles_before_another_patch(
    workspace, app, carrier, monkeypatch, clock
):
    service, p, identifier = configure_fixture(workspace, app)
    sent, retrieved = [], []

    async def configure(number, connection):
        sent.append((number, connection))
        raise TimeoutError("lost after applied")

    async def retrieve(number):
        retrieved.append(number)
        return {"id": number, "connection_id": "desired"}

    monkeypatch.setattr(carrier, "configure_number", configure, raising=False)
    monkeypatch.setattr(carrier, "get_number", retrieve, raising=False)
    path = f"/v1/admin/numbers/{identifier}/configure"
    c, h = workspace["client"], workspace["headers"]
    assert c.post(path, headers=h).status_code == 503
    clock.now += timedelta(seconds=61)
    r = c.post(path, headers=h)
    assert r.status_code == 200, r.text
    assert len(sent) == 1 and retrieved == ["owned"]
    assert r.json()["callback_tested"] is False
    with service.db.transaction(False) as s:
        intents = [
            x for x in service.scoped(s, "command_outbox", p) if x.data.get("kind") == "number_configure"
        ]
        assert len(intents) == 1 and intents[0].data["status"] == "completed"


def test_inventory_routing_change_invalidates_callback_evidence_and_keeps_identity(
    workspace, app, carrier, monkeypatch
):
    service, p, identifier = configure_fixture(workspace, app)
    with service.db.transaction(False) as s:
        prior = service.required(s, "number_inventory", identifier, p).data
        phone = prior["phone"]

    async def inventory():
        return [{"id": "owned", "phone_number": phone, "connection_id": "different", "status": "active"}]

    monkeypatch.setattr(carrier, "list_numbers", inventory)
    r = workspace["client"].post("/v1/admin/numbers/sync", headers=workspace["headers"])
    assert r.status_code == 200, r.text
    with service.db.transaction(False) as s:
        current = service.required(s, "number_inventory", identifier, p).data
        assert current["phone"] == phone
        assert current["connection_id"] == "different"
        assert not current["callback_tested"]
        assert current["lifecycle"] == "investigating"
    assert not carrier.purchases


def test_absent_number_is_flagged_without_releasing_the_callback_identity(
    workspace, app, carrier, monkeypatch
):
    service, p, identifier = configure_fixture(workspace, app)

    async def empty():
        return []

    monkeypatch.setattr(carrier, "list_numbers", empty)
    r = workspace["client"].post("/v1/admin/numbers/sync", headers=workspace["headers"])
    assert r.status_code == 200
    with service.db.transaction(False) as s:
        row = service.required(s, "number_inventory", identifier, p)
        assert row.data["provider_present"] is False
        assert row.data["lifecycle"] == "paused_for_outbound"
        assert row.data["callback_retained"] is True


@pytest.mark.parametrize(
    "change", [{"daily_limit": -1}, {"capacity": 0}, {"capacity": 50}, {"daily_limit": True}]
)
def test_edit_number_rejects_invalid_workload_limits(workspace, change):
    c, h = workspace["client"], workspace["headers"]
    number = c.get("/v1/workspace", headers=h).json()["numbers"][0]
    assert c.patch(f"/v1/admin/numbers/{number['id']}", headers=h, json=change).status_code == 422


def test_production_identity_promotion_requires_confirmed_carrier_route(workspace, app, monkeypatch):
    c, h = workspace["client"], workspace["headers"]
    number = c.post("/v1/admin/numbers", headers=h, json={"phone": "+14155550198"}).json()
    monkeypatch.setattr(app.state.service.settings, "simulation", False)
    r = c.patch(
        f"/v1/admin/numbers/{number['id']}",
        headers=h,
        json={
            "lifecycle": "identity_configured",
            "identity_configured": True,
            "registration_reference": "reviewed",
        },
    )
    assert r.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("meta", [None, {}, {"total_pages": True}, {"total_pages": 2, "page_number": 2}])
async def test_inventory_does_not_treat_incomplete_pagination_as_complete(meta):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [], "meta": meta}))
    ) as http:
        with pytest.raises(ValueError, match="inventory|pagination"):
            await TelnyxCarrier("fixture", "c", http=http).list_numbers()


@pytest.mark.asyncio
async def test_number_configuration_has_one_owner_and_fences_inventory_changes(
    workspace, app, carrier, monkeypatch
):
    service, p, identifier = configure_fixture(workspace, app)
    started, release = asyncio.Event(), asyncio.Event()

    async def configure(number, connection):
        started.set()
        await release.wait()
        return {"id": number, "connection_id": connection}

    monkeypatch.setattr(carrier, "configure_number", configure, raising=False)
    first = asyncio.create_task(service.configure_owned_number(p, identifier))
    await started.wait()
    with pytest.raises(HTTPException) as duplicate:
        await service.configure_owned_number(p, identifier)
    assert duplicate.value.status_code == 409
    with service.db.transaction() as s:
        number = service.required(s, "number_inventory", identifier, p)
        update(
            number,
            provider_id="replacement",
            configuration_status="review_required",
            identity_configured=False,
        )
    release.set()
    with pytest.raises(HTTPException) as stale:
        await first
    assert stale.value.status_code == 409
    with service.db.transaction(False) as s:
        number = service.required(s, "number_inventory", identifier, p)
        assert number.data["configuration_status"] != "confirmed"
        assert number.data["identity_configured"] is False


def test_unconfirmed_route_is_never_promoted_or_resent(workspace, app, carrier, monkeypatch):
    service, p, identifier = configure_fixture(workspace, app)
    sent = []

    async def configure(number, connection):
        sent.append(number)
        return {"id": number, "connection_id": "different"}

    monkeypatch.setattr(carrier, "configure_number", configure, raising=False)
    monkeypatch.setattr(carrier, "get_number", lambda number: configure(number, "unused"), raising=False)
    path = f"/v1/admin/numbers/{identifier}/configure"
    assert workspace["client"].post(path, headers=workspace["headers"]).status_code == 503
    with service.db.transaction(False) as s:
        number = service.required(s, "number_inventory", identifier, p)
        assert number.data["configuration_status"] == "uncertain"
        assert number.data["callback_tested"] is False


@pytest.mark.asyncio
async def test_simulated_owned_configuration_preserves_phone_and_cannot_buy(carrier):
    carrier.numbers = {"owned": {"id": "owned", "phone_number": "+14155550198", "status": "active"}}
    assert len(await carrier.list_numbers()) == 1
    receipt = await carrier.configure_number("owned", "route")
    assert receipt["connection_id"] == "route"
    assert (await carrier.get_number("owned"))["phone_number"] == "+14155550198"
    assert not carrier.purchases
