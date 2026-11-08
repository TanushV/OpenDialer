"""Exclusive execution leases on durable commands."""

from datetime import datetime


def owns_effect(data, owner, now):
    """A response cannot publish after its exclusive execution lease expired."""
    try:
        expiry = datetime.fromisoformat(data.get("lease_until") or "")
        return bool(owner and data.get("lease_owner") == owner and expiry > now)
    except (ValueError, TypeError):
        return False
