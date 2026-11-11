"""The live helper's model route (R10.1): OpenRouter, served only by Cerebras, with no retention or data collection.

One streamed selection per turn. The app receives the completed reply after the route is verified as Cerebras.
"""

import asyncio
import json
import time

import httpx

from app.platform.config import PROVIDER_DEFAULTS
from app.providers.inference import InferenceError

URL = "https://openrouter.ai/api/v1/chat/completions"
# Cerebras only, no fallback provider, no data collection or retention.
ROUTE = {
    "only": ["cerebras"],
    "order": ["cerebras"],
    "allow_fallbacks": False,
    "require_parameters": True,
    "data_collection": "deny",
    "zdr": True,
}
RESPONSE_BYTES = 262144
# Reasoning tokens count against the completion cap even with reasoning excluded from the response.
MAX_COMPLETION_TOKENS = 4096
# An optional progress callback runs at most this often.
PROGRESS_SECONDS = 0.25
PROGRESS_CHARS = 24


class RouteNotVerified(InferenceError):
    """The response did not prove it was served by the pinned Cerebras route."""

    def __init__(self, reason):
        super().__init__("The response does not verify the required Cerebras provider")
        self.reason = reason


def headers(api_key):
    return {
        "Authorization": "Bearer " + api_key,
        "X-OpenRouter-Title": "OpenDialer",
        "X-OpenRouter-Metadata": "enabled",
    }


def provider_identity(payload):
    """The serving provider from the top-level field or OpenRouter's routing metadata."""
    value = payload.get("provider")
    if value is None:
        endpoints = (payload.get("openrouter_metadata") or {}).get("endpoints") or {}
        available, selected = endpoints.get("available"), endpoints.get("selected")
        entry = None
        if isinstance(selected, dict):
            entry = selected
        elif isinstance(available, list) and type(selected) is int and 0 <= selected < len(available):
            entry = available[selected]
        elif isinstance(available, dict) and isinstance(selected, str):
            entry = available.get(selected)
        value = entry.get("provider") if isinstance(entry, dict) else None
    return value


def is_cerebras(provider):
    return isinstance(provider, str) and provider.casefold().split("/")[0] == "cerebras"


class OpenRouterCoach:
    def __init__(self, api_key, model=PROVIDER_DEFAULTS.coach_model, *, http=None):
        self.api_key, self.model, self.http = api_key, model, http
        self.last_usage, self.last_request_id, self.last_provider = {}, None, None
        self.last_model, self.last_latency_ms, self.first_text_ms = model, 0.0, None
        self.connect_ms = 1000

    async def converse(self, messages, *, on_progress=None, quiet="", deadline_ms=8000):
        """The whole reply text. `on_progress(text)` sees it as it grows, never while it could still be `quiet`."""
        self.last_usage, self.last_request_id, self.last_provider = {}, None, None
        self.first_text_ms = None
        started = time.perf_counter()
        content, identity, state = [], {}, {"finished": None, "done": False, "at": 0.0, "length": 0}

        async def stream(http):
            body = {
                "model": self.model,
                "stream": True,
                "max_completion_tokens": MAX_COMPLETION_TOKENS,
                "reasoning": {"effort": "low", "exclude": True},
                "provider": ROUTE,
                "messages": messages,
            }
            timeout = httpx.Timeout(deadline_ms / 1000, connect=min(self.connect_ms, deadline_ms) / 1000)
            async with http.stream(
                "POST", URL, headers=headers(self.api_key), json=body, timeout=timeout
            ) as r:
                r.raise_for_status()
                received = 0
                async for line in r.aiter_lines():
                    received += len(line.encode())
                    if received > RESPONSE_BYTES:
                        raise InferenceError("Coach stream exceeds its response budget")
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        state["done"] = True
                        break
                    try:
                        frame = json.loads(data)
                    except ValueError as exc:
                        raise InferenceError("Invalid coach stream") from exc
                    if not isinstance(frame, dict) or frame.get("error"):
                        raise InferenceError("Coach stream failed")
                    for key, value in (
                        ("id", frame.get("id")),
                        ("provider", provider_identity(frame)),
                        ("model", frame.get("model")),
                    ):
                        if value is not None:
                            if not isinstance(value, str) or (key in identity and identity[key] != value):
                                raise InferenceError("Conflicting coach receipt identity")
                            identity[key] = value
                    if frame.get("usage"):
                        self.last_usage = frame["usage"]
                    for choice in frame.get("choices") or []:
                        delta = (choice or {}).get("delta") or {}
                        if delta.get("refusal"):
                            raise InferenceError("Coach refused")
                        if isinstance(delta.get("content"), str):
                            content.append(delta["content"])
                        if choice.get("finish_reason"):
                            state["finished"] = choice["finish_reason"]
                    text = "".join(content).strip()
                    if not text or (quiet and quiet.startswith(text[: len(quiet)])):
                        continue
                    if not is_cerebras(identity.get("provider")):
                        continue
                    now = time.perf_counter()
                    if self.first_text_ms is None:
                        self.first_text_ms = round((now - started) * 1000, 3)
                    if (
                        on_progress is not None
                        and len(text) - state["length"] >= PROGRESS_CHARS
                        and now - state["at"] >= PROGRESS_SECONDS
                    ):
                        state["at"], state["length"] = now, len(text)
                        await on_progress(text)

        try:
            async with asyncio.timeout(deadline_ms / 1000):
                if self.http:
                    await stream(self.http)
                else:
                    async with httpx.AsyncClient() as http:
                        await stream(http)
        finally:
            self.last_latency_ms = round((time.perf_counter() - started) * 1000, 3)
            self.last_request_id = identity.get("id")
            self.last_provider = identity.get("provider")
            self.last_model = identity.get("model", self.model)
        if self.last_provider is None:
            raise RouteNotVerified("provider_unverified")
        if not is_cerebras(self.last_provider):
            raise RouteNotVerified("provider_mismatch")
        if not state["done"] or state["finished"] != "stop":
            raise InferenceError("Coach stream did not finish")
        return "".join(content)


async def check_key(api_key, http=None):
    """(ok, detail) from OpenRouter's key endpoint, one authenticated request."""
    from app.providers.telnyx import _check

    return await _check(
        "https://openrouter.ai/api/v1/key",
        api_key,
        "OpenRouter",
        http,
        accepted="OpenRouter accepted this key",
    )
