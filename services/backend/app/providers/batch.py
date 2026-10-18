import json
import re
from contextlib import asynccontextmanager

import httpx


def join_batch_results(expected, rows):
    allowed, seen, completed = set(expected), set(), {}
    for row in rows:
        key = row.get("custom_id")
        if key not in allowed or key in seen:
            raise ValueError("Unknown or duplicated Batch custom_id")
        seen.add(key)
        response = row.get("response", {})
        if not row.get("error") and response.get("status_code", 200) == 200:
            completed[key] = response.get("body", {})
    return {"completed": completed, "retry": [k for k in expected if k not in completed]}


class OpenAIBatch:
    def __init__(self, api_key, *, http=None, metadata=None):
        self.headers = {"Authorization": "Bearer " + api_key}
        self.http = http
        self.metadata = metadata or {}

    @asynccontextmanager
    async def client(self):
        if self.http is not None:
            yield self.http
        else:
            async with httpx.AsyncClient(timeout=60) as http:
                yield http

    @staticmethod
    def reference(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
            raise ValueError("Invalid provider reference")
        return value

    async def lines(self, http, file_id):
        path = f"https://api.openai.com/v1/files/{self.reference(file_id)}/content"
        content = bytearray()
        async with http.stream("GET", path, headers=self.headers, timeout=60) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > 8 * 1024 * 1024:
                    raise ValueError("Batch content exceeds size limit")
                content.extend(chunk)
        rows = [json.loads(line) for line in content.splitlines() if line.strip()]
        if len(rows) > 500 or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Batch content exceeds object limit")
        return rows

    async def input_requests(self, file_id):
        async with self.client() as http:
            return await self.lines(http, file_id)

    async def submit(self, requests):
        if len({r["custom_id"] for r in requests}) != len(requests):
            raise ValueError("Batch custom_id must be unique")
        payload = "\n".join(json.dumps(r) for r in requests)
        if len(payload.encode()) > 8 * 1024 * 1024 or len(requests) > 500:
            raise ValueError("Batch request exceeds size limit")
        async with self.client() as http:
            upload = await http.post(
                "https://api.openai.com/v1/files",
                headers=self.headers,
                data={"purpose": "batch"},
                files={"file": ("requests.jsonl", payload, "application/jsonl")},
            )
            upload.raise_for_status()
            batch = await http.post(
                "https://api.openai.com/v1/batches",
                headers=self.headers,
                json={
                    "input_file_id": upload.json()["id"],
                    "endpoint": "/v1/responses",
                    "completion_window": "24h",
                    "metadata": self.metadata,
                },
            )
            batch.raise_for_status()
            return batch.json()

    async def retrieve(self, batch_id):
        async with self.client() as http:
            r = await http.get(
                f"https://api.openai.com/v1/batches/{self.reference(batch_id)}",
                headers=self.headers,
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("id") != batch_id:
                raise ValueError("Batch identity mismatch")
            if data.get("output_file_id"):
                data["results"] = await self.lines(http, data["output_file_id"])
            return data
