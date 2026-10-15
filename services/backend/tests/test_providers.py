import json

import httpx
import pytest

from app.providers.telnyx import TelnyxCarrier
from app.providers.inference import InferenceClient, InferenceError
from app.providers.groq import GroqCoach


@pytest.mark.asyncio
async def test_telnyx_dial_contract_and_no_unbounded_retries():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"call_control_id": "cc-001"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = TelnyxCarrier("secret", "connection-001", http=http)
        result = await provider.dial({"from": "+14155550100", "to": "+14155550101",
            "link_to": "agent", "command_id": "fixed-id", "bridge_on_answer": True,
            "prevent_double_bridge": True, "time_limit_secs": 7200})
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
        return httpx.Response(200, json={"id": "req-1", "output": [
            {"type": "reasoning", "summary": []}, {"type": "message", "content": [
                {"type": "output_text", "text": '{"answer":"ok"}'}]}],
            "usage": {"input_tokens": 10, "output_tokens": 5}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await InferenceClient("api", "secret", "evaluated-model", http=http).complete(
            "Return a JSON answer", {"untrusted": "source text"}, {"type": "object"})
    assert result.data == {"answer": "ok"}
    assert result.usage["input_tokens"] == 10
    assert captured[0]["store"] is False
    assert captured[0]["text"]["format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_litellm_explicit_openai_compatible_route():
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"id": "litellm-test", "choices": [{"message": {"content": '{"ok":true}'}}],
                                       "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await InferenceClient("litellm", "configured-secret", "user-model",
            base_url="http://127.0.0.1:4000/v1", http=http).complete("Return JSON", {}, {"type": "object"})
    assert result.data["ok"]
    assert str(captured[0].url) == "http://127.0.0.1:4000/v1/chat/completions"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal,success", [("response.completed", True), ("response.failed", False),
    ("response.incomplete", False), (None, False)])
async def test_official_chatgpt_plan_stream_requires_completed_terminal_event(terminal, success):
    captured = []
    events = ['data: ' + json.dumps({"type": "response.output_text.delta", "delta": '{"ok":true}'}) + '\n\n']
    if terminal:
        events.append('data: ' + json.dumps({"type": terminal, "response": {"usage": {}}}) + '\n\n')
    def handler(request):
        captured.append(request)
        return httpx.Response(200, text="".join(events), headers={"content-type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = InferenceClient("chatgpt_plan", "oauth-access-token", "account-model", http=http)
        if success:
            assert (await adapter.complete("Return JSON", {}, {"type": "object"})).data["ok"]
        else:
            with pytest.raises(InferenceError):
                await adapter.complete("Return JSON", {}, {"type": "object"})
    body = json.loads(captured[0].content)
    assert body["stream"] is True and body["store"] is False
    assert str(captured[0].url) == "https://api.openai.com/v1/responses"
    assert "max_output_tokens" not in body


@pytest.mark.asyncio
async def test_provider_refusal_does_not_become_empty_success():
    def handler(request):
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(InferenceError):
            await InferenceClient("api", "secret", "model", http=http).complete("JSON", {}, {})


@pytest.mark.asyncio
async def test_groq_strict_tiny_nonstreaming_json():
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"action": "NO_ACTION"})}}], "usage": {}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await GroqCoach("secret", http=http).suggest({"state_version": 3}, deadline_ms=1200)
    assert result["action"] == "NO_ACTION"
    assert captured[0]["stream"] is False
    assert captured[0]["response_format"]["json_schema"]["strict"] is True
    assert "tools" not in captured[0]
