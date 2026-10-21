"""ChatGPT-plan sign-in for a hosted authority: the user pastes the loopback address."""

from urllib.parse import parse_qs, urlsplit

import pytest
from test_control_completion import principal

LOOPBACK = "http://127.0.0.1:1455/auth/callback"


@pytest.fixture
def hosted(workspace, app, monkeypatch):
    monkeypatch.setattr(app.state.service.settings, "mode", "connected")
    return workspace


def start(workspace):
    response = workspace["client"].post("/v1/inference/chatgpt/start", headers=workspace["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def finished(monkeypatch, calls):
    from app.providers.oauth import PlanOAuth

    async def finish(self, pending, *, state, code=None, client_id=None, error=None, http=None):
        calls.append({"state": state, "code": code, "client_id": client_id, "error": error})
        if state != pending["state"] or pending.get("used"):
            from app.providers.oauth import OAuthError

            raise OAuthError("Expired, replayed or mismatched OAuth state")
        return {
            "access_token": "protected-access",
            "refresh_token": "protected-refresh",
            "client_id": client_id,
            "subject": "chatgpt-subject",
            "scopes": ["chatgpt.tokens.use.direct"],
        }

    monkeypatch.setattr(PlanOAuth, "finish", finish)


def test_hosted_sign_in_completes_from_the_pasted_loopback_address(hosted, app, monkeypatch):
    flow = start(hosted)
    assert flow["completion"] == "paste"
    params = parse_qs(urlsplit(flow["authorization_url"]).query)
    assert params["redirect_uri"] == [LOOPBACK]
    calls = []
    finished(monkeypatch, calls)
    pasted = f"{LOOPBACK}?code=one-time-code&state={params['state'][0]}&client_id=oaiapp_test"
    response = hosted["client"].post(
        "/v1/inference/chatgpt/complete", headers=hosted["headers"], json={"redirect_url": pasted}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"connected": True}
    assert "protected" not in response.text, "Tokens never reach the renderer"
    assert calls == [
        {"state": params["state"][0], "code": "one-time-code", "client_id": "oaiapp_test", "error": None}
    ]
    profile = app.state.service.vault.read("plan-" + principal(hosted)["user_id"])
    assert profile["access_token"] == "protected-access" and not profile.get("pending")


@pytest.mark.parametrize(
    "address",
    [
        "https://evil.example/auth/callback?code=c&state={state}",
        "http://127.0.0.1:1455/elsewhere?code=c&state={state}",
        "http://127.0.0.1:1455/auth/callback?code=c&state=wrong",
    ],
)
def test_hosted_completion_rejects_other_addresses_and_states(hosted, app, monkeypatch, address):
    flow = start(hosted)
    state = parse_qs(urlsplit(flow["authorization_url"]).query)["state"][0]
    finished(monkeypatch, [])
    response = hosted["client"].post(
        "/v1/inference/chatgpt/complete",
        headers=hosted["headers"],
        json={"redirect_url": address.format(state=state)},
    )
    assert response.status_code in {400, 422}, response.text
    assert not app.state.service.vault.read("plan-" + principal(hosted)["user_id"]).get("access_token")


def test_a_completed_hosted_sign_in_cannot_be_replayed(hosted, monkeypatch):
    flow = start(hosted)
    state = parse_qs(urlsplit(flow["authorization_url"]).query)["state"][0]
    finished(monkeypatch, [])
    body = {"redirect_url": f"{LOOPBACK}?code=c&state={state}&client_id=oaiapp_test"}
    first = hosted["client"].post("/v1/inference/chatgpt/complete", headers=hosted["headers"], json=body)
    assert first.status_code == 200, first.text
    again = hosted["client"].post("/v1/inference/chatgpt/complete", headers=hosted["headers"], json=body)
    assert again.status_code == 400, again.text


def test_local_mode_keeps_its_own_loopback_listener(workspace):
    response = workspace["client"].post(
        "http://127.0.0.1:8765/v1/inference/chatgpt/start", headers=workspace["headers"]
    )
    assert response.status_code == 200, response.text
    flow = response.json()
    assert flow["completion"] == "loopback"
    redirect = parse_qs(urlsplit(flow["authorization_url"]).query)["redirect_uri"][0]
    assert urlsplit(redirect).hostname == "127.0.0.1" and redirect.endswith("/auth/callback")
