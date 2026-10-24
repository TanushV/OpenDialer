"""Owned-number operations through HTTP with simulation off and all carrier IO mocked."""

import httpx
import pytest
from app.providers.telnyx import TelnyxCarrier


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
        "call_recording_enabled": False,
    }
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/v2/phone_numbers":
            return httpx.Response(200, json={"data": [number], "meta": {"page_number": 1, "total_pages": 1}})
        if request.method == "PATCH":
            if result == "rejected":
                return httpx.Response(422, json={"errors": [{"title": "Invalid route"}]})
            number["connection_id"] = "test-route"
            if result == "timeout":
                raise httpx.ReadTimeout("Response lost after submission", request=request)
        return httpx.Response(200, json={"data": number})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        service, "carrier", lambda tenant: TelnyxCarrier("synthetic", "test-route", http=http)
    )
    client, headers = workspace["client"], workspace["headers"]

    def request(method, path, expected=200, **kwargs):
        response = client.request(method, path, headers=headers, **kwargs)
        assert response.status_code == expected, response.text
        return response.json()

    try:
        assert request("POST", "/v1/admin/numbers/sync")["added"] == 1
        items = request("GET", "/v1/workspace/numbers")["items"]
        row = next(n for n in items if n["phone"] == number["phone_number"])
        path = f"/v1/admin/numbers/{row['id']}/configure"
        receipt = request("POST", path, {"success": 200, "timeout": 503, "rejected": 422}[result], json={})
        if result == "timeout":
            # Repeating the operation verifies the same submitted intent, without a second PATCH.
            patches = sum(method == "PATCH" for method, _ in seen)
            receipt = request("POST", path, json={})
            assert sum(method == "PATCH" for method, _ in seen) == patches == 1
        if result != "rejected":
            assert receipt["configuration_status"] == "confirmed"
            assert receipt["provider_recording_enabled"] is False
            assert receipt["identity_configured"] is True and receipt["callback_tested"] is False
            before = len(seen)
            assert request("POST", path, json={})["configuration_status"] == "confirmed"
            assert len(seen) == before
        else:
            assert "Invalid route" in str(receipt)
            row = next(n for n in request("GET", "/v1/workspace/numbers")["items"] if n["id"] == row["id"])
            assert row["configuration_status"] == "failed" and row["identity_configured"] is False
    finally:
        client.portal.call(http.aclose)
