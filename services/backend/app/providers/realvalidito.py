"""RealValidito do-not-call lookups for CRM leads (D23, section 9A).

`POST /dnclookup/validate` takes up to 1,000 ten-digit numbers and returns, per number, `federal_dnc`, `state_dnc`
(a map of states), `tcpa_litigator`, `invalid` and `cleaned_number`. Codes 603 and 604 mean no credits and 600 a bad
key; each is definitive until the owner fixes it. Anything else unreadable is an outage. None of them ever clears a
number: the caller leaves the lead "DNC not checked" (section 9A).
"""

import httpx

BASE_URL = "https://app.realvalidito.com"
BATCH = 1000
CODES = {600: "DNC_KEY_INVALID", 603: "DNC_NO_CREDITS", 604: "DNC_NO_CREDITS"}


class DNCUnavailable(RuntimeError):
    def __init__(self, message, reason="DNC_UNAVAILABLE"):
        super().__init__(message)
        self.reason = reason


def ten_digits(e164):
    if len(e164) != 12 or not e164.startswith("+1") or not e164[2:].isdigit():
        raise ValueError("A normalized US number is required")
    return e164[2:]


def _code(payload, status):
    for value in (status, payload.get("code") if isinstance(payload, dict) else None):
        try:
            if int(value) in CODES:
                return CODES[int(value)]
        except (TypeError, ValueError):
            continue
    return None


def _entries(payload):
    if isinstance(payload, list):
        return payload
    for key in ("data", "results", "numbers"):
        if isinstance(payload.get(key), list):
            return payload[key]
    raise DNCUnavailable("RealValidito answered without results")


def parse(entry):
    """One number's answer: which lists it is on, or that it is invalid. Missing flags are an outage, not a pass."""
    flags = ("federal_dnc", "tcpa_litigator", "invalid")
    if any(type(entry.get(flag)) is not bool for flag in flags) or not isinstance(
        entry.get("state_dnc"), dict
    ):
        raise DNCUnavailable("RealValidito answered with an incomplete result")
    lists = (["federal"] if entry["federal_dnc"] else []) + (
        ["tcpa_litigator"] if entry["tcpa_litigator"] else []
    )
    lists += [f"state:{state}" for state, listed in sorted(entry["state_dnc"].items()) if listed]
    return {"invalid": entry["invalid"], "lists": lists}


class RealValidito:
    def __init__(self, api_key, api_secret, *, http=None, timeout=30):
        self.api_key, self.api_secret, self.http, self.timeout = api_key, api_secret, http, timeout

    async def _send(self, method, path, **kwargs):
        async def go(http):
            try:
                response = await http.request(method, BASE_URL + path, timeout=self.timeout, **kwargs)
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
            except httpx.HTTPError as exc:
                raise DNCUnavailable("RealValidito unreachable: " + type(exc).__name__) from exc
            reason = _code(payload, response.status_code)
            if reason:
                raise DNCUnavailable("RealValidito refused the request", reason)
            if response.status_code != 200:
                raise DNCUnavailable(f"RealValidito HTTP {response.status_code}")
            return payload

        if self.http:
            return await go(self.http)
        async with httpx.AsyncClient() as http:
            return await go(http)

    async def credits(self):
        payload = await self._send("GET", f"/dnclookup/getcredits/{self.api_key}/{self.api_secret}")
        value = payload.get("credits") if isinstance(payload, dict) else payload
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise DNCUnavailable("RealValidito credits unreadable") from exc

    async def validate(self, numbers):
        """{e164: {"invalid", "lists"}} for every number asked, in batches of at most 1,000."""
        wanted = {ten_digits(n): n for n in dict.fromkeys(numbers)}
        results, digits = {}, list(wanted)
        for start in range(0, len(digits), BATCH):
            chunk = digits[start : start + BATCH]
            payload = await self._send(
                "POST",
                "/dnclookup/validate",
                json={"api_key": self.api_key, "api_secret": self.api_secret, "numbers": chunk},
            )
            for entry in _entries(payload):
                number = str(entry.get("number") or entry.get("cleaned_number") or "")[-10:]
                if number in wanted:
                    results[wanted[number]] = parse(entry)
            missing = [wanted[d] for d in chunk if wanted[d] not in results]
            if missing:
                raise DNCUnavailable(f"RealValidito left {len(missing)} numbers unanswered")
        return results
