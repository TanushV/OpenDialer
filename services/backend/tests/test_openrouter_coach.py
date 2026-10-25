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
    assert body["stream"] is False and body["parallel_tool_calls"] is False
    assert body["tools"][0]["function"]["parameters"]["required"] == ["fingerprint", "verdicts"]
    assert adapter.last_request_id == "review-123" and adapter.last_usage["cost"] == 0.0001


def completion(provider="Cerebras", content=None, **overrides):
    if content and "fingerprint" in content and "choices" not in overrides:
        overrides["choices"] = [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "review-action",
                            "function": {"name": "submit_postcall_review", "arguments": json.dumps(content)},
                        }
                    ]
                },
            }
        ]
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
    # Reasoning tokens count against the completion cap even when excluded from the response.
    assert "max_tokens" not in body and body["max_completion_tokens"] >= 4096
    assert body["reasoning"] == {"effort": "low", "exclude": True}
    assert captured[0].headers["x-openrouter-metadata"] == "enabled"
    assert "usage" not in body, "Usage accounting is always returned; usage.include is retired"
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


UNSUPPORTED_KEYWORDS = {"minItems", "maxItems", "minLength", "maxLength"}


def objects(node):
    if isinstance(node, dict):
        if "properties" in node or node.get("type") == "object":
            yield node
        for key, value in node.items():
            if key in {"properties", "$defs"}:
                for child in value.values():
                    yield from objects(child)
            else:
                yield from objects(value)
    elif isinstance(node, list):
        for item in node:
            yield from objects(item)


def keywords(node):
    if isinstance(node, list):
        for item in node:
            yield from keywords(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in {"properties", "$defs"}:
                for child in value.values():
                    yield from keywords(child)
            else:
                yield key
                yield from keywords(value)


TURN = {
    "state_version": 1,
    "context_fingerprint": "fixture",
    "state_delta": {
        "stage": "discovery",
        "observations": [],
        "rubric_items": [],
        "tracker_matches": [],
        "interpreted_segments": [{"kind": "segment", "id": "s1", "version": 1}],
    },
    "field_audit": [],
    "guidance": None,
}


@pytest.mark.asyncio
async def test_wire_schemas_are_cerebras_strict_while_pydantic_keeps_the_bounds():
    """Cerebras strict mode rejects length/item bounds and needs closed, fully required objects."""
    from app.grounding import review_request

    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(
            body["tools"][0]["function"]["parameters"]
            if "tools" in body
            else body["response_format"]["json_schema"]["schema"]
        )
        review = json.loads(body["messages"][1]["content"])
        if "fingerprint" not in review:
            return httpx.Response(200, json=completion(content=TURN))
        verdict = {"field": "f", "verdict": "SUPPORTED", "evidence": ["k"]}
        answer = {"fingerprint": review["fingerprint"], "verdicts": [verdict] * 101}
        return httpx.Response(200, json=completion(content=answer))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = OpenRouterCoach("fixture", http=http)
        await adapter.suggest({"state_version": 1})
        with pytest.raises(InferenceError):
            # 101 verdicts exceed the pydantic bound that the wire schema no longer carries.
            await adapter.review(review_request("guidance", [{"field": "f", "evidence": []}], [{"id": "k"}]))
    for schema in sent:
        assert not UNSUPPORTED_KEYWORDS & set(keywords(schema))
        for node in objects(schema):
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])
    assert list(sent[0]["properties"])[:3] == ["state_version", "context_fingerprint", "state_delta"], (
        "State is generated before optional guidance so truncation cannot discard the interpretation"
    )


@pytest.mark.asyncio
async def test_oversize_generated_guidance_is_still_rejected_after_the_response():
    oversize = {
        **TURN,
        "guidance": {
            "kind": "DISCOVERY",
            "say_or_ask": "x" * 1001,
            "reason": "",
            "alternatives": [],
            "evidence_segment_ids": [],
            "playbook_card_ids": [],
            "expires_in_ms": 20000,
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion(content=oversize)))
    ) as http:
        with pytest.raises(InferenceError):
            await OpenRouterCoach("fixture", http=http).suggest({"state_version": 1})


def metadata(provider):
    return {"openrouter_metadata": {"endpoints": {"available": [{"provider": provider}], "selected": 0}}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity,verified",
    [
        ({"provider": "Cerebras"}, True),
        ({"provider": None, **metadata("Cerebras")}, True),
        ({"provider": None, **metadata("Another provider")}, "provider_mismatch"),
        ({"provider": None}, "provider_unverified"),
    ],
)
async def test_provider_identity_comes_from_the_top_level_or_routing_metadata(identity, verified):
    from app.providers.openrouter import RouteNotVerified

    payload = {k: v for k, v in {**completion(content=TURN), **identity}.items() if v is not None}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        adapter = OpenRouterCoach("fixture", http=http)
        if verified is True:
            result = await adapter.suggest({"state_version": 1})
            assert result["action"] == "NO_ACTION" and adapter.last_provider == "Cerebras"
        else:
            with pytest.raises(RouteNotVerified) as failure:
                await adapter.suggest({"state_version": 1})
            assert failure.value.reason == verified
        assert adapter.last_request_id == "gen-123" and adapter.last_usage["cost"] == 0.0001


@pytest.mark.asyncio
async def test_truncated_turn_keeps_its_complete_state_but_shows_nothing():
    content = json.dumps(TURN)
    truncated = content[: content.index('"field_audit"')] + '"field_audit": [{"field": "say_or'
    payload = completion(choices=[{"finish_reason": "length", "message": {"content": truncated}}])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        result = await OpenRouterCoach("fixture", http=http).suggest({"state_version": 1})
    assert result["action"] == "NO_ACTION"
    assert result["state_update"] == TURN["state_delta"]


@pytest.mark.asyncio
async def test_settlement_uses_the_always_returned_usage_cost():
    from app.privacy import usage_metadata

    payload = completion(content=TURN)
    payload["usage"] = {"cost": 0.0003, "cost_details": {"upstream_inference_cost": None}, "is_byok": False}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        adapter = OpenRouterCoach("fixture", http=http)
        await adapter.suggest({"state_version": 1})
    assert usage_metadata(adapter.last_usage)["cost"] == 0.0003
