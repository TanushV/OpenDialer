import json
from dataclasses import dataclass

import httpx


class InferenceError(RuntimeError):
    pass


@dataclass
class InferenceResult:
    data: dict
    usage: dict
    request_id: str | None = None


class InferenceClient:
    def __init__(self, mode, api_key, model, *, base_url=None, http=None, timeout=30):
        self.mode, self.api_key, self.model = mode, api_key, model
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        if mode == "chatgpt_plan" and self.base_url != "https://api.openai.com/v1":
            raise ValueError("ChatGPT plan inference requires the official public Responses endpoint")
        self.http, self.timeout = http, timeout

    async def complete(self, instructions, context, schema):
        untrusted = json.dumps(context, ensure_ascii=False)
        body = {"model": self.model}
        if self.mode == "litellm":
            path = "/chat/completions"
            body.update(
                messages=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": untrusted},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "result", "strict": True, "schema": schema},
                },
                stream=False,
            )
        else:
            path = "/responses"
            body.update(
                input=[{"role": "system", "content": instructions}, {"role": "user", "content": untrusted}],
                store=False,
            )
            if self.mode == "chatgpt_plan":
                body["stream"] = True
            else:
                body["text"] = {
                    "format": {"type": "json_schema", "name": "result", "strict": True, "schema": schema}
                }

        async def send(http):
            try:
                async with http.stream(
                    "POST",
                    self.base_url + path,
                    headers={"Authorization": "Bearer " + self.api_key},
                    json=body,
                    timeout=self.timeout,
                ) as r:
                    if r.status_code != 200:
                        await r.aread()
                        raise InferenceError(f"Inference HTTP {r.status_code}")
                    if self.mode == "chatgpt_plan":
                        output, completed, usage, request_id = "", False, {}, None
                        async for line in r.aiter_lines():
                            if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                                continue
                            event = json.loads(line[5:])
                            event_type = event.get("type")
                            if event_type == "response.output_text.delta":
                                output += event.get("delta", "")
                            if event_type in {"response.failed", "response.incomplete", "error"}:
                                raise InferenceError(f"Plan inference terminated: {event_type}")
                            if event_type == "response.completed":
                                completed = True
                                usage = event.get("response", {}).get("usage", {})
                                request_id = event.get("response", {}).get("id")
                        if not completed:
                            raise InferenceError("Stream interrupted before response.completed")
                        return InferenceResult(json.loads(output), usage, request_id)
                    payload = json.loads(await r.aread())
                    if self.mode == "litellm":
                        message = payload["choices"][0]["message"]
                        if message.get("refusal"):
                            raise InferenceError("Model refused")
                        output = message.get("content", "")
                        usage = payload.get("usage", {})
                        usage = {
                            **usage,
                            "input_tokens": usage.get("prompt_tokens", 0),
                            "output_tokens": usage.get("completion_tokens", 0),
                        }
                    else:
                        pieces = [c for item in payload.get("output", []) for c in item.get("content", [])]
                        if any(c.get("type") == "refusal" for c in pieces):
                            raise InferenceError("Model refused")
                        if payload.get("status") in {"failed", "incomplete"}:
                            raise InferenceError("Incomplete inference")
                        output = "".join(c.get("text", "") for c in pieces if c.get("type") == "output_text")
                        usage = payload.get("usage", {})
                    result = json.loads(output)
                    if not isinstance(result, dict):
                        raise InferenceError("Object output required")
                    return InferenceResult(result, usage, payload.get("id"))
            except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError) as e:
                raise InferenceError(f"Inference failed: {type(e).__name__}") from e

        if self.http:
            return await send(self.http)
        async with httpx.AsyncClient() as http:
            return await send(http)
