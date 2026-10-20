"""Documented OpenRouter SSE transport: no incomplete or unverified advice is published."""

import asyncio
import json

import httpx
import pytest
from app.providers.inference import InferenceError
from app.providers.openrouter import OpenRouterCoach
from test_remediation_coach import advice, ordinary_advice


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


def primary_then_explanation(delay=0.08, *, complete_primary=True):
    from test_helper_progress import frame

    primary = {
        k: v
        for k, v in ordinary_advice({"state_version": 1, "context_fingerprint": "stream-fixture"}).items()
        if k not in {"reason", "state_update"}
    }
    opening = json.dumps(primary)[:-1] + ', "reason": "'
    if not complete_primary:
        opening = opening[: opening.index('"field_audit"')]

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield frame(opening)
            await asyncio.sleep(delay)
            if not complete_primary:
                yield frame(json.dumps(primary)[len(opening) :][:-1] + ', "reason": "')
            yield frame('Explanation", "state_update": null}')
            yield frame(choices=[{"delta": {}, "finish_reason": "stop"}])
            yield b"data: [DONE]\n\n"

    return Stream()


@pytest.mark.asyncio
@pytest.mark.parametrize("progress", ["accepted", "withheld", "absent"])
async def test_complete_primary_unlocks_the_full_answer_deadline(progress):
    """The first-useful ceiling bounds the first displayable content, not the reviewed answer.

    A factual primary is withheld until review (on_progress returns False) and plain F1 help has
    no early display at all; neither may be cut off at the first-useful ceiling.
    """
    shown = []

    async def on_progress(result):
        shown.append(result)
        return progress == "accepted"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=primary_then_explanation()))
    ) as http:
        provider = OpenRouterCoach("fixture", http=http)
        provider.first_useful_ms = 30
        provider.on_progress = None if progress == "absent" else on_progress
        result = await provider.suggest({"private_question": "Explain"}, deadline_ms=400)
    assert result["reason"] == "Explanation"
    assert len(shown) == (0 if progress == "absent" else 1)


@pytest.mark.asyncio
async def test_no_complete_primary_before_the_first_useful_ceiling_times_out():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=primary_then_explanation(complete_primary=False))
        )
    ) as http:
        provider = OpenRouterCoach("fixture", http=http)
        provider.first_useful_ms = 30
        with pytest.raises(TimeoutError):
            await provider.suggest({"private_question": "Explain"}, deadline_ms=400)


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


def metadata_stream(content, *, final, finish="stop"):
    """Documented streaming: routing metadata arrives on the final chunk, not on every frame."""
    text = ""
    for fragment in (content[:40], content[40:]):
        delta = {"id": "stream-meta", "model": "fixture", "choices": [{"delta": {"content": fragment}}]}
        text += "data: " + json.dumps(delta) + "\n\n"
    last = {
        "id": "stream-meta",
        "choices": [{"delta": {}, "finish_reason": finish}],
        "usage": {"cost": 0.001},
        **final,
    }
    return text + "data: " + json.dumps(last) + "\n\ndata: [DONE]\n\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "final,verified",
    [
        (
            {"openrouter_metadata": {"endpoints": {"available": [{"provider": "Cerebras"}], "selected": 0}}},
            True,
        ),
        (
            {"openrouter_metadata": {"endpoints": {"available": [{"provider": "Other"}], "selected": 0}}},
            False,
        ),
        ({}, False),
    ],
)
async def test_stream_identity_may_arrive_only_on_the_final_chunk(final, verified):
    from app.providers.openrouter import RouteNotVerified
    from test_openrouter_coach import TURN

    shown = []

    async def progress(result):
        shown.append(provider.last_provider)  # The route identity when display was offered.
        return True

    guided = {
        **TURN,
        "field_audit": [
            {"field": f, "classification": "nonassertive", "sources": [], "implied_premises": []}
            for f in ("say_or_ask", "reason")
        ],
        "guidance": {
            "kind": "DISCOVERY",
            "say_or_ask": "Which part of that would be useful to clarify first?",
            "reason": "Clarify the stated concern.",
            "alternatives": [],
            "evidence_segment_ids": ["s1"],
            "playbook_card_ids": [],
            "expires_in_ms": 20000,
        },
    }
    body = metadata_stream(json.dumps(guided), final=final)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
    ) as http:
        provider = OpenRouterCoach("fixture", http=http)
        provider.on_progress = progress
        if verified:
            result = await provider.suggest({"private_question": "Explain"}, deadline_ms=1000)
            assert result["action"] == "SHOW_CARD" and result["state_update"] == TURN["state_delta"]
            assert provider.last_provider == "Cerebras"
        else:
            with pytest.raises(RouteNotVerified):
                await provider.suggest({"private_question": "Explain"}, deadline_ms=1000)
    assert shown == (["Cerebras"] if verified else []), "Nothing is displayed before verification"
    assert provider.last_request_id == "stream-meta" and provider.last_usage["cost"] == 0.001


@pytest.mark.asyncio
async def test_truncated_stream_keeps_complete_state_without_guidance():
    from test_openrouter_coach import TURN

    content = json.dumps(TURN)
    truncated = content[: content.index('"field_audit"')] + '"field_audit": [{"fi'
    body = metadata_stream(truncated, final={"provider": "Cerebras"}, finish="length")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
    ) as http:
        result = await OpenRouterCoach("fixture", http=http).suggest(
            {"private_question": "Explain"}, deadline_ms=1000
        )
    assert result["action"] == "NO_ACTION" and result["state_update"] == TURN["state_delta"]
