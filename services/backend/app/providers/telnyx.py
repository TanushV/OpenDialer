import base64
import binascii
from datetime import UTC, datetime

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .simulated import UncertainCarrierResult


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


class TelnyxCarrier:
    simulated = False

    def __init__(self, api_key, connection_id, *, http=None, webhook_url=""):
        self.api_key, self.connection_id = api_key, connection_id
        self.http = http
        self.webhook_url = webhook_url

    async def request(self, method, path, payload=None):
        async def send(client):
            r = await client.request(
                method,
                "https://api.telnyx.com/v2" + path,
                headers={"Authorization": "Bearer " + self.api_key, "Accept": "application/json"},
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
            return response["data"]
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise UncertainCarrierResult(
                "Provider response uncertain; awaiting signed webhook/reconciliation"
            ) from e
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                raise UncertainCarrierResult("Carrier server response uncertain") from e
            raise

    async def token(self, credential_id):
        result = await self.request("POST", f"/telephony_credentials/{credential_id}/token")
        return result if isinstance(result, str) else result.get("data", result)

    async def hangup(self, provider_id, command_id):
        return await self.action(provider_id, "hangup", {"command_id": command_id})

    async def action(self, provider_id, action, payload):
        from urllib.parse import quote

        return await self.request("POST", f"/calls/{quote(provider_id, safe='')}/actions/{action}", payload)

    async def playback(self, provider_id, payload):
        return await self.action(provider_id, "playback_start", payload)

    async def reconcile(self, command_id, provider_id=None):
        # The dial command ID is not a provider lookup endpoint. Without its receipt, hold for webhook/manual review.
        if not provider_id:
            return None
        from urllib.parse import quote

        response = await self.request("GET", f"/calls/{quote(provider_id, safe='')}")
        return response.get("data")

    async def list_numbers(self):
        found, page = [], 1
        while page <= 100:
            response = await self.request("GET", f"/phone_numbers?page[number]={page}&page[size]=100")
            found.extend(response.get("data", []))
            if page >= response.get("meta", {}).get("total_pages", page):
                break
            page += 1
        return found

    async def search_numbers(self, region):
        from urllib.parse import urlencode

        query = urlencode(
            {
                "filter[country_code]": "US",
                "filter[national_destination_code]": region,
                "filter[features]": "voice",
                "filter[limit]": 10,
            }
        )
        response = await self.request("GET", "/available_phone_numbers?" + query)
        return response.get("data", [])

    async def purchase(self, numbers, command_id):
        # Number order API has no invented idempotency field; durable outbox prevents unknown-outcome resubmission.
        return await self.request(
            "POST",
            "/number_orders",
            {"phone_numbers": [{"phone_number": n} for n in numbers], "connection_id": self.connection_id},
        )

    async def lookup(self, phone):
        from urllib.parse import quote

        return await self.request("GET", f"/number_lookup/{quote(phone, safe='')}?type=carrier")

    async def configure_number(self, number_id, connection_id):
        return await self.request("PATCH", f"/phone_numbers/{number_id}", {"connection_id": connection_id})

    async def rnd(self, phone, reference_date):
        # RND requires an independently authorized account adapter; carrier lookup is not a substitute.
        raise ValueError("Configure an authorized Reassigned Numbers Database subscription adapter")

    async def verify_incoming_only_credential(self, credential_id):
        credential = await self.request("GET", f"/telephony_credentials/{credential_id}")
        connection_id = credential.get("data", {}).get("connection_id")
        if not connection_id:
            raise ValueError("Credential does not identify a scoped SIP connection")
        connection = (await self.request("GET", f"/credential_connections/{connection_id}")).get("data", {})
        outbound = connection.get("outbound")
        if not isinstance(outbound, dict) or outbound.get("outbound_voice_profile_id"):
            raise ValueError(
                "WebRTC credential must have no outbound PSTN voice profile; server controls prospect dialing"
            )
        return True
