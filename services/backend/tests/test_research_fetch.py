"""First-party fetch limits with in-memory HTTP responses, never public requests."""

from unittest.mock import AsyncMock

import httpx
import pytest
from app.research.fetch import SafeFetcher, UnsafeURL


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr("app.research.fetch.public_address", AsyncMock(return_value="93.184.215.14"))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["foreign_redirect", "redirect_loop", "oversize", "unsupported"])
async def test_research_fetch_stops_at_its_identity_and_resource_limits(monkeypatch, kind):
    seen = []
    client_type = httpx.AsyncClient

    def handler(request):
        seen.append(str(request.url))
        if kind == "foreign_redirect":
            return httpx.Response(302, headers={"location": "https://other.test/"})
        if kind == "redirect_loop":
            return httpx.Response(302, headers={"location": "/again"})
        if kind == "oversize":
            return httpx.Response(200, text="x" * 101, headers={"content-type": "text/plain"})
        return httpx.Response(200, content=b"binary", headers={"content-type": "application/octet-stream"})

    monkeypatch.setattr(
        "app.research.fetch.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    fetcher = SafeFetcher(max_bytes=100, max_redirects=2, allowed_domains={"example.test"})
    with pytest.raises(
        UnsafeURL,
        match={
            "foreign_redirect": "allowlisted",
            "redirect_loop": "redirect budget",
            "oversize": "too large",
            "unsupported": "content type",
        }[kind],
    ):
        await fetcher.fetch("https://example.test/")
    assert len(seen) == (3 if kind == "redirect_loop" else 1)


@pytest.mark.asyncio
async def test_research_follows_www_redirect_and_strips_page_code(monkeypatch):
    client_type = httpx.AsyncClient
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(301, headers={"location": "https://www.example.test/about"})
        return httpx.Response(
            200,
            text="<title>Example</title><p>Public fact</p><script>hidden code</script>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    monkeypatch.setattr(
        "app.research.fetch.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    page = await SafeFetcher(allowed_domains={"example.test"}).fetch("https://example.test/")
    assert page["url"] == "https://www.example.test/about"
    assert page["title"] == "Example" and "Public fact" in page["text"]
    assert "hidden code" not in page["text"] and len(seen) == 2
