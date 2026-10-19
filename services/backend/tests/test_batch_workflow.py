import asyncio
from datetime import timedelta

from app.providers.batch import OpenAIBatch


def test_deferred_scripts_require_metered_api_mode(workspace):
    c, h = workspace["client"], workspace["headers"]
    lead = c.get("/v1/workspace", headers=h).json()["leads"][0]
    c.put("/v1/admin/settings", headers=h, json={"inference_mode": "chatgpt_plan"})
    assert (
        c.post(
            "/v1/admin/batches/scripts",
            headers=h,
            json={"lead_ids": [lead["id"]], "idempotency_key": "plan-batch"},
        ).status_code
        == 409
    )


def test_deferred_batch_publishes_independent_validated_results_and_reports_missing(workspace, monkeypatch):
    c, h = workspace["client"], workspace["headers"]
    leads = c.get("/v1/workspace", headers=h).json()["leads"][:2]
    submitted = []

    async def submit(self, requests):
        submitted.extend(requests)
        return {"id": "batch-contract-1", "status": "validating"}

    async def retrieve(self, batch_id):
        value = {
            "lead_id": leads[0]["id"],
            "opener": "Hi, this is Alex Morgan from Example Seller. May I explain why I called?",
            "screening_intro": "Alex from Example Seller.",
            "discovery_questions": ["What would be useful?"],
            "fact_ids_used": [],
            "claims": [],
        }
        import json

        return {
            "id": batch_id,
            "status": "completed",
            "results": [
                {
                    "custom_id": next(
                        r["custom_id"] for r in submitted if r["custom_id"].startswith(leads[0]["id"])
                    ),
                    "response": {
                        "status_code": 200,
                        "body": {
                            "id": "response-a",
                            "output": [{"content": [{"type": "output_text", "text": json.dumps(value)}]}],
                            "usage": {"input_tokens": 100, "output_tokens": 40},
                        },
                    },
                }
            ],
        }

    monkeypatch.setattr(OpenAIBatch, "submit", submit)
    monkeypatch.setattr(OpenAIBatch, "retrieve", retrieve)
    from app.providers.inference import InferenceResult
    from test_grounding_review import review_response

    class Reviewer:
        async def complete(self, instructions, context, schema):
            return InferenceResult(
                review_response(context, verdict="NON_FACTUAL"), {}, "fixture-batch-review"
            )

    monkeypatch.setattr(c.app.state.service, "inference", lambda *args: Reviewer())
    c.put(
        "/v1/admin/settings", headers=h, json={"inference_mode": "api", "openai_api_key": "contract-only-key"}
    )
    r = c.post(
        "/v1/admin/batches/scripts",
        headers=h,
        json={
            "lead_ids": [lead["id"] for lead in leads],
            "idempotency_key": "deferred-one",
            "ready_at": (c.app.state.service.clock() + timedelta(days=2)).isoformat(),
        },
    )
    assert r.status_code == 200, r.text
    assert all(x["url"] == "/v1/responses" and x["body"]["store"] is False for x in submitted)
    result = c.post(f"/v1/admin/batches/{r.json()['id']}/reconcile", headers=h)
    assert result.status_code == 200, result.text
    assert result.json()["pending_review_lead_ids"] == [leads[0]["id"]]
    asyncio.run(c.app.state.service.run_jobs(queues=["scripts"]))
    result = c.post(f"/v1/admin/batches/{r.json()['id']}/reconcile", headers=h)
    assert result.json()["published"] == [leads[0]["id"]]
    assert result.json()["retry_lead_ids"] == [leads[1]["id"]]
    assert c.get(f"/v1/leads/{leads[0]['id']}/packet", headers=h).json()["script"]["fallback"] is False
