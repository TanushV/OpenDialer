from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from app.providers.oauth import OAuthError, PlanOAuth
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

NOW = datetime(2026, 10, 14, 18, tzinfo=UTC)


def test_oauth_stable_host_pkce_and_exact_callback():
    oauth = PlanOAuth("urn:uuid:host", clock=lambda: NOW)
    pending = oauth.begin("http://127.0.0.1:12345/auth/callback")
    params = parse_qs(urlsplit(pending["authorization_url"]).query)
    assert params["client_id"] == ["dynamic_agent_client"]
    assert params["ext_agent_host_id"] == ["urn:uuid:host"]
    assert params["code_challenge_method"] == ["S256"]
    assert len(pending["verifier"]) >= 43
    with pytest.raises(OAuthError):
        oauth.begin("https://evil.example/callback")


@pytest.mark.asyncio
async def test_oauth_checks_state_issued_client_signature_nonce_scope_and_identity():
    private = generate_private_key(public_exponent=65537, key_size=2048)
    key = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    key["kid"] = "test-key"
    oauth = PlanOAuth("urn:uuid:host", clock=lambda: NOW)
    pending = oauth.begin("http://127.0.0.1:12345/auth/callback")
    token = jwt.encode(
        {
            "iss": "https://auth.openai.com",
            "aud": "oaiapp_test",
            "sub": "subject",
            "nonce": pending["nonce"],
            "exp": NOW.timestamp() + 3600,
            "iat": NOW.timestamp(),
        },
        private,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    def handler(request):
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "protected-access",
                    "refresh_token": "protected-refresh",
                    "id_token": token,
                    "scope": "openid email chatgpt.tokens.use.direct",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(200, json={"keys": [key]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OAuthError):
            await oauth.finish({**pending}, state="wrong", code="code", client_id="oaiapp_test", http=http)
        result = await oauth.finish(
            pending, state=pending["state"], code="code", client_id="oaiapp_test", http=http
        )
        assert result["subject"] == "subject" and result["client_id"] == "oaiapp_test"
        with pytest.raises(OAuthError):
            await oauth.finish(
                pending, state=pending["state"], code="code", client_id="oaiapp_test", http=http
            )


@pytest.mark.asyncio
async def test_registered_client_refreshes_and_saves_before_inference():
    import json

    from app.providers.oauth import RegisteredPlanClient

    profile = {
        "client_id": "test-client",
        "subject": "test-subject",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "scopes": ["chatgpt.tokens.use.direct"],
        "expires_at": NOW.timestamp() - 1,
    }
    saved, seen = [], []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/oauth/token"):
            assert parse_qs(request.content.decode())["refresh_token"] == ["old-refresh"]
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 120,
                },
            )
        assert saved[0]["access_token"] == "new-access"
        assert request.headers["authorization"] == "Bearer new-access"
        events = [
            {"type": "response.output_text.delta", "delta": '{"answer":"ok"}'},
            {"type": "response.completed", "response": {"id": "receipt", "usage": {"input_tokens": 7}}},
        ]
        return httpx.Response(200, text="".join("data: " + json.dumps(e) + "\n\n" for e in events))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = RegisteredPlanClient(
            lambda: profile, saved.append, "test-model", "test-host", lambda: NOW, http=http
        )
        result = await client.complete("Return JSON", {}, {"type": "object"})
    assert result.data == {"answer": "ok"} and result.request_id == "receipt"
    assert result.usage == {"input_tokens": 7}
    assert saved[0]["expires_at"] == NOW.timestamp() + 120
    assert saved[0]["refresh_token"] == "new-refresh"
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_revoked_plan_scope_stops_before_inference_or_saving():
    from app.providers.oauth import RegisteredPlanClient

    saved, seen = [], []
    profile = {
        "client_id": "test-client",
        "subject": "test-subject",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "scopes": ["chatgpt.tokens.use.direct"],
        "expires_at": NOW.timestamp() - 1,
    }

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"access_token": "new-access", "scope": "openid"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = RegisteredPlanClient(
            lambda: profile, saved.append, "test-model", "test-host", lambda: NOW, http=http
        )
        with pytest.raises(OAuthError, match="revoked"):
            await client.complete("Return JSON", {}, {"type": "object"})
    assert seen == ["/api/accounts/oauth/token"] and not saved
