import asyncio
import ipaddress
import socket
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

# Total wall time for one source, including redirects and the body; a slow-drip
# origin cannot hold a research unit for longer than this.
FETCH_DEADLINE_SECONDS = 20


class UnsafeURL(ValueError):
    pass


async def public_address(host, port):
    try:
        results = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
        addresses = {ipaddress.ip_address(row[4][0]) for row in results}
    except (OSError, ValueError):
        raise UnsafeURL("Source must resolve to a public network address") from None
    if not addresses or any(not address.is_global or address.is_multicast for address in addresses):
        raise UnsafeURL("Source must resolve only to public network addresses")
    return str(min(addresses, key=lambda address: (address.version, int(address))))


def validate_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 80, 443}
    ):
        raise UnsafeURL("Unsupported source URL")


def same_site(host, allowed):
    """The imported domain and its www name are one identity; other subdomains are not."""
    host = (host or "").lower().rstrip(".")
    return any(host in {domain, "www." + domain} or "www." + host == domain for domain in allowed)


def parse_page(url, text, size):
    from bs4 import BeautifulSoup  # Loaded with the first research page, not at authority startup.

    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return {
        "url": url,
        "title": soup.title.get_text() if soup.title else "",
        "text": soup.get_text(" ", strip=True)[:50000],
        "links": official_links(url, soup),
        "decoded_bytes": size,
    }


class SafeFetcher:
    def __init__(
        self, max_bytes=1_000_000, max_redirects=3, allowed_domains=None, *, deadline=FETCH_DEADLINE_SECONDS
    ):
        self.max_bytes, self.max_redirects = max_bytes, max_redirects
        self.allowed_domains = set(allowed_domains or [])
        self.deadline = deadline
        self.decoded_bytes_total = 0

    async def fetch(self, url):
        try:
            async with asyncio.timeout(self.deadline):
                return await self._fetch(url)
        except TimeoutError as exc:
            raise UnsafeURL("Source exceeded its total fetch deadline") from exc

    async def _fetch(self, url):
        for _ in range(self.max_redirects + 1):
            parsed = urlsplit(url)
            if self.allowed_domains and not same_site(parsed.hostname, self.allowed_domains):
                raise UnsafeURL("Source domain is not allowlisted")
            validate_url(url)
            address = await public_address(
                parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
            )
            # Connect to exactly the vetted IP, retaining the original HTTP/TLS identity.
            # A second DNS lookup would reopen the private-network boundary through rebinding.
            target = httpx.URL(url).copy_with(host=address)
            async with (
                httpx.AsyncClient(follow_redirects=False, timeout=10, trust_env=False) as http,
                http.stream(
                    "GET",
                    target,
                    headers={"User-Agent": "OpenDialer (public first-party research)", "Host": parsed.netloc},
                    extensions={"sni_hostname": parsed.hostname},
                ) as response,
            ):
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";")[0]
                if content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
                    raise UnsafeURL("Unsupported source content type")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=min(65536, self.max_bytes + 1)):
                    self.decoded_bytes_total += len(chunk)
                    if len(body) + len(chunk) > self.max_bytes:
                        raise UnsafeURL("Source is too large")
                    body.extend(chunk)
            text = body.decode("utf-8", errors="replace")
            if len(text.encode()) > self.max_bytes:
                raise UnsafeURL("Decoded source is too large")
            return await asyncio.to_thread(parse_page, url, text, len(body))
        raise UnsafeURL("Source redirect budget exceeded")


def same_origin(base, url):
    """Identity boundary, not a guess from brand names or search snippets."""
    try:
        first, other = urlsplit(base), urlsplit(url)
        return bool(
            other.scheme in {"https", "http"}
            and not other.username
            and not other.password
            and (first.scheme, first.hostname, first.port or (443 if first.scheme == "https" else 80))
            == (other.scheme, other.hostname, other.port or (443 if other.scheme == "https" else 80))
        )
    except ValueError:
        return False


def official_links(url, soup):
    links, seen = [], {urldefrag(url)[0]}
    for anchor in soup.find_all("a", href=True):
        target = urldefrag(urljoin(url, anchor["href"]))[0]
        if target in seen or not same_origin(url, target):
            continue
        seen.add(target)
        links.append({"url": target, "text": anchor.get_text(" ", strip=True)[:200]})
        if len(links) == 128:
            break
    return links
