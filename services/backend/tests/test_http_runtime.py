"""A09/A25/A50: owned bounded HTTP pools do not let optional work starve controls."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient


def test_authority_lifetime_owns_distinct_pools_and_closes_all_of_them(app):
    service = app.state.service
    with TestClient(app):
        clients = [getattr(service, name, None) for name in ("control_http", "coach_http", "background_http")]
        assert all(isinstance(client, httpx.AsyncClient) for client in clients)
        assert len({id(client) for client in clients}) == 3
        assert not any(client.is_closed for client in clients)
    assert all(client.is_closed for client in clients)


def test_provider_factories_reuse_owned_pools_without_sharing_authorization(workspace, app):
    service = app.state.service
    service.settings.simulation = False
    for tenant in ("first-tenant", "second-tenant"):
        service.vault.write(
            tenant,
            {
                "telnyx_api_key": tenant + "-carrier-test",
                "telnyx_connection_id": tenant + "-connection",
                "openai_api_key": tenant + "-inference-test",
            },
        )
    carriers = [service.carrier(tenant) for tenant in ("first-tenant", "second-tenant")]
    inference = [service.inference(tenant, "script_write") for tenant in ("first-tenant", "second-tenant")]
    assert carriers[0].http is not None and carriers[0].http is carriers[1].http
    assert inference[0].http is not None and inference[0].http is inference[1].http
    assert len({id(carriers[0].http), id(inference[0].http), id(service.coach_http)}) == 3
    assert carriers[0].api_key != carriers[1].api_key
    assert inference[0].api_key != inference[1].api_key
    assert not carriers[0].http.headers.get("Authorization")
    assert not inference[0].http.headers.get("Authorization")


@pytest.mark.asyncio
async def test_pooled_carrier_requests_keep_per_tenant_credentials_without_new_clients(
    workspace, app, monkeypatch
):
    service = app.state.service
    service.settings.simulation = False
    seen = []

    async def respond(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"data": {"token": "scoped-telephony-token"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service.control_http = client
        for tenant in ("alpha", "beta"):
            service.vault.write(tenant, {"telnyx_api_key": tenant, "telnyx_connection_id": tenant})
        carriers = [service.carrier(tenant) for tenant in ("alpha", "beta")]
        assert all(carrier.http is client for carrier in carriers)

        def forbidden(*args, **kwargs):
            raise AssertionError("Provider constructed a per-request client")

        monkeypatch.setattr(httpx, "AsyncClient", forbidden)
        await asyncio.gather(*(carrier.request("GET", "/telephony_credentials") for carrier in carriers))
    assert sorted(seen) == ["Bearer alpha", "Bearer beta"]


@pytest.mark.asyncio
async def test_provider_pools_do_not_retain_cookies_from_other_accounts(app, monkeypatch):
    original = httpx.AsyncClient
    seen = []

    def respond(request):
        seen.append(request.headers.get("Cookie"))
        return httpx.Response(200, headers={"Set-Cookie": "private_session=tenant-one; Path=/"}, json={})

    def client(**kwargs):
        return original(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    async with app.router.lifespan_context(app):
        pool = app.state.service.coach_http
        await pool.get("https://api.example.test/resource")
        await pool.get("https://api.example.test/resource")
        assert not list(pool.cookies.items())
    assert seen == [None, None]


@pytest.mark.asyncio
async def test_background_pool_saturation_does_not_delay_control_requests(app):
    release, admitted = asyncio.Event(), asyncio.Event()
    waiting = []
    handlers = set()

    async def handle(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            if b"/background" in request:
                waiting.append(1)
                if len(waiting) == 4:
                    admitted.set()
                await release.wait()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    pending = []
    try:
        async with app.router.lifespan_context(app):
            background = getattr(app.state.service, "background_http", None)
            control = getattr(app.state.service, "control_http", None)
            assert background is not None and control is not None
            try:
                pending = [asyncio.create_task(background.get(url + "/background")) for _ in range(4)]
                await asyncio.wait_for(admitted.wait(), 1)
                with pytest.raises(httpx.PoolTimeout):
                    await background.get(url + "/background", timeout=httpx.Timeout(1, pool=0.02))
                result = await asyncio.wait_for(control.get(url + "/control"), 0.2)
                assert result.status_code == 200
                assert len(waiting) == 4
            finally:
                release.set()
                await asyncio.gather(*pending, return_exceptions=True)
    finally:
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
