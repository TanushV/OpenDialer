import json

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
    def __init__(self, api_key):
        self.headers = {"Authorization": "Bearer " + api_key}

    async def submit(self, requests):
        if len({r["custom_id"] for r in requests}) != len(requests):
            raise ValueError("Batch custom_id must be unique")
        payload = "\n".join(json.dumps(r) for r in requests)
        async with httpx.AsyncClient(timeout=60) as http:
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
                },
            )
            batch.raise_for_status()
            return batch.json()

    async def retrieve(self, batch_id):
        async with httpx.AsyncClient(timeout=60) as http:
            r = await http.get(f"https://api.openai.com/v1/batches/{batch_id}", headers=self.headers)
            r.raise_for_status()
            data = r.json()
            if data.get("output_file_id"):
                result = await http.get(
                    f"https://api.openai.com/v1/files/{data['output_file_id']}/content", headers=self.headers
                )
                result.raise_for_status()
                data["results"] = [json.loads(line) for line in result.text.splitlines() if line.strip()]
            return data
