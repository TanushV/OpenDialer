import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

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
    def __init__(self, max_bytes=1_000_000, max_redirects=3, allowed_domains=None):
        self.max_bytes, self.max_redirects = max_bytes, max_redirects
        self.allowed_domains = set(allowed_domains or [])

    async def fetch(self, url):
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=10) as http:
            for _ in range(self.max_redirects + 1):
                parsed = urlsplit(url)
                if self.allowed_domains and parsed.hostname not in self.allowed_domains:
                    raise UnsafeURL("Source domain is not allowlisted")
                addresses = validate_url(url)
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
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";")[0]
                    if content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
                        raise UnsafeURL("Unsupported source content type")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_bytes:
                            raise UnsafeURL("Source is too large")
                    text = body.decode("utf-8", errors="replace")
                    soup = BeautifulSoup(text, "html.parser")
                    for tag in soup(["script", "style", "noscript"]):
                        tag.decompose()
                    return {
                        "url": url,
                        "title": soup.title.get_text() if soup.title else "",
                        "text": soup.get_text(" ", strip=True)[:50000],
                    }
        raise UnsafeURL("Source redirect budget exceeded")
