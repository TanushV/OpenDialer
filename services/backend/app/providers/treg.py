"""Treg's HTTP call route for the in-app waterfall (D19). Credentials stay on Treg's side; the app holds a capped token.

`{base}/call/{endpoint}` relays one catalog request. The response body is the provider's; Treg's headers say what it
cost (`X-Treg-Cost-Micro`), its call id and whether it is an async job. Every request carries the caller's
idempotency key, so a retry after a lost answer replays the original request and is never charged twice. A Wiza
reveal is started, then polled for free until it finishes.
"""

import asyncio

import httpx

BASE_URL = "https://treg.to"
POLL = "wiza.people.reveal.get"
POLLS, POLL_SECONDS = 20, 2.0
EXCLUDE = "aviato,leadmagic,quickenrich"


class TregError(RuntimeError):
    pass


class Treg:
    def __init__(self, token, *, base_url=BASE_URL, http=None, sleep=asyncio.sleep, timeout=190):
        self.token, self.base_url, self.http, self.sleep, self.timeout = (
            token,
            base_url.rstrip("/"),
            http,
            sleep,
            timeout,
        )

    async def _send(self, http, method, endpoint, params, headers):
        kwargs = {"params": params} if method == "GET" else {"json": params}
        try:
            response = await http.request(
                method,
                f"{self.base_url}/call/{endpoint}",
                headers={"X-Treg-Token": self.token, "X-Treg-Client": "opendialer", **headers},
                timeout=self.timeout,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise TregError("Treg unreachable: " + type(exc).__name__) from exc
        if response.status_code in (401, 403) and response.headers.get("X-Treg-Error"):
            raise TregError("Treg refused the token")
        try:
            body = response.json()
        except ValueError:
            body = response.text
        meta = {"http_status": response.status_code}
        if call_id := response.headers.get("X-Treg-Call-Id"):
            meta["call_id"] = call_id
        cost = response.headers.get("X-Treg-Cost-Micro")
        if cost is not None:
            meta["reserved_micro" if response.headers.get("X-Treg-Async") else "charged_micro"] = int(cost)
        elif 400 <= response.status_code < 600:
            meta["charged_micro"] = 0  # A refused or failed relay is not charged.
        if response.headers.get("X-Treg-Async"):
            meta["async"] = True
        return {"result": body, "_treg": meta}

    async def call(self, endpoint, method, params, max_cost_micro, key):
        headers = {
            "Idempotency-Key": key,
            "X-Treg-Route-Max-Cost": f"{max_cost_micro / 1_000_000:.6f}",
            "X-Treg-Route-Exclude": EXCLUDE,
        }

        async def go(http):
            answer = await self._send(http, method, endpoint, params, headers)
            data = answer["result"].get("data") if isinstance(answer["result"], dict) else None
            pending = isinstance(data, dict) and data.get("status") in {"queued", "resolving"}
            if endpoint.startswith("wiza.") and (answer["_treg"].get("async") or pending):
                return await self._finish_wiza(http, answer, key)
            return answer

        if self.http:
            return await go(self.http)
        async with httpx.AsyncClient() as http:
            return await go(http)

    async def _finish_wiza(self, http, submitted, key):
        task = (submitted["result"].get("data") or {}).get("id")
        if not task:
            raise TregError("A pending Wiza job has no id")
        for attempt in range(POLLS):
            await self.sleep(POLL_SECONDS)
            polled = await self._send(
                http, "GET", POLL, {"id": task}, {"Idempotency-Key": f"{key}:poll:{attempt}"}
            )
            data = polled["result"].get("data") if isinstance(polled["result"], dict) else None
            if isinstance(data, dict) and (data.get("status") == "finished" or data.get("is_complete")):
                # Wiza settles the reserved kickoff at its real credit use; the reservation is the bound until the
                # settlement is read, and a miss is free.
                found = bool(data.get("phones") or data.get("mobile_phone") or data.get("phone_number"))
                charged = submitted["_treg"].get("reserved_micro", submitted["_treg"].get("charged_micro", 0))
                meta = {**submitted["_treg"], "charged_micro": charged if found else 0, "async": False}
                return {"result": polled["result"], "_treg": meta}
        raise TregError("Wiza did not finish in time; the job is kept for the next run")

    async def check(self):
        """Settings check on save: an authenticated, free request."""
        async with httpx.AsyncClient() as http:
            try:
                response = await http.get(
                    f"{self.base_url}/auth/me", headers={"X-Treg-Token": self.token}, timeout=15
                )
            except httpx.HTTPError as exc:
                return False, "Treg unreachable: " + type(exc).__name__
        return (response.status_code == 200, "Connected" if response.status_code == 200 else "Token refused")
