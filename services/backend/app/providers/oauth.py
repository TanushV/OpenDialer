import base64
import hashlib
import secrets
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit

import httpx
import jwt


class OAuthError(ValueError):
    pass


class PlanOAuth:
    issuer = "https://auth.openai.com"
    resource = "https://api.openai.com/v1"

    def __init__(self, host_id, clock=None):
        self.host_id = host_id
        self.clock = clock or (lambda: datetime.now(UTC))

    def begin(self, redirect_uri, registration=None):
        uri = urlsplit(redirect_uri)
        if (
            uri.scheme != "http"
            or uri.hostname != "127.0.0.1"
            or uri.path != "/auth/callback"
            or uri.query
            or uri.fragment
        ):
            raise OAuthError("Use a bound 127.0.0.1 /auth/callback listener")
        registration = registration or {}
        verifier = secrets.token_urlsafe(64)
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        client_id = registration.get("client_id", "dynamic_agent_client")
        params = {
            "client_id": client_id,
            "ext_agent_host_id": self.host_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
            "resource": self.resource,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("="),
        }
        if client_id == "dynamic_agent_client":
            params["agent_name_hint"] = "OpenDialer"
        elif registration.get("id_token"):
            params["id_token_hint"] = registration["id_token"]
        return {
            "authorization_url": self.issuer + "/api/accounts/authorize?" + urlencode(params),
            "verifier": verifier,
            "state": state,
            "nonce": nonce,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "created_at": self.clock().timestamp(),
            "used": False,
            "selected_subject": registration.get("subject"),
        }

    async def identity(self, id_token, client_id, http, nonce=None):
        response = await http.get(self.issuer + "/.well-known/jwks.json", timeout=10)
        response.raise_for_status()
        try:
            header = jwt.get_unverified_header(id_token)
            jwk = next(k for k in response.json()["keys"] if k.get("kid") == header.get("kid"))
            if header.get("alg") not in {"RS256", "ES256"}:
                raise OAuthError("Unapproved token signature algorithm")
            claims = jwt.decode(
                id_token,
                jwt.PyJWK.from_dict(jwk).key,
                algorithms=[header["alg"]],
                issuer=self.issuer,
                audience=client_id,
                options={"require": ["sub", "exp", "iat"], "verify_exp": False, "verify_iat": False},
            )
            now = self.clock().timestamp()
            if claims["exp"] < now - 5 or claims["iat"] > now + 5 or not claims.get("sub"):
                raise OAuthError("Expired or invalid OpenID identity")
            if nonce is not None and not secrets.compare_digest(claims.get("nonce", ""), nonce):
                raise OAuthError("OIDC nonce mismatch")
            return claims
        except (jwt.PyJWTError, StopIteration, KeyError, TypeError) as exc:
            raise OAuthError("OpenID identity validation failed") from exc

    async def finish(self, pending, *, state, code=None, client_id=None, error=None, http=None):
        if (
            pending.get("used")
            or self.clock().timestamp() - pending["created_at"] > 600
            or not secrets.compare_digest(state or "", pending["state"])
        ):
            raise OAuthError("Expired, replayed or mismatched OAuth state")
        pending["used"] = True
        if error or not code:
            raise OAuthError("Authorization denied or incomplete")
        saved = pending["client_id"]
        issued = client_id or saved
        if issued == "dynamic_agent_client" or (saved != "dynamic_agent_client" and issued != saved):
            raise OAuthError("Issued registration client ID missing or changed")

        async def exchange(client):
            response = await client.post(
                self.issuer + "/api/accounts/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": issued,
                    "code": code,
                    "code_verifier": pending["verifier"],
                    "redirect_uri": pending["redirect_uri"],
                    "resource": self.resource,
                },
                timeout=15,
            )
            response.raise_for_status()
            result = response.json()
            identity = await self.identity(result["id_token"], issued, client, pending["nonce"])
            if pending.get("selected_subject") and identity["sub"] != pending["selected_subject"]:
                raise OAuthError("Returning account identity changed")
            scopes = result.get("scope", "").split()
            if "chatgpt.tokens.use.direct" not in scopes:
                raise OAuthError("ChatGPT plan permission was not granted")
            return {
                **result,
                "client_id": issued,
                "issuer": self.issuer,
                "subject": identity["sub"],
                "email": identity.get("email"),
                "ext_agent_host_id": self.host_id,
                "scopes": scopes,
                "expires_at": self.clock().timestamp() + int(result.get("expires_in", 3600)),
                "saved_at": self.clock().isoformat(),
            }

        if http:
            return await exchange(http)
        async with httpx.AsyncClient() as client:
            return await exchange(client)

    async def refresh(self, registration):
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.issuer + "/api/accounts/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": registration["client_id"],
                    "refresh_token": registration["refresh_token"],
                    "resource": self.resource,
                },
                timeout=15,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("id_token"):
                claims = await self.identity(result["id_token"], registration["client_id"], client)
                if claims["sub"] != registration["subject"]:
                    raise OAuthError("Refreshed account identity changed")
            scopes = result.get("scope", " ".join(registration["scopes"])).split()
            if "chatgpt.tokens.use.direct" not in scopes:
                raise OAuthError("Plan permission revoked")
            return {
                **registration,
                **result,
                "scopes": scopes,
                "expires_at": self.clock().timestamp() + int(result.get("expires_in", 3600)),
                "saved_at": self.clock().isoformat(),
            }


class RegisteredPlanClient:
    def __init__(self, load, save, model, host_id, clock):
        self.load, self.save, self.model = load, save, model
        self.oauth = PlanOAuth(host_id, clock)

    async def complete(self, instructions, context, schema):
        from app.providers.inference import InferenceClient

        profile = self.load()
        if profile.get("expires_at", 0) and profile["expires_at"] < self.oauth.clock().timestamp() + 30:
            if not profile.get("refresh_token"):
                raise OAuthError("Plan session expired; sign in again")
            profile = await self.oauth.refresh(profile)
            self.save(profile)
        if not profile.get("access_token"):
            raise OAuthError("A personal eligible ChatGPT registration is required")
        prompt = (
            instructions
            + "\nReturn only a JSON object matching this schema: "
            + __import__("json").dumps(schema)
        )
        return await InferenceClient("chatgpt_plan", profile["access_token"], self.model).complete(
            prompt, context, schema
        )
