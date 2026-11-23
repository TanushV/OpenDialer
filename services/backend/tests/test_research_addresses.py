"""SafeFetcher's address gate on literal IPs (no DNS or network). Audit of November 23, 2026."""

import pytest
from app.research.fetch import UnsafeURL, public_address


@pytest.mark.parametrize("host", ["64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "64:ff9b::a00:1"])
async def test_a_nat64_address_that_embeds_a_private_ipv4_is_refused(host):
    """64:ff9b::/96 is globally routable, but a NAT64 gateway forwards it to the embedded IPv4 address."""
    with pytest.raises(UnsafeURL):
        await public_address(host, 443)


async def test_a_nat64_address_that_embeds_a_public_ipv4_is_allowed():
    assert await public_address("64:ff9b::808:808", 443) == "64:ff9b::808:808"
