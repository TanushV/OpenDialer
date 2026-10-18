"""Documented OpenRouter SSE transport: no incomplete or unverified advice is published."""

import asyncio
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("id", "different-request"), ("provider", "Other"), ("model", "different-model")]
)
async def test_stream_identity_cannot_change_and_then_revert(field, value):
    original = frames()
    lines = original.splitlines(keepends=True)
    positions = [i for i, line in enumerate(lines) if line.startswith("data: {")]
    changed = json.loads(lines[positions[1]][6:])
    changed[field] = value
    lines[positions[1]] = "data: " + json.dumps(changed) + "\n"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="".join(lines)))
    ) as http:
        provider = OpenRouterCoach("fixture", http=http)
        with pytest.raises(InferenceError):
            await provider.suggest({"state_version": 1, "private_question": "Explain"})
        # A contradictory later identity cannot steal the original request's liability.
        assert provider.last_request_id == "stream-one"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    [
        [],
        {"choices": [None]},
        {"choices": [{"delta": {"content": 42}}]},
        {"choices": [{"index": 1, "delta": {"content": "other answer"}}]},
    ],
)
async def test_malformed_stream_frames_fail_through_the_provider_contract(frame):
    text = "data: " + json.dumps(frame) + "\n\n" + frames()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=text))
    ) as http:
        with pytest.raises(InferenceError):
            await OpenRouterCoach("fixture", http=http).suggest({"private_question": "Explain"})


@pytest.mark.asyncio
async def test_terminal_accounting_frame_does_not_reopen_content_or_overwrite_an_error():
    text = frames(finish="error", done=False)
    text += 'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"cost":0.001}}\n\n'
    text += "data: [DONE]\n\n"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=text))
    ) as http:
        with pytest.raises(InferenceError):
            await OpenRouterCoach("fixture", http=http).suggest({"private_question": "Explain"})


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_only_accepted_primary_help_unlocks_the_longer_explanation_deadline(accepted):
    from test_helper_progress import frame

    primary = {k: v for k, v in advice().items() if k not in {"reason", "state_update"}}
    shown = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield frame(json.dumps(primary)[:-1] + ', "reason": "')
            await asyncio.sleep(0.08)
            yield frame('Explanation", "state_update": null}')
            yield frame(choices=[{"delta": {}, "finish_reason": "stop"}])
            yield b"data: [DONE]\n\n"

    async def progress(result):
        shown.append(result)
        return accepted

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Stream()))
    ) as http:
        provider = OpenRouterCoach("fixture", http=http)
        provider.first_useful_ms = 30
        provider.on_progress = progress
        if accepted:
            result = await provider.suggest({"private_question": "Explain"}, deadline_ms=400)
            assert result["reason"] == "Explanation"
        else:
            with pytest.raises(TimeoutError):
                await provider.suggest({"private_question": "Explain"}, deadline_ms=400)
        assert len(shown) == 1


@pytest.mark.asyncio
async def test_stream_comments_cannot_extend_the_full_response_deadline():
    from test_helper_progress import frame

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield frame(json.dumps(advice()))
            for _ in range(20):
                await asyncio.sleep(0.01)
                yield b": keepalive\n\n"

    async def progress(_):
        return True

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Stream()))
    ) as http:
        provider = OpenRouterCoach("fixture", http=http)
        provider.first_useful_ms = 30
        provider.on_progress = progress
        with pytest.raises(TimeoutError):
            await provider.suggest({"private_question": "Explain"}, deadline_ms=60)
