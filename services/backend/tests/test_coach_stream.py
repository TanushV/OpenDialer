"""Documented OpenRouter SSE transport: no incomplete or unverified advice is published."""

import json

import httpx
import pytest
from app.providers.inference import InferenceError
from app.providers.openrouter import OpenRouterCoach
from test_remediation_coach import advice


def frames(*, provider="Cerebras", finish="stop", done=True, error=False):
    encoded = json.dumps(advice())
    result = ": keep-alive\n\n"
    for fragment in (encoded[:30], encoded[30:]):
        result += (
            "data: "
            + json.dumps(
                {
                    "id": "stream-one",
                    "provider": provider,
                    "model": "fixture",
                    "choices": [{"delta": {"content": fragment}, "finish_reason": None}],
                }
            )
            + "\n\n"
        )
    if error:
        result += (
            "data: " + json.dumps({"error": {"message": "private provider text must not escape"}}) + "\n\n"
        )
    result += (
        "data: "
        + json.dumps(
            {
                "id": "stream-one",
                "provider": provider,
                "choices": [{"delta": {}, "finish_reason": finish}],
                "usage": {"cost": 0.001, "completion_tokens": 20},
            }
        )
        + "\n\n"
    )
    return result + ("data: [DONE]\n\n" if done else "")


@pytest.mark.asyncio
async def test_manual_help_uses_streaming_schema_and_preserves_final_usage():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, text=frames(), headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = OpenRouterCoach("fixture", http=http)
        result = await provider.suggest(
            {"state_version": 1, "private_question": "Explain the constraint"}, deadline_ms=8000
        )
    assert seen[0]["stream"] is True
    assert seen[0]["response_format"]["type"] == "json_schema"
    assert result["say_or_ask"] == advice()["say_or_ask"]
    assert provider.last_usage["cost"] == 0.001
    assert provider.last_request_id == "stream-one"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes", [{"provider": "other"}, {"finish": "length"}, {"done": False}, {"error": True}]
)
async def test_manual_stream_errors_never_publish_partial_content(changes):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=frames(**changes)))
    ) as http:
        with pytest.raises(InferenceError):
            await OpenRouterCoach("fixture", http=http).suggest(
                {"state_version": 1, "private_question": "Explain"}
            )
