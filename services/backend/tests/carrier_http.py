"""A carrier transport for calling tests that counts raw requests before any retry deduplication."""

import json

import httpx


class CarrierHTTP:
    """Record the actual adapter's requests and control the carrier's replies."""

    def __init__(self, credentials=None):
        self.requests = []
        self.dials = {}
        self.live = set()
        self.lose_dial_response = False
        self.lose_hangup_response_for = None
        self.credentials = credentials or {"fixture-credential": "fixture-rep"}
        self.incoming = set()

    def __call__(self, request):
        path = request.url.path.removeprefix("/v2")
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, path, body))
        if path.startswith("/telephony_credentials/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "resource_id": "connection:12345",
                        "sip_username": self.credentials[path.removeprefix("/telephony_credentials/")],
                        "expired": False,
                    }
                },
            )
        if path == "/credential_connections/12345":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "sip_uri_calling_preference": "internal",
                        "outbound": {"outbound_voice_profile_id": None},
                    }
                },
            )
        if (request.method, path) == ("POST", "/calls"):
            identity = f"fixture-leg-{len(self.dials) + 1}"
            self.dials[identity] = body
            self.live.add(identity)
            if self.lose_dial_response and not body["to"].startswith("sip:"):
                raise httpx.ReadTimeout("accepted; response lost", request=request)
            return httpx.Response(200, json={"data": {"call_control_id": identity}})
        method, identity, *action = path.removeprefix("/").split("/")
        assert method == "calls" and (identity in self.dials or identity in self.incoming), (
            request.method,
            path,
        )
        if request.method == "GET":
            return httpx.Response(
                200, json={"data": {"call_control_id": identity, "is_alive": identity in self.live}}
            )
        assert action in (
            ["actions", "hangup"],
            ["actions", "answer"],
            ["actions", "transfer"],
            ["actions", "transcription_start"],
            ["actions", "transcription_stop"],
        )
        if action[-1] == "hangup":
            self.live.discard(identity)
            if self.lose_hangup_response_for == identity:
                raise httpx.ReadTimeout("hang-up accepted; response lost", request=request)
        return httpx.Response(200, json={"data": {"result": "ok"}})

    def dial_budget(self, count):
        # Count requests before carrier-side deduplication. An extra POST is an extra attempted effect.
        dials = [body for method, path, body in self.requests if (method, path) == ("POST", "/calls")]
        rep_legs = [body for body in dials if body["to"].startswith("sip:")]
        prospects = [body for body in dials if not body["to"].startswith("sip:")]
        assert (len(rep_legs), len(prospects)) == (count, count), self.requests
        assert len({body["command_id"] for body in dials}) == count * 2
        return prospects
