"""Explicit number rental approval; provider effects remain simulated."""

from datetime import timedelta

import pytest


def request(w, method, path, body=None, status=200):
    r = w["client"].request(method, path, headers=w["headers"], json=body)
    assert r.status_code == status, r.text
    return r.json()


def quote(w, region="650"):
    return request(w, "POST", "/v1/admin/numbers/search", {"region": region})["numbers"][0]


def limits(w, count=3):
    return request(
        w,
        "PUT",
        "/v1/admin/settings",
        {
            "maximum_owned_numbers": count,
            "number_initial_limit_usd": 10,
            "number_monthly_limit_usd": 10,
        },
    )


def approve(w, q, status=200):
    return request(
        w,
        "POST",
        "/v1/admin/numbers/purchase",
        {
            "quote_id": q["id"],
            "approve_recurring": True,
        },
        status,
    )


def test_purchase_requires_limits_and_explicit_approval(workspace, carrier):
    q = quote(workspace)
    assert q["id"]
    approve(workspace, q, 409)
    limits(workspace)
    request(
        workspace,
        "POST",
        "/v1/admin/numbers/purchase",
        {
            "quote_id": q["id"],
            "approve_recurring": False,
        },
        422,
    )
    assert carrier.purchases == []
    result = approve(workspace, q)
    assert result["status"] == "ordered"
    assert len(carrier.purchases) == 1
    assert approve(workspace, q)["id"] == result["id"]
    assert len(carrier.purchases) == 1
    numbers = request(workspace, "GET", "/v1/workspace/numbers")["items"]
    new = next(n for n in numbers if n["phone"] == q["phone"])
    assert new["lifecycle"] == "purchased" and not new["callback_tested"]
    approve(workspace, quote(workspace, "408"), 409)


def test_expired_quote_never_orders(workspace, carrier, clock):
    limits(workspace)
    q = quote(workspace)
    clock.now += timedelta(minutes=6)
    approve(workspace, q, 409)
    assert carrier.purchases == []


@pytest.mark.parametrize("field", ["number_initial_limit_usd", "number_monthly_limit_usd"])
def test_purchase_checks_both_cost_limits(workspace, carrier, field):
    limits(workspace)
    request(workspace, "PUT", "/v1/admin/settings", {field: 0})
    approve(workspace, quote(workspace), 409)
    assert carrier.purchases == []


def test_uncertain_purchase_never_resubmits_and_survives_new_quote(workspace, carrier, monkeypatch):
    limits(workspace)
    q = quote(workspace)

    async def lost(*args):
        carrier.purchases.append(args)
        raise TimeoutError("Lost receipt")

    monkeypatch.setattr(carrier, "purchase_number", lost, raising=False)
    assert approve(workspace, q)["status"] == "uncertain"
    assert approve(workspace, q)["status"] == "uncertain"
    approve(workspace, quote(workspace), 409)
    assert len(carrier.purchases) == 1


def test_changed_price_does_not_order(workspace, carrier, monkeypatch):
    limits(workspace)
    q = quote(workspace)

    async def changed(region):
        return [
            {
                "phone_number": q["phone"],
                "cost_information": {
                    "currency": "USD",
                    "upfront_cost": "5",
                    "monthly_cost": "20",
                },
            }
        ]

    monkeypatch.setattr(carrier, "search_numbers", changed)
    assert approve(workspace, q)["status"] == "failed"
    assert carrier.purchases == []


def test_order_reconciliation_does_not_buy_again(workspace, carrier, monkeypatch):
    limits(workspace)
    q = quote(workspace)
    purchase = carrier.purchase_number

    async def accepted_but_lost(phone, reference):
        await purchase(phone, reference)
        raise TimeoutError("Receipt lost")

    monkeypatch.setattr(carrier, "purchase_number", accepted_but_lost)
    assert approve(workspace, q)["status"] == "uncertain"
    result = request(workspace, "POST", f"/v1/admin/numbers/orders/{q['id']}/reconcile")
    assert result["status"] == "ordered"
    assert len(carrier.purchases) == 1
    assert request(workspace, "GET", "/v1/admin/numbers/orders")[0]["provider_order_id"]


def test_pending_order_reserves_last_number_slot(workspace, carrier, monkeypatch):
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    limits(workspace)
    first, second = quote(workspace), quote(workspace, "408")
    entered, release = threading.Event(), threading.Event()
    purchase = carrier.purchase_number

    async def delayed(phone, reference):
        entered.set()
        await asyncio.to_thread(release.wait, 30)
        return await purchase(phone, reference)

    monkeypatch.setattr(carrier, "purchase_number", delayed)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(approve, workspace, first)
        try:
            assert entered.wait(30)
            approve(workspace, second, 409)
        finally:
            release.set()
        assert future.result()["status"] == "ordered"
    assert len(carrier.purchases) == 1


def test_telnyx_purchase_and_reconciliation_use_original_reference(workspace, app, monkeypatch):
    import json

    import httpx
    from app.providers.telnyx import TelnyxCarrier

    limits(workspace)
    q = quote(workspace)
    service = app.state.service
    service.settings.simulation = False
    service.configs.write(
        workspace["tenant_id"], {"telnyx_api_key": "synthetic", "telnyx_connection_id": "route"}
    )
    seen, saved = [], {}

    def handler(req):
        seen.append((req.method, req.url.path))
        if req.url.path == "/v2/available_phone_numbers":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "phone_number": q["phone"],
                            "cost_information": {"currency": "USD", "upfront_cost": "0", "monthly_cost": "1"},
                        }
                    ]
                },
            )
        if req.method == "POST":
            body = json.loads(req.content)
            assert body == {
                "connection_id": "route",
                "customer_reference": q["id"],
                "phone_numbers": [{"phone_number": q["phone"]}],
            }
            saved.update(id="provider-order", customer_reference=q["id"], status="success")
            raise httpx.ReadTimeout("lost response", request=req)
        if req.url.path.endswith("/voice"):
            return httpx.Response(
                200, json={"data": {"call_recording": {"inbound_call_recording_enabled": False}}}
            )
        if req.url.path == "/v2/number_orders":
            assert req.url.params["filter[customer_reference]"] == q["id"]
            return httpx.Response(200, json={"data": [saved]})
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "owned",
                        "phone_number": q["phone"],
                        "status": "active",
                    }
                ],
                "meta": {"total_pages": 1, "page_number": 1},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(service, "carrier", lambda _: TelnyxCarrier("synthetic", "route", http=http))
    try:
        assert approve(workspace, q)["status"] == "uncertain"
        assert (
            request(workspace, "POST", f"/v1/admin/numbers/orders/{q['id']}/reconcile")["provider_order_id"]
            == "provider-order"
        )
        assert approve(workspace, q)["status"] == "ordered"
        assert seen.count(("POST", "/v2/number_orders")) == 1
    finally:
        workspace["client"].portal.call(http.aclose)
