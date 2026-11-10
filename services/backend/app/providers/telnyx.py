import base64
import binascii
from datetime import UTC, datetime

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .errors import CarrierNotSubmitted, UncertainCarrierResult


class CarrierConfigurationError(ValueError):
    """The carrier account is reachable but configured so the documented flow cannot work."""


def verify_webhook(body, signature, timestamp, public_key, *, now=None):
    try:
        now = now or datetime.now(UTC)
        if abs(now.timestamp() - int(timestamp)) > 300:
            return False
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(base64.b64decode(signature, validate=True), timestamp.encode() + b"|" + body)
        return True
    except (ValueError, TypeError, binascii.Error, InvalidSignature):
        return False


def _errors(response):
    try:
        body = response.json()
    except ValueError:
        return []
    errors = body.get("errors") if isinstance(body, dict) else None
    return [e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []


class TelnyxCarrier:
    simulated = False
    instant_agent_legs = False
    replay = False  # A real carrier: its calls are real and end through its events.

    def __init__(self, api_key, connection_id, *, http=None, webhook_url=""):
        self.api_key, self.connection_id = api_key, connection_id
        self.http = http
        self.webhook_url = webhook_url

    async def request(self, method, path, payload=None, *, accept="application/json"):
        async def send(client):
            r = await client.request(
                method,
                "https://api.telnyx.com/v2" + path,
                headers={"Authorization": "Bearer " + self.api_key, "Accept": accept},
                json=payload,
                timeout=httpx.Timeout(15, connect=5),
            )
            r.raise_for_status()
            if not r.content:
                return {}
            try:
                return r.json()
            except ValueError:
                return r.text

        if self.http:
            return await send(self.http)
        async with httpx.AsyncClient() as client:
            return await send(client)

    async def dial(self, payload):
        body = {"connection_id": self.connection_id, **payload}
        if self.webhook_url:
            body["webhook_url"] = self.webhook_url
            body["webhook_url_method"] = "POST"
        try:
            response = await self.request("POST", "/calls", body)
            data = response.get("data") if isinstance(response, dict) else None
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("call_control_id"), str)
                or not data["call_control_id"]
            ):
                raise UncertainCarrierResult("Carrier accepted request without a usable call reference")
            return data
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            # No connection was established, so no request bytes reached the carrier.
            raise CarrierNotSubmitted("Carrier connection failed before the dial was sent") from e
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise UncertainCarrierResult(
                "Provider response uncertain; awaiting signed webhook/reconciliation"
            ) from e
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                raise UncertainCarrierResult("Carrier server response uncertain") from e
            raise

    async def lookup_number(self, phone):
        from urllib.parse import quote

        response = await self.request("GET", f"/number_lookup/{quote(phone, safe='')}?type=carrier")
        carrier = (response.get("data") or {}).get("carrier") or {}
        return {
            "carrier": carrier.get("normalized_carrier") or carrier.get("name"),
            "line_type": carrier.get("type"),
        }

    async def token(self, credential_id):
        from urllib.parse import quote

        # Documented response: 201 with the JWT as text/plain, not a JSON envelope.
        result = await self.request(
            "POST", f"/telephony_credentials/{quote(credential_id, safe='')}/token", accept="*/*"
        )
        if not isinstance(result, str) or not result.strip():
            raise CarrierConfigurationError("Carrier returned no access token for this credential")
        return result.strip()

    async def hangup(self, provider_id, command_id):
        return await self.action(provider_id, "hangup", {"command_id": command_id})

    async def action(self, provider_id, action, payload):
        from urllib.parse import quote

        result = await self.request("POST", f"/calls/{quote(provider_id, safe='')}/actions/{action}", payload)
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict) or data.get("result") != "ok":
            raise UncertainCarrierResult("Carrier control has no successful acknowledgement")
        return result

    async def reconcile(self, command_id, provider_id=None):
        # The dial command ID is not a provider lookup endpoint. Without its receipt, hold for webhook/manual review.
        if not provider_id:
            return None
        from urllib.parse import quote

        try:
            response = await self.request("GET", f"/calls/{quote(provider_id, safe='')}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {404, 422}:
                raise
            response = None
        data = response.get("data") if isinstance(response, dict) else None
        if data is None:
            # Status is retained only ten minutes after a call ends; a live leg always has it.
            return {"call_control_id": provider_id, "is_alive": False, "status_expired": True}
        return data

    async def list_numbers(self):
        found, page, expected_pages = [], 1, None
        while page <= 100:
            response = await self.request("GET", f"/phone_numbers?page[number]={page}&page[size]=250")
            if (
                not isinstance(response, dict)
                or not isinstance(response.get("data"), list)
                or response.get("errors")
            ):
                raise ValueError("Incomplete carrier inventory response")
            meta = response.get("meta")
            if not isinstance(meta, dict):
                raise ValueError("Missing carrier inventory pagination")
            pages = meta.get("total_pages")
            if (
                type(pages) is not int
                or not 0 <= pages <= 100
                or meta.get("page_number") != page
                or (expected_pages is not None and expected_pages != pages)
                or (pages == 0 and response["data"])
            ):
                raise ValueError("Incomplete or changing carrier inventory pagination")
            expected_pages = pages
            found.extend(response.get("data", []))
            if page >= pages:
                break
            page += 1
        for number in found:
            number["call_recording_enabled"] = await self.number_recording(number["id"])
        return found

    async def search_numbers(self, region, city=None, limit=10):
        from urllib.parse import urlencode

        from app.optimization.geography import AREA_STATES

        query = urlencode(
            {
                "filter[country_code]": "US",
                "filter[national_destination_code]": region,
                "filter[features]": "voice",
                "filter[limit]": limit,
                # Without this a sold-out area code (415) finds nothing instead of its overlay (628).
                "filter[best_effort]": "true",
                # An area code spans several places; 628 is San Francisco and Marin.
                **(
                    {"filter[locality]": city, "filter[administrative_area]": AREA_STATES.get(region, "")}
                    if city
                    else {}
                ),
            }
        )
        try:
            response = await self.request("GET", "/available_phone_numbers?" + query)
        except httpx.HTTPStatusError as exc:
            # Telnyx answers an area with no stock at all as a 400 "no numbers found" (code 10031).
            if exc.response.status_code == 400 and any(
                str(e.get("code")) == "10031" for e in _errors(exc.response)
            ):
                return []
            raise
        return response.get("data", [])

    async def purchase_number(self, phone, reference):
        response = await self.request(
            "POST",
            "/number_orders",
            {
                "phone_numbers": [{"phone_number": phone}],
                "connection_id": self.connection_id,
                "customer_reference": reference,
            },
        )
        return response.get("data", {})

    async def find_number_order(self, reference):
        from urllib.parse import urlencode

        response = await self.request(
            "GET",
            "/number_orders?"
            + urlencode(
                {
                    "filter[customer_reference]": reference,
                    "page[size]": 100,
                }
            ),
        )
        matches = [r for r in response.get("data", []) if r.get("customer_reference") == reference]
        return matches[0] if len(matches) == 1 else None

    async def configure_number(self, number_id, connection_id):
        from urllib.parse import quote

        # Absolute idempotent settings only. Never enable forwarding, screening,
        # CNAM subscriptions or recording while onboarding an already-owned DID.
        await self.request(
            "PATCH",
            f"/phone_numbers/{quote(number_id, safe='')}/voice",
            {"call_recording": {"inbound_call_recording_enabled": False}},
        )
        await self.request(
            "PATCH", f"/phone_numbers/{quote(number_id, safe='')}", {"connection_id": connection_id}
        )
        return await self.get_number(number_id)

    async def list_caller_name(self, number_id, name):
        """Outbound caller-name (CNAM) listing only; the separately priced inbound lookup
        (`caller_id_name_enabled`) is never sent. Returns the listing Telnyx reads back."""
        from urllib.parse import quote

        path = f"/phone_numbers/{quote(number_id, safe='')}/voice"
        listing = {"cnam_listing_enabled": True, "cnam_listing_details": name} if name else None
        await self.request("PATCH", path, {"cnam_listing": listing or {"cnam_listing_enabled": False}})
        response = await self.request("GET", path)
        return (response.get("data") or {}).get("cnam_listing") or {}

    async def set_daily_spend_limit(self, amount):
        """Telnyx refuses new calls past this amount; set on the voice profile this app dials through."""
        from urllib.parse import quote

        application = await self.request(
            "GET", f"/call_control_applications/{quote(self.connection_id, safe='')}"
        )
        profile = ((application.get("data") or {}).get("outbound") or {}).get("outbound_voice_profile_id")
        if not profile:
            raise CarrierConfigurationError("The voice application has no outbound voice profile")
        path = f"/outbound_voice_profiles/{quote(str(profile), safe='')}"
        current = await self.request("GET", path)
        await self.request(
            "PATCH",
            path,
            {
                "name": current["data"]["name"],
                "daily_spend_limit": f"{float(amount):.2f}",
                "daily_spend_limit_enabled": True,
            },
        )

    async def get_number(self, number_id):
        from urllib.parse import quote

        response = await self.request("GET", f"/phone_numbers/{quote(number_id, safe='')}")
        response["data"]["call_recording_enabled"] = await self.number_recording(number_id)
        return response

    async def number_recording(self, number_id):
        from urllib.parse import quote

        response = await self.request("GET", f"/phone_numbers/{quote(number_id, safe='')}/voice")
        recording = (response.get("data") or {}).get("call_recording") or {}
        return recording.get("inbound_call_recording_enabled")

    async def verify_incoming_only_credential(self, credential_id, *, sip_username=None):
        """The browser credential must be incoming-only and reachable by this account's agent dial."""
        from urllib.parse import quote

        response = await self.request("GET", f"/telephony_credentials/{quote(credential_id, safe='')}")
        credential = response.get("data") if isinstance(response, dict) else None
        if not isinstance(credential, dict):
            raise CarrierConfigurationError("Telephony credential was not found")
        if credential.get("expired"):
            raise CarrierConfigurationError("Telephony credential has expired; issue a new scoped credential")
        resource = credential.get("resource_id")
        kind, _, connection_id = resource.partition(":") if isinstance(resource, str) else ("", "", "")
        if kind != "connection" or not connection_id:
            raise CarrierConfigurationError(
                "Credential resource_id does not identify a credential connection (connection:<id>)"
            )
        if sip_username is not None and credential.get("sip_username") != sip_username:
            raise CarrierConfigurationError(
                "Configured SIP username does not match this credential; the agent leg would ring another client"
            )
        response = await self.request("GET", f"/credential_connections/{quote(connection_id, safe='')}")
        connection = response.get("data") if isinstance(response, dict) else None
        if not isinstance(connection, dict):
            raise CarrierConfigurationError("Credential connection was not found")
        outbound = connection.get("outbound")
        if not isinstance(outbound, dict) or outbound.get("outbound_voice_profile_id"):
            raise CarrierConfigurationError(
                "WebRTC credential connection must have no outbound voice profile; the server dials prospects"
            )
        if connection.get("sip_uri_calling_preference") != "internal":
            raise CarrierConfigurationError(
                "Set the credential connection's sip_uri_calling_preference to 'internal': 'disabled' rejects "
                "the server's agent leg and 'unrestricted' lets anyone ring the rep's browser"
            )
        return True


async def check_key(api_key, http=None):
    """(ok, detail) from one authenticated Telnyx request (section 7: a row turns green only after a real check)."""
    return await _check(
        "https://api.telnyx.com/v2/balance", api_key, "Telnyx", http, accepted="Telnyx accepted this key"
    )


async def _check(url, key, name, http, *, accepted):
    import httpx

    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    try:
        if http is not None:
            response = await http.get(url, headers=headers, timeout=10)
        else:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url, headers=headers)
    except httpx.HTTPError:
        return False, f"{name} could not be reached. Check the internet connection, then check again."
    if response.status_code == 200:
        return True, accepted
    if response.status_code in {401, 403}:
        return False, f"{name} refused this key. Copy it again from {name}."
    return False, f"{name} answered {response.status_code}. Check again in a minute."
