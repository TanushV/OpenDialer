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


@pytest.mark.asyncio
async def test_plan_requests_settle_at_zero_marginal_cost_labeled_as_plan_usage(
    workspace, app, clock, monkeypatch
):
    """Plan-subsidised work must not consume the metered optional budget or stay uncertain."""
    from app.db import MODELS
    from app.providers.inference import InferenceClient, InferenceResult
    from sqlalchemy import select

    service, p = app.state.service, principal(workspace)
    service.vault.write(p["tenant_id"], {"inference_mode": "chatgpt_plan"})
    service.vault.write(
        "plan-" + p["user_id"],
        {
            "access_token": "plan-test",
            "expires_at": clock().timestamp() + 3600,
            "scopes": ["chatgpt.tokens.use.direct"],
        },
    )
    provider = service.inference(p["tenant_id"], "script_write", p["user_id"])
    assert provider.billing == "chatgpt_plan"

    async def complete(self, instructions, context, schema):
        return InferenceResult({}, {"input_tokens": 120, "output_tokens": 40}, "plan-request-1")

    monkeypatch.setattr(InferenceClient, "complete", complete)
    result = await provider.complete("Write", {}, {"type": "object"})
    key = "script:plan-test"
    assert service.reserve_optional_budget(p, key, 0.05, object_id="lead-plan", work_kind="script_write")
    service.usage(p, provider.billing, "script_write", result, object_id="lead-plan", request_key=key)
    with service.db.transaction(False) as s:
        rows = [r.data for r in s.scalars(select(MODELS["usage_ledger"])) if r.data.get("request_key") == key]
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "chatgpt_plan" and row["status"] == "settled"
    assert row["cost_usd"] == 0 and row["reserved_usd"] == 0
    assert row["billing_verified"] is False and row["estimate_source"] == "chatgpt_plan_included"
    assert row["usage"]["input_tokens"] == 120, "Token counts stay visible for plan limits"
