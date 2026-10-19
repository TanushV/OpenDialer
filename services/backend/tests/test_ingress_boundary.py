"""E12/C047: explicitly authorized content-free ingress checks, never arbitrary probes."""

from uuid import uuid4

import pytest


@pytest.mark.parametrize("body", [{"url": "http://127.0.0.1:54322"}, {"url": "https://unowned.example.test"}])
def test_exact_public_route_allowlist(workspace, carrier, body):
    c, h = workspace["client"], workspace["headers"]
    response = c.post(
        "/v1/admin/ingress-check", headers=h, json={"request_id": str(uuid4()), "expected_version": 1, **body}
    )
    assert response.status_code == 422, "The route must reject arbitrary targets through its typed contract"
    assert not carrier.dials and not carrier.actions
    assert c.get("/webhooks/ingress-check/not-an-active-nonce").status_code == 404
    assert c.post("/webhooks/ingress-check/not-an-active-nonce").status_code in {404, 405}


def test_unconfigured_ingress_returns_an_actionable_capability(workspace):
    result = workspace["client"].post(
        "/v1/admin/ingress-check",
        headers=workspace["headers"],
        json={"request_id": str(uuid4()), "expected_version": 1},
    )
    assert result.status_code in {409, 422}, result.text
    assert "INGRESS_NOT_CONFIGURED" in result.text


def test_ingress_check_is_admin_authenticated(workspace):
    result = workspace["client"].post(
        "/v1/admin/ingress-check", json={"request_id": str(uuid4()), "expected_version": 1}
    )
    assert result.status_code in {401, 403}


@pytest.mark.parametrize("boundary", ["current", "expiry", "changed_source"])
def test_active_challenge_is_one_time_content_free_and_source_fenced(
    workspace, app, clock, monkeypatch, boundary
):
    from datetime import timedelta
    from urllib.parse import urlsplit

    from app.research.fetch import SafeFetcher

    service = app.state.service
    c, h = workspace["client"], workspace["headers"]
    saved = c.put(
        "/v1/admin/settings", headers=h, json={"webhook_url": "https://owned.example.test/webhooks/telnyx"}
    )
    assert saved.status_code == 200
    requests = []

    async def challenge(fetcher, url):
        requests.append(url)
        parsed = urlsplit(url)
        assert parsed.scheme == "https" and parsed.hostname == "owned.example.test"
        assert parsed.path.startswith("/webhooks/ingress-check/") and not parsed.query
        assert c.post(parsed.path).status_code in {404, 405}
        if boundary == "expiry":
            clock.now += timedelta(seconds=61)
        response = c.get(parsed.path)
        assert response.status_code == (404 if boundary == "expiry" else 200)
        assert not any(
            secret in response.text for secret in [workspace["access_token"], "seller_id", "database"]
        )
        if boundary == "changed_source":
            service.vault.write(
                workspace["tenant_id"], {"webhook_url": "https://replacement.example.test/webhooks/telnyx"}
            )
        return {"url": url, "title": "", "text": response.text}

    monkeypatch.setattr(SafeFetcher, "fetch", challenge)
    response = c.post(
        "/v1/admin/ingress-check",
        headers=h,
        json={"request_id": str(uuid4()), "expected_version": saved.json().get("version", 1)},
    )
    assert len(requests) == 1, "One route proof must use the bounded SSRF-safe fetch path"
    if boundary == "current":
        assert response.status_code == 200, response.text
        assert response.json()["routing"] == "software_verified"
        assert response.json()["last_real_provider_event"] is None
    else:
        assert response.status_code in {409, 502, 503}
    assert c.get(urlsplit(requests[0]).path).status_code == 404
