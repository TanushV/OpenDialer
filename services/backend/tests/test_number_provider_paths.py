"""Owned-number operations through HTTP with simulation off and all carrier IO mocked."""

import json

import httpx
import pytest
from app import ops
from app.providers.telnyx import TelnyxCarrier
from helpers import bound


@pytest.mark.parametrize("result", ["success", "timeout", "rejected"])
def test_number_configuration_confirms_reconciles_or_reports_rejection(workspace, app, monkeypatch, result):
    service = app.state.service
    service.settings.simulation = False
    service.configs.write(workspace["tenant_id"], {"telnyx_connection_id": "test-route"})
    number = {
        "id": "test-owned-number",
        "phone_number": "+14155550999",
        "connection_id": None,
        "status": "active",
    }
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/v2/phone_numbers":
            return httpx.Response(200, json={"data": [number], "meta": {"page_number": 1, "total_pages": 1}})
        if request.method == "GET" and request.url.path.endswith("/voice"):
            return httpx.Response(
                200, json={"data": {"call_recording": {"inbound_call_recording_enabled": False}}}
            )
        if request.method == "PATCH":
            if result == "rejected":
                return httpx.Response(422, json={"errors": [{"title": "Invalid route"}]})
            number["connection_id"] = "test-route"
            if result == "timeout":
                raise httpx.ReadTimeout("Response lost after submission", request=request)
        return httpx.Response(200, json={"data": number})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        ops, "carrier", bound(lambda tenant: TelnyxCarrier("synthetic", "test-route", http=http))
    )
    client, headers = workspace["client"], workspace["headers"]

    def request(method, path, expected=200, **kwargs):
        response = client.request(method, path, headers=headers, **kwargs)
        assert response.status_code == expected, response.text
        return response.json()

    try:
        assert request("POST", "/v1/admin/numbers/sync")["added"] == 0
        receipt = request(
            "POST",
            "/v1/admin/numbers/add",
            {"success": 200, "timeout": 503, "rejected": 422}[result],
            json={"phone": number["phone_number"]},
        )
        items = request("GET", "/v1/workspace/numbers")["items"]
        row = next(n for n in items if n["phone"] == number["phone_number"])
        path = f"/v1/admin/numbers/{row['id']}/configure"
        if result == "timeout":
            # Repeating the operation verifies the same submitted intent, without a second PATCH.
            patches = sum(method == "PATCH" for method, _ in seen)
            receipt = request("POST", path, json={})
            assert sum(method == "PATCH" for method, _ in seen) == patches == 1
        if result != "rejected":
            assert receipt["configuration_status"] == "confirmed"
            assert receipt["provider_recording_enabled"] is False
            assert receipt["identity_configured"] is True and receipt["lifecycle"] == "limited_real_use"
            before = len(seen)
            assert request("POST", path, json={})["configuration_status"] == "confirmed"
            assert len(seen) == before
        else:
            assert "Invalid route" in str(receipt)
            row = next(n for n in request("GET", "/v1/workspace/numbers")["items"] if n["id"] == row["id"])
            assert row["configuration_status"] == "failed" and row["identity_configured"] is False
    finally:
        client.portal.call(http.aclose)


def test_shared_webhook_sync_lists_every_owned_number_with_its_route(workspace, app, monkeypatch):
    from time import monotonic

    service = app.state.service
    service.settings.simulation = False
    service.configs.write(workspace["tenant_id"], {"telnyx_connection_id": "test-route"})
    service.managed.config = {"tenant_id": workspace["tenant_id"], "workspace_id": "test"}
    service.managed.last_exchange, service.managed.caught_up = monotonic(), True
    owned = [
        {"id": "unrouted", "phone_number": "+14155550901", "connection_id": None, "status": "active"},
        {"id": "ours", "phone_number": "+14155550902", "connection_id": "test-route", "status": "active"},
        {
            "id": "elsewhere",
            "phone_number": "+14155550903",
            "connection_id": "another-app",
            "status": "active",
        },
    ]

    def handler(request):
        if request.url.path == "/v2/phone_numbers":
            return httpx.Response(200, json={"data": owned, "meta": {"page_number": 1, "total_pages": 1}})
        return httpx.Response(
            200, json={"data": {"call_recording": {"inbound_call_recording_enabled": False}}}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        ops, "carrier", bound(lambda tenant: TelnyxCarrier("synthetic", "test-route", http=http))
    )
    try:
        response = workspace["client"].post("/v1/admin/numbers/sync", headers=workspace["headers"])
        assert response.status_code == 200, response.text
        assert {n["phone"]: n["route"] for n in response.json()["owned"]} == {
            "+14155550901": "unassigned",
            "+14155550902": "this_app",
            "+14155550903": "other_app",
        }
        assert response.json()["added"] == 0
    finally:
        workspace["client"].portal.call(http.aclose)


def test_caller_name_lists_outbound_cnam_only_and_records_the_read_back(workspace, app, monkeypatch):
    service = app.state.service
    service.settings.simulation = False
    sent = []
    stored = {}

    def handler(request):
        if request.method == "PATCH":
            body = json.loads(request.content)
            sent.append(body)
            stored.update(body["cnam_listing"])
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(200, json={"data": {"cnam_listing": stored}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ops, "carrier", bound(lambda tenant: TelnyxCarrier("synthetic", "route", http=http)))
    client, headers = workspace["client"], workspace["headers"]
    number = next(n for n in client.get("/v1/workspace/numbers", headers=headers).json()["items"])
    from app.platform.db import get as get_row
    from app.platform.db import update

    with service.db.transaction() as s:
        update(
            get_row(s, "number_inventory", number["id"], workspace["tenant_id"]),
            provider_id="telnyx-number-1",
        )
    path = "/v1/admin/caller-name"
    try:
        assert client.put(path, headers=headers, json={"name": "Too long a business name"}).status_code == 422
        assert client.put(path, headers=headers, json={"name": "Agite; DROP"}).status_code == 422
        response = client.put(path, headers=headers, json={"name": "Agite  Systems"})
        assert response.status_code == 200, response.text
        assert response.json() == {"name": "AGITE SYSTEMS", "failed": 0}
        listed = next(
            n
            for n in client.get("/v1/workspace/numbers", headers=headers).json()["items"]
            if n["id"] == number["id"]
        )
        assert listed["caller_name"]["listed"] == "AGITE SYSTEMS"
        assert listed["caller_name"]["status"] == "submitted"
        assert sent == [
            {"cnam_listing": {"cnam_listing_enabled": True, "cnam_listing_details": "AGITE SYSTEMS"}}
        ]
        assert "caller_id_name_enabled" not in str(sent)
    finally:
        client.portal.call(http.aclose)
