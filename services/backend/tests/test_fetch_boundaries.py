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


def serve(monkeypatch, handler):
    original = httpx.AsyncClient
    clients = []

    def factory(**kwargs):
        client = original(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(fetch.httpx, "AsyncClient", factory)
    return clients


@pytest.mark.parametrize(
    "address",
    [
        "64:ff9b::7f00:1",  # NAT64 well-known prefix embedding loopback; is_global is True on 3.12
        "64:ff9b::808:808",  # NAT64 always routes through a translator, never a direct origin
        "64:ff9b:1::a00:1",  # Local-use NAT64
        "::7f00:1",  # Deprecated IPv4-compatible; is_global is True on 3.12
        "::ffff:8.8.8.8",  # IPv4-mapped; the embedded address decides, so deny the form
        "::ffff:127.0.0.1",
        "2002:7f00:1::1",  # 6to4 embedding loopback
        "2001::1",  # Teredo
        "ff02::1",  # Multicast; is_global is True on 3.12
        "100.64.0.1",  # Shared address space
    ],
)
def test_translated_embedded_and_non_global_addresses_are_denied(address):
    with pytest.raises(fetch.UnsafeURL):
        fetch.validate_url("https://example.test/", resolver=lambda host: [address])


def test_ipv6_literal_destinations_are_checked_and_public_ipv6_is_allowed():
    with pytest.raises(fetch.UnsafeURL):
        fetch.validate_url("https://[64:ff9b::7f00:1]/")
    assert fetch.validate_url("https://example.test/", resolver=lambda host: ["2606:4700::6810:84e5"])


@pytest.mark.asyncio
async def test_pinned_ipv6_destination_is_bracketed_with_original_host_and_sni(monkeypatch):
    monkeypatch.setattr(fetch, "validate_url", lambda url: ["2606:4700::6810:84e5"])
    seen = []

    def reply(request):
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="Official")

    serve(monkeypatch, reply)
    await fetch.SafeFetcher(allowed_domains={"example.test"}).fetch("https://example.test/about")
    assert str(seen[0].url) == "https://[2606:4700::6810:84e5]/about"
    assert seen[0].headers["host"] == "example.test"
    assert seen[0].extensions["sni_hostname"] == b"example.test"


@pytest.mark.asyncio
async def test_slow_drip_source_stops_at_the_total_fetch_deadline(monkeypatch):
    import asyncio

    monkeypatch.setattr(fetch, "validate_url", lambda url: ["93.184.216.34"])

    async def drip():
        for _ in range(1000):
            await asyncio.sleep(0.05)
            yield b"x"

    serve(
        monkeypatch,
        lambda request: httpx.Response(200, headers={"content-type": "text/plain"}, content=drip()),
    )
    fetcher = fetch.SafeFetcher(deadline=0.3)
    with pytest.raises(fetch.UnsafeURL, match="deadline"):
        await asyncio.wait_for(fetcher.fetch("https://example.test/"), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start,target,allowed",
    [
        ("https://example.test/", "https://www.example.test/", True),
        ("https://www.example.test/", "https://example.test/home", True),
        ("https://example.test/", "https://shop.example.test/", False),
        ("https://example.test/", "https://www.other.test/", False),
    ],
)
async def test_root_and_www_redirects_follow_one_explicit_rule(monkeypatch, start, target, allowed):
    from urllib.parse import urlsplit

    monkeypatch.setattr(fetch, "validate_url", lambda url: ["93.184.216.34"])

    def reply(request):
        if request.headers["host"] == urlsplit(start).hostname:
            return httpx.Response(301, headers={"location": target})
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="Official")

    serve(monkeypatch, reply)
    fetcher = fetch.SafeFetcher(allowed_domains={urlsplit(start).hostname})
    if allowed:
        assert (await fetcher.fetch(start))["url"] == target
    else:
        with pytest.raises(fetch.UnsafeURL):
            await fetcher.fetch(start)


@pytest.mark.asyncio
async def test_pinned_connections_are_never_pooled_across_hosts(monkeypatch):
    """Two names on one address must not share a TLS connection negotiated for the first name."""
    monkeypatch.setattr(fetch, "validate_url", lambda url: ["93.184.216.34"])
    hosts = []

    def reply(request):
        hosts.append(request.headers["host"])
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="Official")

    clients = serve(monkeypatch, reply)
    fetcher = fetch.SafeFetcher()
    for host in ("first.example.test", "second.example.test"):
        await fetcher.fetch(f"https://{host}/")
    assert hosts == ["first.example.test", "second.example.test"]
    assert len(clients) == 2 and all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_source_html_is_parsed_off_the_event_loop(monkeypatch):
    import threading

    loop_thread, parsed = threading.get_ident(), []
    original = fetch.BeautifulSoup

    def soup(*args, **kwargs):
        parsed.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(fetch, "BeautifulSoup", soup)
    monkeypatch.setattr(fetch, "validate_url", lambda url: ["93.184.216.34"])
    serve(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html"}, text="<title>A</title><p>Official</p>"
        ),
    )
    assert (await fetch.SafeFetcher().fetch("https://example.test/"))["text"] == "A Official"
    assert parsed and loop_thread not in parsed
