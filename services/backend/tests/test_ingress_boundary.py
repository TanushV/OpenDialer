"""E12/C047: explicitly authorized content-free ingress checks, never arbitrary probes."""

import asyncio
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


@pytest.mark.parametrize("boundary", ["current", "expiry", "changed_source", "revoked_role", "replay"])
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
        assert (await asyncio.to_thread(c.post, parsed.path)).status_code in {404, 405}
        if boundary == "expiry":
            clock.now += timedelta(seconds=61)
        response = await asyncio.to_thread(c.get, parsed.path, headers={"Host": parsed.hostname})
        assert response.status_code == (404 if boundary == "expiry" else 200)
        assert not any(
            secret in response.text for secret in [workspace["access_token"], "seller_id", "database"]
        )
        if boundary == "changed_source":
            service.vault.write(
                workspace["tenant_id"], {"webhook_url": "https://replacement.example.test/webhooks/telnyx"}
            )
        if boundary == "revoked_role":
            from app.db import get, update

            with service.db.transaction() as s:
                update(get(s, "users", workspace["user_id"]), role="rep")
        if boundary == "replay":
            assert (await asyncio.to_thread(c.get, parsed.path)).status_code == 404
        return {"url": url, "title": "", "text": response.text}

    monkeypatch.setattr(SafeFetcher, "fetch", challenge)
    response = c.post(
        "/v1/admin/ingress-check",
        headers=h,
        json={"request_id": str(uuid4()), "expected_version": saved.json().get("version", 1)},
    )
    assert len(requests) == 1, "One route proof must use the bounded SSRF-safe fetch path"
    if boundary in {"current", "replay"}:
        assert response.status_code == 200, response.text
        assert response.json()["routing"] == "software_verified"
        assert response.json()["last_real_provider_event"] is None
    else:
        assert response.status_code in {403, 409, 502, 503}
    assert c.get(urlsplit(requests[0]).path).status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/webhooks/telnyx",
        "https://public.example.test/health",
        "https://public.example.test/webhooks/telnyx?key=value",
        "https://public.example.test:bad/webhooks/telnyx",
    ],
)
def test_probe_rejects_unsafe_saved_endpoint_without_network(workspace, monkeypatch, url):
    from app.research.fetch import SafeFetcher

    async def forbidden(*args):
        pytest.fail("Invalid endpoint must fail before network admission")

    monkeypatch.setattr(SafeFetcher, "fetch", forbidden)
    c, h = workspace["client"], workspace["headers"]
    saved = c.put("/v1/admin/settings", headers=h, json={"webhook_url": url})
    if saved.status_code == 200:
        response = c.post(
            "/v1/admin/ingress-check",
            headers=h,
            json={"request_id": str(uuid4()), "expected_version": saved.json().get("version", 1)},
        )
        assert response.status_code == 422
    else:
        assert saved.status_code == 422


def test_ingress_network_failure_is_actionable_and_closes_challenge(workspace, app, monkeypatch):
    import httpx
    from app.research.fetch import SafeFetcher

    async def disconnected(*args):
        raise httpx.ConnectError("Synthetic disconnect with provider-private response text")

    monkeypatch.setattr(SafeFetcher, "fetch", disconnected)
    c, h = workspace["client"], workspace["headers"]
    saved = c.put(
        "/v1/admin/settings", headers=h, json={"webhook_url": "https://owned.example.test/webhooks/telnyx"}
    )
    response = c.post(
        "/v1/admin/ingress-check",
        headers=h,
        json={"request_id": str(uuid4()), "expected_version": saved.json()["version"]},
    )
    assert response.status_code == 502
    assert "provider-private" not in response.text
    assert app.state.service.ingress_challenges == {}
    from test_remediation_privacy import principal

    with app.state.db.transaction(False) as s:
        attempts = [
            r
            for r in app.state.service.scoped(s, "outcomes", principal(workspace))
            if r.data.get("kind") == "ingress_check"
        ]
        assert len(attempts) == 1 and attempts[0].data["status"] == "failed"


def test_route_proof_response_replays_without_another_network_request(workspace, monkeypatch):
    from urllib.parse import urlsplit

    from app.research.fetch import SafeFetcher

    c, h = workspace["client"], workspace["headers"]
    calls = []

    async def fetch(fetcher, url):
        calls.append(url)
        response = await asyncio.to_thread(c.get, urlsplit(url).path)
        return {"text": response.text}

    monkeypatch.setattr(SafeFetcher, "fetch", fetch)
    saved = c.put(
        "/v1/admin/settings", headers=h, json={"webhook_url": "https://owned.example.test/webhooks/telnyx"}
    )
    body = {"request_id": str(uuid4()), "expected_version": saved.json()["version"]}
    first = c.post("/v1/admin/ingress-check", headers=h, json=body)
    assert first.status_code == 200
    assert c.post("/v1/admin/ingress-check", headers=h, json=body).json() == first.json()
    assert len(calls) == 1
    assert (
        c.post(
            "/v1/admin/ingress-check",
            headers=h,
            json={**body, "expected_version": body["expected_version"] + 1},
        ).status_code
        == 409
    )


def test_consumed_challenge_retains_admission_and_rejected_probe_has_no_pending_intent(workspace, app):
    from fastapi import HTTPException
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    service.vault.write(p["tenant_id"], {"webhook_url": "https://owned.example.test/webhooks/telnyx"})
    first = service._ingress_start(p, {"request_id": str(uuid4()), "expected_version": 0})
    assert service.ingress_challenge(first["nonce"]).endswith(first["nonce"])
    with pytest.raises(HTTPException) as duplicate:
        service.ingress_challenge(first["nonce"])
    assert duplicate.value.status_code == 404
    with pytest.raises(HTTPException) as denied:
        service._ingress_start(p, {"request_id": str(uuid4()), "expected_version": 0})
    assert denied.value.status_code == 409
    with service.db.transaction(False) as s:
        pending = [r for r in service.scoped(s, "outcomes", p)
                   if r.data.get("kind") == "ingress_check" and r.data["status"] == "pending"]
        assert [r.id for r in pending] == [first["id"]]
