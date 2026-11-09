"""Set up a rep's seat from the owner's computer (D25, R9C.9).

From the rep's Telnyx API key: a Call Control application for the rep's calls, capped at 2 channels (one call), on the
team's outbound voice profile; a telephony credential for the rep's audio on the team's credential connection; and a
relay workspace for the rep, invited by the owner's seat and redeemed through the existing setup function. The result
is the rep's private setup file. Nothing here touches another seat: a route already in use is refused by the relay.
"""

import hashlib
import secrets
from urllib.parse import quote

import httpx

CHANNELS = 2  # The prospect and the rep's leg: one call at a time (section 9C).
SHARED = (
    "hubspot_service_key",
    "treg_token",
    "realvalidito_api_key",
    "realvalidito_api_secret",
    "openrouter_api_key",
)


class SeatSetupError(RuntimeError):
    pass


async def create_seat(owner, rep, *, label, credential_id, webhook_url, relay, relay_url, shared, http=None):
    """`owner` and `rep` are TelnyxCarrier clients (owner's connection; rep's key). Returns the rep's setup file."""
    try:
        application = await owner.request(
            "GET", f"/call_control_applications/{quote(owner.connection_id, safe='')}"
        )
        profile = ((application.get("data") or {}).get("outbound") or {}).get("outbound_voice_profile_id")
        credential = await owner.request("GET", f"/telephony_credentials/{quote(credential_id, safe='')}")
        kind, _, connection = str((credential.get("data") or {}).get("resource_id") or "").partition(":")
        if not profile or kind != "connection" or not connection:
            raise SeatSetupError("The owner's calling setup is incomplete; import its setup file first")
        name = f"OpenDialer {label}"[:60]
        created = await rep.request(
            "POST",
            "/call_control_applications",
            {
                "application_name": name,
                "webhook_event_url": webhook_url,
                "webhook_api_version": "2",
                "outbound": {"channel_limit": CHANNELS, "outbound_voice_profile_id": profile},
                "inbound": {"channel_limit": CHANNELS},
            },
        )
        app_id = str(created["data"]["id"])
        login = await rep.request(
            "POST", "/telephony_credentials", {"connection_id": connection, "name": name}
        )
        login = login["data"]
    except httpx.HTTPStatusError as exc:
        raise SeatSetupError(
            f"Telnyx refused the rep's setup (HTTP {exc.response.status_code}); check the rep's key"
        ) from exc
    token = secrets.token_urlsafe(36)
    try:
        await relay.rpc(
            "relay_invite_seat",
            {
                "label": label,
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "connection_id": app_id,
                "settings": {"telephony_credential_id": login["id"], "sip_username": login["sip_username"]},
            },
        ).execute()
    except Exception as exc:
        raise SeatSetupError(
            "The relay refused the new seat: only the team owner's computer can set up seats"
        ) from exc

    async def redeem(client):
        response = await client.post(
            f"{relay_url}/functions/v1/managed-setup", json={"token": token}, timeout=20
        )
        if response.status_code != 200:
            raise SeatSetupError(
                f"The relay could not issue the rep's setup file (HTTP {response.status_code})"
            )
        return response.json()

    if http:
        issued = await redeem(http)
    else:
        async with httpx.AsyncClient() as client:
            issued = await redeem(client)
    return {
        **issued,
        "telnyx_api_key": rep.api_key,
        "number_pool": True,
        **{k: v for k, v in shared.items() if k in SHARED and v},
    }
