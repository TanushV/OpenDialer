"""R9B.16: Treg's call route carries the idempotency key and cost cap, reads what each call cost and finishes Wiza jobs."""

import json

import httpx
from app.providers.treg import Treg


async def nosleep(_):
    return None


async def test_a_call_carries_its_key_and_cap_and_reports_the_charge():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200, json={"data": {"found": True}}, headers={"X-Treg-Cost-Micro": "5000", "X-Treg-Call-Id": "c1"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        answer = await Treg("tok", http=http).call(
            "leadocean.people.phone.find", "GET", {"linkedin_url": "u"}, 5000, "k1"
        )
    request = seen[0]
    assert (
        request.url.path == "/call/leadocean.people.phone.find" and request.url.params["linkedin_url"] == "u"
    )
    assert (request.headers["Idempotency-Key"], request.headers["X-Treg-Route-Max-Cost"]) == (
        "k1",
        "0.005000",
    )
    assert request.headers["X-Treg-Token"] == "tok"
    assert answer["_treg"] == {"http_status": 200, "call_id": "c1", "charged_micro": 5000}


async def test_a_refused_relay_costs_nothing():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(402, json={"error": "fair use"}))
    ) as http:
        answer = await Treg("tok", http=http).call("aiark.people.phone.find", "POST", {}, 1, "k")
    assert answer["_treg"] == {"http_status": 402, "charged_micro": 0}


async def test_a_wiza_job_is_polled_for_free_until_it_finishes():
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("wiza.people.phone.find"):
            assert json.loads(request.content)["enrichment_level"] == "phone"
            return httpx.Response(
                200,
                json={"data": {"id": 7, "status": "queued"}},
                headers={"X-Treg-Cost-Micro": "150000", "X-Treg-Async": "{}"},
            )
        if len(calls) < 3:
            return httpx.Response(200, json={"data": {"status": "resolving"}})
        return httpx.Response(
            200, json={"data": {"status": "finished", "phones": [{"number": "+12125550101"}]}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        answer = await Treg("tok", http=http, sleep=nosleep).call(
            "wiza.people.phone.find", "POST", {"enrichment_level": "phone"}, 150000, "k"
        )
    assert calls.count("/call/wiza.people.reveal.get") == 2
    assert answer["_treg"]["charged_micro"] == 150000
    assert answer["result"]["data"]["phones"][0]["number"] == "+12125550101"
