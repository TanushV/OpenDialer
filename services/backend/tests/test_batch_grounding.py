"""Deferred output needs the same meaning check, without repeated paid reviews."""

import json

import pytest
from app.providers.batch import OpenAIBatch
from app.providers.inference import InferenceResult
from test_grounding_review import review_response
from test_remediation_rep_workflow import p


@pytest.mark.parametrize("verdict", ["NON_FACTUAL", "UNSUPPORTED", "timeout"])
def test_batch_review_is_complete_cached_and_budgeted(workspace, app, monkeypatch, verdict):
    c, h = workspace["client"], workspace["headers"]
    service = app.state.service
    lead = c.get("/v1/workspace", headers=h).json()["leads"][0]
    requests, reviews = [], []

    async def submit(self, batch_requests):
        requests.extend(batch_requests)
        return {"id": "review-batch", "status": "validating"}

    async def retrieve(self, batch_id):
        context = json.loads(requests[0]["body"]["input"][1]["content"])
        candidate = {
            key: context["baseline"][key]
            for key in requests[0]["body"]["text"]["format"]["schema"]["properties"]
        }
        candidate["discovery_questions"] = ["Where would useful support make the greatest difference?"]
        return {
            "id": batch_id,
            "status": "completed",
            "results": [
                {
                    "custom_id": requests[0]["custom_id"],
                    "response": {
                        "status_code": 200,
                        "body": {
                            "id": "batch-result",
                            "output": [{"content": [{"type": "output_text", "text": json.dumps(candidate)}]}],
                            "usage": {"cost": 0.001},
                        },
                    },
                }
            ],
        }

    class Reviewer:
        async def complete(self, instructions, context, schema):
            reviews.append(context)
            if verdict == "timeout":
                raise TimeoutError("Unknown paid effect")
            return InferenceResult(review_response(context, verdict=verdict), {"cost": 0.002}, "batch-review")

    monkeypatch.setattr(OpenAIBatch, "submit", submit)
    monkeypatch.setattr(OpenAIBatch, "retrieve", retrieve)
    monkeypatch.setattr(service, "inference", lambda *args: Reviewer())
    c.put("/v1/admin/settings", headers=h, json={"inference_mode": "api", "openai_api_key": "fixture-only"})
    intent = c.post(
        "/v1/admin/batches/scripts",
        headers=h,
        json={"lead_ids": [lead["id"]], "idempotency_key": "review-batch"},
    ).json()
    for _ in range(2):
        response = c.post(f"/v1/admin/batches/{intent['id']}/reconcile", headers=h)
        assert response.status_code == 200, response.text
        assert response.json()["published"] == ([lead["id"]] if verdict == "NON_FACTUAL" else [])
    assert len(reviews) == 1
    with service.db.transaction(False) as s:
        usage = [
            r.data
            for r in service.scoped(s, "usage_ledger", p(workspace))
            if r.data.get("work_kind") == "grounding_review"
        ]
    assert len(usage) == 1
    assert usage[0]["status"] == ("uncertain" if verdict == "timeout" else "settled")
