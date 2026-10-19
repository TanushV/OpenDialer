import asyncio
import ipaddress
import socket
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup


class UnsafeURL(ValueError):
    pass


def resolve(host):
    return list({info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})


def validate_url(url, resolver=resolve):
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 80, 443}
            or parsed.hostname.lower() in {"localhost", "metadata.google.internal"}
        ):
            raise UnsafeURL("Forbidden destination")
        addresses = resolver(parsed.hostname)
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            raise UnsafeURL("Private, loopback, link-local or mixed DNS destination")
        return addresses
    except (ValueError, OSError) as e:
        raise UnsafeURL("Unsafe or unresolvable source URL") from e


class SafeFetcher:
    def __init__(self, max_bytes=1_000_000, max_redirects=3, allowed_domains=None, http=None):
        self.max_bytes, self.max_redirects = max_bytes, max_redirects
        self.allowed_domains = set(allowed_domains or [])
        self.http = http
        self.decoded_bytes_total = 0

    async def fetch(self, url):
        if self.http is not None:
            return await self._fetch(self.http, url)
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=10) as http:
            return await self._fetch(http, url)

    async def _fetch(self, http, url):
        for _ in range(self.max_redirects + 1):
            parsed = urlsplit(url)
            if self.allowed_domains and parsed.hostname not in self.allowed_domains:
                raise UnsafeURL("Source domain is not allowlisted")
            addresses = await asyncio.to_thread(validate_url, url)
            # Pin the verified address; retain Host and TLS SNI to close DNS rebinding between validation and connect.
            pinned = httpx.URL(url).copy_with(host=addresses[0])
            async with http.stream(
                "GET",
                pinned,
                headers={
                    "Host": parsed.netloc,
                    "User-Agent": "OpenDialer/0.1 (public first-party research)",
                },
                extensions={"sni_hostname": parsed.hostname.encode()},
                follow_redirects=False,
                timeout=10,
            ) as response:
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
                soup = BeautifulSoup(text, "html.parser")
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                return {
                    "url": url,
                    "title": soup.title.get_text() if soup.title else "",
                    "text": soup.get_text(" ", strip=True)[:50000],
                    "links": official_links(url, soup),
                    "decoded_bytes": len(body),
                }
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


def discovery_links(home, maximum=2):
    """Pick a small useful official set; never recursively crawl linked pages."""
    ranked = {}
    for link in home.get("links", [])[:128]:
        target = urldefrag(urljoin(home["url"], link.get("url", "")))[0]
        if target == urldefrag(home["url"])[0] or not same_origin(home["url"], target):
            continue
        label = (urlsplit(target).path + " " + link.get("text", "")).casefold()
        rank = next(
            (
                i
                for i, words in enumerate(
                    (("service", "product", "solution"), ("about", "company"), ("location", "contact"))
                )
                if any(word in label for word in words)
            ),
            None,
        )
        if rank is not None:
            ranked[target] = min(rank, ranked.get(target, rank))
    return sorted(ranked, key=lambda target: (ranked[target], target))[:maximum]
