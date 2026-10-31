import json

import httpx
import pytest
from app.providers.inference import InferenceClient, InferenceError
from app.providers.telnyx import TelnyxCarrier


@pytest.mark.asyncio
async def test_telnyx_dial_contract_and_no_unbounded_retries():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"call_control_id": "cc-001"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = TelnyxCarrier("secret", "connection-001", http=http)
        result = await provider.dial(
            {
                "from": "+14155550100",
                "to": "+14155550101",
                "link_to": "agent",
                "command_id": "fixed-id",
                "bridge_on_answer": True,
                "prevent_double_bridge": True,
                "time_limit_secs": 7200,
            }
        )
    assert result["call_control_id"] == "cc-001"
    assert len(requests) == 1
    assert requests[0].url.path == "/v2/calls"
    payload = json.loads(requests[0].content)
    assert payload["connection_id"] == "connection-001"
    assert payload["command_id"] == "fixed-id"
    assert "bridge_intent" not in payload


@pytest.mark.asyncio
async def test_scoped_telnyx_token_not_master_key():
    def handler(request):
        assert request.url.path == "/v2/telephony_credentials/cred-123/token"
        return httpx.Response(201, text="scoped-token")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        token = await TelnyxCarrier("secret-master", "connection", http=http).token("cred-123")
    assert token == "scoped-token"
    assert "secret-master" not in token


@pytest.mark.asyncio
async def test_openai_responses_contract_and_usage():
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "req-1",
                "output": [
                    {"type": "reasoning", "summary": []},
                    {"type": "message", "content": [{"type": "output_text", "text": '{"answer":"ok"}'}]},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await InferenceClient("api", "secret", "evaluated-model", http=http).complete(
            "Return a JSON answer", {"untrusted": "source text"}, {"type": "object"}
        )
    assert result.data == {"answer": "ok"}
    assert result.usage["input_tokens"] == 10
    assert captured[0]["store"] is False
    assert captured[0]["text"]["format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_litellm_explicit_openai_compatible_route():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "litellm-test",
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await InferenceClient(
            "litellm", "configured-secret", "user-model", base_url="http://127.0.0.1:4000/v1", http=http
        ).complete("Return JSON", {}, {"type": "object"})
    assert result.data["ok"]
    assert str(captured[0].url) == "http://127.0.0.1:4000/v1/chat/completions"


@pytest.mark.asyncio
async def test_provider_refusal_does_not_become_empty_success():
    def handler(request):
        return httpx.Response(
            200, json={"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(InferenceError):
            await InferenceClient("api", "secret", "model", http=http).complete("JSON", {}, {})


@pytest.mark.asyncio
async def test_api_agent_loops_tools_with_web_search_and_follow_up_turns():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            output = [
                {"type": "web_search_call", "status": "completed"},
                {
                    "type": "function_call",
                    "name": "finish_research",
                    "call_id": "c1",
                    "arguments": '{"lead":"lead-1","summary":"x"}',
                },
            ]
        elif len(bodies) == 2:
            output = [{"type": "function_call", "name": "nope", "call_id": "c2", "arguments": "{}"}]
        else:
            output = [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}]
        return httpx.Response(200, json={"output": output, "usage": {"input_tokens": 10, "output_tokens": 2}})

    seen, turns = [], iter(["Close lead-1.", None])

    async def handle(name, args):
        seen.append((name, args))
        return json.dumps({"ok": True})

    tools = [{"name": "finish_research", "description": "d", "schema": {"type": "object"}}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        usage = await InferenceClient("api", "k", "model", http=http).run(
            "Instructions", tools, handle, "first", lambda: next(turns)
        )
    assert seen == [("finish_research", {"lead": "lead-1", "summary": "x"})]
    assert usage == {"input_tokens": 40, "output_tokens": 8, "web_search_calls": 1}
    assert {"type": "web_search"} in bodies[0]["tools"] and bodies[0]["store"] is False
    assert bodies[2]["input"][-1]["output"].startswith('{"ok": false')
    assert bodies[3]["input"][-1] == {"role": "user", "content": "Close lead-1."}
    compatible = InferenceClient("litellm", "k", "m", base_url="http://127.0.0.1:4000/v1")
    assert not compatible.official
