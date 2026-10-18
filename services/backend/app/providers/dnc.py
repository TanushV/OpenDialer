"""Documented prepaid lookup. A query result never replaces seller registry authority."""

from datetime import UTC, datetime

import httpx


class DNCError(ValueError):
    pass


class PrepaidDNC:
    def __init__(self, api_key, *, http=None, clock=None):
        self.api_key, self.http = api_key, http
        self.clock = clock or (lambda: datetime.now(UTC))

    async def check(self, phone):
        if len(phone) != 12 or not phone.startswith("+1") or not phone[2:].isdigit():
            raise DNCError("An exact normalized US number is required")

        async def send(http):
            try:
                response = await http.get(
                    "https://www.donotcalldnc.com/api/v1/check-1/",
                    headers={"X-API-KEY": self.api_key},
                    params={"phone": phone[2:]},
                    timeout=8,
                )
                response.raise_for_status()
                data = response.json()
                at = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
                if (
                    data.get("success") is not True
                    or data.get("phone") != phone[2:]
                    or type(data.get("is_dnc")) is not bool
                    or data.get("status") != ("DNC" if data["is_dnc"] else "NOT DNC")
                    or type(data.get("credits_remaining")) is not int
                    or data["credits_remaining"] < 0
                    or at.tzinfo is None
                    or not -60 <= (self.clock() - at).total_seconds() <= 300
                ):
                    raise DNCError("Provider result is incomplete, stale or inconsistent")
                return {
                    "phone": phone,
                    "listed": data["is_dnc"],
                    "checked_at": at.isoformat(),
                    "provider": "donotcalldnc",
                    "source_authorization_required": True,
                    "credits_remaining": data["credits_remaining"],
                }
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                raise DNCError("Screening unavailable; no clearance or automatic paid retry") from exc

        if self.http:
            return await send(self.http)
        async with httpx.AsyncClient() as http:
            return await send(http)
