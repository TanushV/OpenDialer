import json

import httpx
import pytest
from app.providers.inference import InferenceClient, InferenceError


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["api", "litellm", "chatgpt_plan"])
async def test_action_requires_named_tool_and_preserves_receipt(mode):
    async def respond(request):
        body = json.loads(request.content)
        assert body["parallel_tool_calls"] is False
        assert "tools" in body
        tool = {
            "type": "function_call",
            "name": "submit_research",
            "call_id": "action-1",
            "arguments": '{"facts": []}',
        }
        payload = {"id": "response-1", "status": "completed", "output": [tool]}
        if mode == "litellm":
            payload = {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "action-1",
                                    "type": "function",
                                    "function": {"name": tool["name"], "arguments": tool["arguments"]},
                                }
                            ]
                        },
                    }
                ]
            }
        if mode == "chatgpt_plan":
            return httpx.Response(
                200,
                text="data: " + json.dumps({"type": "response.completed", "response": payload}) + "\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await InferenceClient(
            mode, "test", "model", http=http, tool_name="submit_research"
        ).complete("Research", {}, {"type": "object"})
    assert result.data == {"facts": []}
    assert result.tool_call_id == "action-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        [],
        [{"type": "message", "content": [{"type": "output_text", "text": '{"facts":[]}'}]}],
        [{"type": "function_call", "name": "delete_data", "call_id": "x", "arguments": "{}"}],
    ],
)
async def test_free_text_and_unoffered_actions_cannot_mutate(output):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"status": "completed", "output": output})
        )
    ) as http:
        with pytest.raises(InferenceError):
            await InferenceClient("api", "test", "model", http=http, tool_name="submit_research").complete(
                "Research", {}, {}
            )


@pytest.mark.asyncio
async def test_postcall_uses_tools_while_live_helper_keeps_text_protocol():
    from app.providers.openrouter import OpenRouterCoach

    async def respond(request):
        body = json.loads(request.content)
        name = body["tool_choice"]["function"]["name"]
        assert name == "submit_postcall_analysis"
        assert "response_format" not in body
        return httpx.Response(
            200,
            json={
                "provider": "Cerebras",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "postcall-1",
                                    "type": "function",
                                    "function": {"name": name, "arguments": '{"value":1}'},
                                }
                            ]
                        },
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await OpenRouterCoach("test", http=http)._complete(
            {"mode": "finalize"}, "Analyze", {"type": "object"}, 10000
        )
    assert result == {"value": 1}


@pytest.mark.asyncio
async def test_postcall_rejects_prose_instead_of_executing_it():
    from app.providers.openrouter import OpenRouterCoach

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "provider": "Cerebras",
                    "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                },
            )
        )
    ) as http:
        with pytest.raises(InferenceError):
            await OpenRouterCoach("test", http=http)._complete({"mode": "finalize"}, "Analyze", {}, 10000)


def test_ai_saved_outcomes_do_not_become_observed_training_labels():
    from datetime import UTC, datetime

    from app.optimization.learning import outcome_at

    now = datetime(2026, 10, 24, tzinfo=UTC)
    row = {
        "outcome": "not_interested",
        "outcome_source": "ai_reviewed",
        "outcome_available_at": now.isoformat(),
    }
    assert outcome_at(row, now) is None
    assert outcome_at({**row, "outcome_source": "rep_reported"}, now)["outcome"] == "not_interested"
