"""R10.1: the live helper's route is OpenRouter served only by Cerebras, one streamed plain-text reply per turn."""

import json

import httpx
import pytest
from app.helper.live import QUIET
from app.providers.inference import InferenceError
from app.providers.openrouter import OpenRouterCoach, RouteNotVerified

MESSAGES = [
    {"role": "system", "content": "Coach the rep."},
    {"role": "user", "content": "Rep: How do you handle this today?\nProspect: Spreadsheets, mostly."},
]


def stream_frames(texts, provider="Cerebras", finish="stop", **extra):
    frames = [
        {
            "id": "gen-1",
            "provider": provider,
            "model": "fixture",
            "choices": [{"index": 0, "delta": {"content": t}}],
        }
        for t in texts
    ]
    frames.append(
        {
            "id": "gen-1",
            "provider": provider,
            "model": "fixture",
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            "usage": {"cost": 0.0003},
            **extra,
        }
    )
    frames = [{k: v for k, v in f.items() if v is not None} for f in frames]
    return "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"


def streaming(text):
    return lambda request: httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})


@pytest.mark.asyncio
async def test_a_reply_streams_on_the_pinned_route_and_is_shown_as_it_grows():
    sent, shown = [], []
    words = ["Ask ", "how long ", "the spreadsheet ", "takes each ", "week, ", "and who ", "keeps it."]

    def handler(request):
        sent.append(json.loads(request.content))
        return streaming(stream_frames(words))(request)

    async def progress(text):
        shown.append(text)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = OpenRouterCoach("key", http=http)
        text = await adapter.converse(MESSAGES, on_progress=progress, quiet=QUIET, deadline_ms=5000)
    body = sent[0]
    assert body["stream"] is True and "response_format" not in body and "tools" not in body
    assert body["provider"]["only"] == ["cerebras"] and body["provider"]["allow_fallbacks"] is False
    assert body["provider"]["zdr"] is True and body["provider"]["data_collection"] == "deny"
    assert body["messages"] == MESSAGES
    assert text == "".join(words)
    assert shown and all(text.startswith(partial) for partial in shown)
    assert adapter.first_text_ms is not None and adapter.last_request_id == "gen-1"
    assert adapter.last_usage == {"cost": 0.0003}


@pytest.mark.asyncio
async def test_a_quiet_reply_is_never_shown_while_it_streams():
    shown = []

    async def progress(text):
        shown.append(text)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(streaming(stream_frames(["(noth", "ing)"])))
    ) as http:
        text = await OpenRouterCoach("key", http=http).converse(
            MESSAGES, on_progress=progress, quiet=QUIET, deadline_ms=5000
        )
    assert text == QUIET and shown == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "reason"), [("OpenAI", "provider_mismatch"), (None, "provider_unverified")]
)
async def test_a_reply_from_another_route_is_refused_and_never_shown(provider, reason):
    shown = []

    async def progress(text):
        shown.append(text)

    transport = httpx.MockTransport(
        streaming(stream_frames(["Ask about timing and who decides."], provider=provider))
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(RouteNotVerified) as failure:
            await OpenRouterCoach("key", http=http).converse(MESSAGES, on_progress=progress, deadline_ms=5000)
    assert failure.value.reason == reason and shown == []


@pytest.mark.asyncio
async def test_the_provider_may_be_named_only_in_routing_metadata():
    metadata = {
        "openrouter_metadata": {"endpoints": {"available": [{"provider": "Cerebras"}], "selected": 0}}
    }
    transport = httpx.MockTransport(
        streaming(stream_frames(["Ask about timing."], provider=None, **metadata))
    )
    async with httpx.AsyncClient(transport=transport) as http:
        adapter = OpenRouterCoach("key", http=http)
        assert await adapter.converse(MESSAGES, deadline_ms=5000) == "Ask about timing."
    assert adapter.last_provider == "Cerebras"


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["content_filter", None])
async def test_an_unfinished_or_filtered_reply_is_a_failure(finish):
    text = (
        stream_frames(["Ask about timing."], finish=finish)
        if finish
        else stream_frames([])[: -len("data: [DONE]\n\n")]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(streaming(text))) as http:
        with pytest.raises(InferenceError):
            await OpenRouterCoach("key", http=http).converse(MESSAGES, deadline_ms=5000)


@pytest.mark.asyncio
async def test_a_rate_limit_is_not_retried_or_sent_elsewhere():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"error": "rate limited"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await OpenRouterCoach("key", http=http).converse(MESSAGES, deadline_ms=5000)
    assert len(calls) == 1


def test_model_and_provider_settings_are_secure_and_do_not_erase_other_provider_keys(workspace):
    c, h = workspace["client"], workspace["headers"]
    c.put("/v1/admin/settings", headers=h, json={"telnyx_api_key": "existing-test-telnyx"})
    result = c.put(
        "/v1/admin/settings",
        headers=h,
        json={
            "coach_provider": "openrouter",
            "openrouter_api_key": "test-secret-openrouter",
            "coach_model": "openai/gpt-oss-120b",
        },
    )
    assert result.status_code == 200, result.text
    assert "test-secret-openrouter" not in result.text
    assert result.json()["configured"]["openrouter_api_key"]
    assert result.json()["configured"]["telnyx_api_key"]
    assert result.json()["coach_provider"] == "openrouter"


def test_a_simulated_workspace_never_constructs_a_paid_coach(workspace, app):
    from app import live_help

    client, headers = workspace["client"], workspace["headers"]
    p = {"tenant_id": workspace["tenant_id"]}
    service = app.state.service
    assert live_help.provider(service, p) is None  # No key: no helper.
    client.put("/v1/admin/settings", headers=headers, json={"openrouter_api_key": "test-only-never-use"})
    assert service.settings.simulation
    assert type(live_help.provider(service, p)).__name__ == "SimulatedCoach"
