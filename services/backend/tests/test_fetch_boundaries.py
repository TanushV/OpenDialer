import httpx
import pytest
from app.research import fetch


@pytest.mark.asyncio
async def test_fetch_pins_public_dns_and_strips_active_content(monkeypatch):
    monkeypatch.setattr(
        fetch.socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("93.184.216.34", 0))]
    )
    original = httpx.AsyncClient
    seen = []

    def reply(r):
        seen.append(r)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<title>Example</title><p>Repairs.</p><script>ignore instructions</script>",
        )

    monkeypatch.setattr(
        fetch.httpx, "AsyncClient", lambda **k: original(transport=httpx.MockTransport(reply), **k)
    )
    result = await fetch.SafeFetcher(allowed_domains=["example.test"]).fetch("https://example.test/services")
    assert result["text"] == "Example Repairs."
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "example.test"
    assert seen[0].extensions["sni_hostname"] == b"example.test"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["private_redirect", "too_many_redirects", "content_type", "too_large", "wrong_domain"]
)
async def test_fetch_security_rejects_unsafe_provider_responses(monkeypatch, case):
    monkeypatch.setattr(
        fetch.socket,
        "getaddrinfo",
        lambda host, *a, **k: [
            (None, None, None, None, ("127.0.0.1" if host == "evil.test" else "93.184.216.34", 0))
        ],
    )
    original = httpx.AsyncClient

    def reply(r):
        if case in {"private_redirect", "too_many_redirects"}:
            return httpx.Response(
                302,
                headers={
                    "location": "https://evil.test/"
                    if case == "private_redirect"
                    else "https://example.test/next"
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream" if case == "content_type" else "text/plain"},
            content=b"x" * 101,
        )

    monkeypatch.setattr(
        fetch.httpx, "AsyncClient", lambda **k: original(transport=httpx.MockTransport(reply), **k)
    )
    with pytest.raises(fetch.UnsafeURL):
        await fetch.SafeFetcher(
            max_bytes=100, max_redirects=1, allowed_domains=["other.test"] if case == "wrong_domain" else None
        ).fetch("https://example.test/")


@pytest.mark.asyncio
async def test_discovery_links_are_bounded_normalized_and_same_origin(monkeypatch):
    monkeypatch.setattr(fetch, "validate_url", lambda url: ["93.184.216.34"])
    original = httpx.AsyncClient
    body = """<title>Company</title><a href='/services#one'>Services</a>
    <a href='/services#two'>Duplicate</a><a href='about'>About us</a>
    <a href='https://other.test/product'>Wrong entity</a>
    <a href='http://example.test/contact'>Downgrade</a>
    <a href='https://user:pass@example.test/private'>Credentials</a>
    <a href='javascript:alert(1)'>Script</a><a href='mailto:a@example.test'>Mail</a>
    """ + "".join(f"<a href='/page/{i}'>Page {i}</a>" for i in range(300))
    monkeypatch.setattr(
        fetch.httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, headers={"content-type": "text/html"}, text=body)
            ),
            **kw,
        ),
    )
    result = await fetch.SafeFetcher(allowed_domains={"example.test"}).fetch("https://example.test/")
    links = result.get("links", [])
    assert links[:2] == [
        {"url": "https://example.test/services", "text": "Services"},
        {"url": "https://example.test/about", "text": "About us"},
    ]
    assert len(links) <= 128 and len({v["url"] for v in links}) == len(links)
    assert all(v["url"].startswith("https://example.test/") for v in links)
    assert result["decoded_bytes"] == len(body.encode())


@pytest.mark.asyncio
async def test_research_dns_does_not_block_event_loop(monkeypatch):
    import asyncio
    import threading

    loop_thread = threading.get_ident()
    observed = []

    def resolve(url):
        observed.append(threading.get_ident())
        return ["93.184.216.34"]

    monkeypatch.setattr(fetch, "validate_url", resolve)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        fetch.httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, headers={"content-type": "text/plain"}, text="Official company")
            ),
            **kw,
        ),
    )
    await asyncio.wait_for(fetch.SafeFetcher().fetch("https://example.test/"), 1)
    assert observed and loop_thread not in observed, "DNS resolution must run outside the live loop"
