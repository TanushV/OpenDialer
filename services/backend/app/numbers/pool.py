"""The shared number pool, app side (D26, section 9C): lease a caller ID from the relay before each dial, release it
when the call ends, and read the team's daily use for number health.

The relay decides; this module only asks. Without an answer there is no caller ID, so the dial is refused with a named
reason and nothing reaches the prospect. Two seats never get one number at once (tested in `test_relay_pool.py`).
"""

LEASE_SECONDS = 300
REASONS = {
    "POOL_EXHAUSTED": "Every pool number reached today's cap or is in use. Add numbers to the pool or call later.",
    "POOL_BUSY": "Every pool number is in use by another rep right now. Calling resumes when one frees up.",
    "POOL_UNREACHABLE": "The number pool could not be reached, so no caller ID was chosen. This clears when it answers.",
}


class PoolUnavailable(RuntimeError):
    def __init__(self, reason):
        super().__init__(REASONS.get(reason, reason))
        self.reason = reason if reason in REASONS else "POOL_UNREACHABLE"


def area_preferences(phone, area_states):
    """The prospect's own area code first, then the other area codes in their state, as the relay's preference list."""
    own = phone[2:5] if phone and phone.startswith("+1") and len(phone) == 12 else None
    if not own:
        return []
    state = area_states.get(own)
    same = sorted(code for code, where in area_states.items() if state and where == state and code != own)
    return [own, *same][:20]


async def lease(client, prospect, areas, call_ref, seconds=LEASE_SECONDS):
    try:
        answer = await client.rpc(
            "relay_lease_number",
            {"prospect": prospect, "areas": list(areas), "call_ref": call_ref, "lease_seconds": seconds},
        ).execute()
    except Exception as exc:
        raise PoolUnavailable("POOL_UNREACHABLE") from exc
    data = answer.data or {}
    if not data.get("phone"):
        raise PoolUnavailable(data.get("reason") or "POOL_EXHAUSTED")
    return data["phone"]


async def release(client, call_ref):
    await client.rpc("relay_release_number", {"call_ref": call_ref}).execute()


async def usage(client):
    return (await client.rpc("relay_pool_usage", {}).execute()).data or {}
