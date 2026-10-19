import json

import httpx
import pytest
from app.providers.inference import InferenceError
from app.providers.openrouter import OpenRouterCoach


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_provider", ["Cerebras", "Another provider"])
async def test_independent_live_review_pins_same_route_and_records_its_own_receipt(actual_provider):
    from app.grounding import review_request

    request = review_request(
        "guidance",
        [{"field": "say_or_ask", "text": "A fact.", "evidence": ["k"]}],
        [{"id": "k", "text": "A fact."}],
    )
    answer = {
        "fingerprint": request["fingerprint"],
        "verdicts": [{"field": "say_or_ask", "verdict": "SUPPORTED", "evidence": ["k"]}],
    }
    captured = []

    def handler(sent):
        captured.append(json.loads(sent.content))
        return httpx.Response(200, json=completion(actual_provider, answer, id="review-123"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = OpenRouterCoach("fixture", http=http)
        if actual_provider == "Cerebras":
            assert await adapter.review(request, deadline_ms=1200) == answer
        else:
            with pytest.raises(InferenceError):
                await adapter.review(request, deadline_ms=1200)
    body = captured[0]
    assert body["provider"] == {
        "only": ["cerebras"],
        "order": ["cerebras"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
    }
    assert body["stream"] is False and "tools" not in body
    assert body["response_format"]["json_schema"]["schema"]["required"] == ["fingerprint", "verdicts"]
    assert adapter.last_request_id == "review-123" and adapter.last_usage["cost"] == 0.0001


def completion(provider="Cerebras", content=None, **overrides):
    return {
        "id": "gen-123",
        "model": "openai/gpt-oss-120b",
        "provider": provider,
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(content or {"action": "NO_ACTION"})}}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 30, "cost": 0.0001},
        **overrides,
    }


@pytest.mark.asyncio
async def test_live_transport_requests_one_typed_turn_contract():
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await OpenRouterCoach("fixture", http=http).suggest({"state_version": 1})
    schema = captured[0]["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == {
        "guidance",
        "state_delta",
        "field_audit",
        "state_version",
        "context_fingerprint",
    }
    assert schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_openrouter_pins_cerebras_requires_schema_and_retains_actual_receipt():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=completion())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = OpenRouterCoach("test-openrouter-key", http=http)
        result = await adapter.suggest({"state_version": 3}, deadline_ms=1200)
    body = json.loads(captured[0].content)
    assert str(captured[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert captured[0].headers["authorization"] == "Bearer test-openrouter-key"
    assert body["provider"]["only"] == ["cerebras"]
    assert body["provider"]["allow_fallbacks"] is False and body["provider"]["require_parameters"] is True
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["max_tokens"] <= 1000 and body["reasoning"]["effort"] in {"minimal", "low"}
    assert not body["stream"] and "tools" not in body
    assert result["action"] == "NO_ACTION"
    assert adapter.last_request_id == "gen-123" and adapter.last_provider == "Cerebras"
    assert adapter.last_usage["cost"] == 0.0001 and adapter.last_latency_ms >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        completion("Another provider"),
        completion(choices=[{"finish_reason": "length", "message": {"content": '{"action":"NO_ACTION"}'}}]),
        completion(choices=[{"finish_reason": "stop", "message": {"refusal": "cannot comply"}}]),
        completion(choices=[{"finish_reason": "stop", "message": {"content": "not JSON"}}]),
    ],
)
async def test_wrong_provider_truncation_refusal_and_invalid_json_are_failures(payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as http:
        with pytest.raises(InferenceError):
            await OpenRouterCoach("test", http=http).suggest({})


@pytest.mark.asyncio
async def test_rate_limit_does_not_retry_or_switch_provider():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, json={"error": {"message": "rate limit"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await OpenRouterCoach("test", http=http).suggest({})
    assert len(requests) == 1


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


async def test_reused_adapter_does_not_attribute_the_previous_receipt_to_a_failed_attempt():
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(200, json=completion()) if len(attempts) == 1 else httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = OpenRouterCoach("fixture", http=http)
        await adapter.suggest({})
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.suggest({})
        assert adapter.last_request_id is None and adapter.last_usage == {}
