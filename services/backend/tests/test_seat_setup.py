"""R9C.9: a rep's seat is set up from the owner's computer: its own capped Call Control application, its own audio
credential, a relay invitation, and a private setup file that carries the shared keys and turns the pool on."""

import json
from types import SimpleNamespace

import httpx
import pytest
from app.numbers.seat import SeatSetupError, create_seat
from app.providers.telnyx import TelnyxCarrier


class Relay:
    def __init__(self, refuse=False):
        self.calls, self.refuse = [], refuse

    def rpc(self, name, args):
        self.calls.append((name, args))

        async def execute():
            if self.refuse:
                raise RuntimeError("Only the team owner can set up a seat")
            return SimpleNamespace(data="seat-id")

        return SimpleNamespace(execute=execute)


def telnyx(sent):
    def respond(request):
        body = json.loads(request.content) if request.content else None
        sent.append((request.method, request.url.path, body, request.headers.get("Authorization")))
        path = request.url.path
        if path == "/v2/call_control_applications/owner-app":
            return httpx.Response(200, json={"data": {"outbound": {"outbound_voice_profile_id": "ovp-1"}}})
        if path == "/v2/telephony_credentials/owner-cred":
            return httpx.Response(200, json={"data": {"resource_id": "connection:cred-conn"}})
        if path == "/v2/call_control_applications":
            return httpx.Response(201, json={"data": {"id": "2001"}})
        if path == "/v2/telephony_credentials":
            return httpx.Response(201, json={"data": {"id": "rep-cred", "sip_username": "rep9"}})
        if path == "/functions/v1/managed-setup":
            return httpx.Response(
                200, json={"version": 1, "label": "Rep", "workspace_id": "seat-id", "password": "p" * 48}
            )
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


async def test_a_seat_gets_its_own_capped_application_credential_and_setup_file():
    sent = []
    http = telnyx(sent)
    relay = Relay()
    owner = TelnyxCarrier("owner-key", "owner-app", http=http)
    rep = TelnyxCarrier("rep-key-123", None, http=http)
    profile = await create_seat(
        owner,
        rep,
        label="Rep 9",
        credential_id="owner-cred",
        webhook_url="https://relay/functions/v1/telnyx-webhook",
        relay=relay,
        relay_url="https://relay",
        shared={"hubspot_service_key": "hs", "telnyx_api_key": "owner-key", "treg_token": ""},
        http=http,
    )
    created = next(
        body for method, path, body, _ in sent if method == "POST" and path == "/v2/call_control_applications"
    )
    assert created["outbound"] == {"channel_limit": 2, "outbound_voice_profile_id": "ovp-1"}
    assert created["webhook_event_url"].endswith("/telnyx-webhook")
    login = next(
        body for method, path, body, _ in sent if path == "/v2/telephony_credentials" and method == "POST"
    )
    assert login["connection_id"] == "cred-conn", (
        "the rep's audio sign-in is on the team's credential connection"
    )
    assert all(
        auth == "Bearer rep-key-123"
        for method, path, _, auth in sent
        if method == "POST" and path.startswith("/v2/")
    )
    name, args = relay.calls[0]
    assert name == "relay_invite_seat" and args["connection_id"] == "2001" and len(args["token_hash"]) == 64
    assert args["settings"] == {"telephony_credential_id": "rep-cred", "sip_username": "rep9"}
    assert profile["telnyx_api_key"] == "rep-key-123" and profile["number_pool"] is True
    assert profile["hubspot_service_key"] == "hs" and "treg_token" not in profile


async def test_a_seat_the_relay_refuses_is_named_and_returns_no_file():
    sent = []
    http = telnyx(sent)
    with pytest.raises(SeatSetupError, match="only the team owner"):
        await create_seat(
            TelnyxCarrier("owner-key", "owner-app", http=http),
            TelnyxCarrier("rep-key-123", None, http=http),
            label="Rep",
            credential_id="owner-cred",
            webhook_url="w",
            relay=Relay(refuse=True),
            relay_url="https://relay",
            shared={},
            http=http,
        )
    assert not any(path == "/functions/v1/managed-setup" for _, path, _, _ in sent)


def test_a_seat_file_turns_on_the_pool_and_stores_the_shared_keys_as_secrets():
    from app.managed_webhooks import SEAT_SECRETS, ConnectionProfile

    profile = ConnectionProfile(
        label="Rep",
        url="https://abcdefghijklmnopqrst.supabase.co",
        publishable_key="sb_publishable_x" * 2,
        workspace_id="00000000-0000-0000-0000-000000000009",
        email="seat@opendialer.invalid",
        password="p" * 48,
        number_pool=True,
        hubspot_service_key="hs",
        realvalidito_api_key="rk",
        realvalidito_api_secret="rs",
    )
    assert profile.number_pool and {"hubspot_service_key", "realvalidito_api_secret"} <= set(SEAT_SECRETS)
    assert "hs" not in repr(profile)
