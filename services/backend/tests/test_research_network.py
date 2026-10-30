"""Research must never fetch local services, including through rebinding/redirects."""

import socket

import httpx
import pytest
from app.research.fetch import SafeFetcher, UnsafeURL


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "100.64.0.1"]
)
async def test_private_research_addresses_are_rejected(monkeypatch, address):
    real = httpx.AsyncClient
    seen = []

    def dns(*args, **kwargs):
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
        ]

    def response(request):
        seen.append(request)
        return httpx.Response(200, text="private network data", headers={"content-type": "text/plain"})

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(
        "app.research.fetch.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(response), **kw),
    )
    with pytest.raises(UnsafeURL, match="public"):
        await SafeFetcher(allowed_domains={"example.test"}).fetch("https://example.test/")
    assert not seen


async def test_research_pins_the_public_address_and_rechecks_redirects(monkeypatch):
    real = httpx.AsyncClient
    resolved, seen = [], []

    def dns(host, *args, **kwargs):
        resolved.append(host)
        address = "93.184.215.14" if len(resolved) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    def response(request):
        seen.append(request)
        assert request.url.host == "93.184.215.14"
        assert request.headers["Host"] == "example.test"
        assert request.extensions["sni_hostname"] == "example.test"
        return httpx.Response(302, headers={"location": "/again"})

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(
        "app.research.fetch.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(response), **kw),
    )
    with pytest.raises(UnsafeURL, match="public"):
        await SafeFetcher(allowed_domains={"example.test"}).fetch("https://example.test/")
    assert len(seen) == 1 and len(resolved) == 2
