"""Deferred output needs the same meaning check, without repeated paid reviews."""

import asyncio
import json
from datetime import timedelta

import pytest
from app.providers.batch import OpenAIBatch
from app.providers.inference import InferenceResult
from test_grounding_review import review_response
from test_remediation_rep_workflow import p


@pytest.mark.parametrize("verdict", ["NON_FACTUAL", "UNSUPPORTED", "timeout", "changed_lead", "cancelled"])
@pytest.mark.asyncio
async def test_batch_review_is_complete_cached_and_budgeted(workspace, app, monkeypatch, clock, verdict):
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
            if verdict == "cancelled":
                raise asyncio.CancelledError()
            if verdict == "changed_lead":
                from app.db import update

                with service.db.transaction() as s:
                    update(service.required(s, "leads", lead["id"], p(workspace)), preparation_revision=100)
            return InferenceResult(
                review_response(context, verdict="NON_FACTUAL" if verdict == "changed_lead" else verdict),
                {"cost": 0.002},
                "batch-review",
            )

    monkeypatch.setattr(OpenAIBatch, "submit", submit)
    monkeypatch.setattr(OpenAIBatch, "retrieve", retrieve)
    monkeypatch.setattr(service, "inference", lambda *args: Reviewer())
    c.put("/v1/admin/settings", headers=h, json={"inference_mode": "api", "openai_api_key": "fixture-only"})
    intent = c.post(
        "/v1/admin/batches/scripts",
        headers=h,
        json={
            "lead_ids": [lead["id"]],
            "idempotency_key": "review-batch",
            "ready_at": (service.clock() + timedelta(days=2)).isoformat(),
        },
    ).json()
    for _ in range(2):
        response = c.post(f"/v1/admin/batches/{intent['id']}/reconcile", headers=h)
        assert response.status_code == 200, response.text
        assert response.json()["published"] == []
        assert response.json()["pending_review_lead_ids"] == [lead["id"]]
    assert not reviews, "Reconciliation queues reviews; it cannot charge or monopolize the request"
    if verdict == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await service.run_jobs(p(workspace)["tenant_id"], ["scripts"])
        clock.now += timedelta(minutes=2)
        service.jobs.recover_expired()
    await service.run_jobs(p(workspace)["tenant_id"], ["scripts"])
    response = c.post(f"/v1/admin/batches/{intent['id']}/reconcile", headers=h)
    assert response.json()["published"] == ([lead["id"]] if verdict == "NON_FACTUAL" else [])
    assert response.json()["pending_review_lead_ids"] == []
    await service.run_jobs(p(workspace)["tenant_id"], ["scripts"])
    assert len(reviews) == 1
    with service.db.transaction(False) as s:
        usage = [
            r.data
            for r in service.scoped(s, "usage_ledger", p(workspace))
            if r.data.get("work_kind") == "grounding_review"
        ]
    assert len(usage) == 1
    assert usage[0]["status"] == ("uncertain" if verdict in {"timeout", "cancelled"} else "settled")


@pytest.mark.parametrize("hours", [None, 23, 24])
def test_deferred_requires_adequate_explicit_deadline_before_spending(workspace, app, monkeypatch, hours):
    service, sent = app.state.service, []
    service.vault.write(p(workspace)["tenant_id"], {"inference_mode": "api", "openai_api_key": "fixture"})

    async def submit(provider, requests):
        sent.append(requests)
        return {"id": "deadline-batch", "status": "in_progress"}

    monkeypatch.setattr(OpenAIBatch, "submit", submit)
    lead = workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["leads"][0]
    body = {"lead_ids": [lead["id"]], "idempotency_key": "deadline"}
    if hours is not None:
        body["ready_at"] = (service.clock() + timedelta(hours=hours)).isoformat()
    response = workspace["client"].post("/v1/admin/batches/scripts", headers=workspace["headers"], json=body)
    assert response.status_code == (200 if hours == 24 else 409 if hours is None else 422), response.text
    assert len(sent) == (1 if hours == 24 else 0)
    if hours == 24:
        body["ready_at"] = (service.clock() + timedelta(hours=48)).isoformat()
        conflict = workspace["client"].post(
            "/v1/admin/batches/scripts", headers=workspace["headers"], json=body
        )
        assert conflict.status_code == 409 and len(sent) == 1
