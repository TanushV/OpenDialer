"""RealValidito do-not-call lookups for CRM leads (D23, section 9A).

`POST /dnclookup/validate` takes up to 1,000 ten-digit numbers and returns, per number, `federal_dnc`, `state_dnc`
(a map of states), `tcpa_litigator`, `invalid` and `cleaned_number`. Codes 603 and 604 mean no credits and 600 a bad
key; each is definitive until the owner fixes it. Anything else unreadable is an outage. None of them ever clears a
number: the caller leaves the lead "DNC not checked" (section 9A).
"""

from urllib.parse import quote

import httpx

BASE_URL = "https://app.realvalidito.com"
BATCH = 1000
EXAMPLE_CREDENTIALS = {"#api-key", "#api-secret"}
ERRORS = {
    "600": ("Key or secret refused by RealValidito", "DNC_KEY_INVALID"),
    "601": ("RealValidito requires both the key and secret", "DNC_KEY_INVALID"),
    "603": ("Not enough RealValidito do-not-call credits for this request", "DNC_NO_CREDITS"),
    "604": ("No credits left for RealValidito do-not-call lookups", "DNC_NO_CREDITS"),
    "605": (
        "RealValidito rejected the credentials or DNC lookup is disabled on the account",
        "DNC_UNAVAILABLE",
    ),
    "616": (
        "RealValidito blocked this IP. Check Allowed IP addresses in the API credentials console.",
        "DNC_UNAVAILABLE",
    ),
}


class DNCUnavailable(RuntimeError):
    def __init__(self, message, reason="DNC_UNAVAILABLE"):
        super().__init__(message)
        self.reason = reason


def ten_digits(e164):
    if len(e164) != 12 or not e164.startswith("+1") or not e164[2:].isdigit():
        raise ValueError("A normalized US number is required")
    return e164[2:]


def _check_response(payload, status):
    codes = [status]
    failed = False
    if isinstance(payload, dict):
        codes.append(payload.get("code"))
        failed = str(payload.get("status")).lower() == "failed"
        error = payload.get("error")
        if isinstance(error, dict):
            codes.append(error.get("error_code"))
        legacy = payload.get("Response")
        if isinstance(legacy, dict):
            failed |= str(legacy.get("Status")).lower() == "failed"
            errors = legacy.get("Errors")
            if isinstance(errors, dict):
                codes.append(errors.get("ErrorCode"))
    for code in codes:
        error = ERRORS.get(str(code))
        if error:
            raise DNCUnavailable(*error)
    if status != 200:
        raise DNCUnavailable(f"RealValidito HTTP {status}")
    if failed:
        raise DNCUnavailable("RealValidito rejected the request")


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
        if self.api_key in EXAMPLE_CREDENTIALS or self.api_secret in EXAMPLE_CREDENTIALS:
            raise DNCUnavailable(
                "RealValidito key and secret must replace the example placeholders", "DNC_KEY_INVALID"
            )

        async def go(http):
            try:
                response = await http.request(method, BASE_URL + path, timeout=self.timeout, **kwargs)
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
            except httpx.HTTPError as exc:
                raise DNCUnavailable("RealValidito unreachable: " + type(exc).__name__) from exc
            _check_response(payload, response.status_code)
            return payload

        if self.http:
            return await go(self.http)
        async with httpx.AsyncClient() as http:
            return await go(http)

    async def credits(self):
        key, secret = quote(self.api_key, safe=""), quote(self.api_secret, safe="")
        payload = await self._send("GET", f"/dnclookup/getcredits/{key}/{secret}")
        value = payload
        if isinstance(payload, dict):
            value = payload.get("credits")
            data = payload.get("data")
            if isinstance(data, dict):
                value = data.get("available_credits", value)
        if type(value) in (int, str):
            try:
                balance = int(value)
            except ValueError as exc:
                raise DNCUnavailable("RealValidito returned an invalid do-not-call credit balance") from exc
            if balance >= 0:
                return balance
        raise DNCUnavailable("RealValidito returned an invalid do-not-call credit balance")

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
