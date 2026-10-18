"""Advance template preparation acceptance, written before the implementation."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from app.db import Job, update
from app.providers.inference import InferenceResult
from conftest import start
from sqlalchemy import event, select
from test_remediation_rep_workflow import p

TEMPLATE = {
    "opener": "Hello {name}, this is {rep} from {seller}. {relevance}",
    "gatekeeper": "Could you direct me to whoever handles {topic}?",
    "screening_intro": "{rep} from {seller}, calling about {topic}.",
    "discovery_questions": ["How are you handling {topic}?"],
    "topic": "customer service",
    "proof": "{proof}",
    "closing_question": "Would a further conversation be useful?",
}


def configure(workspace, **changes):
    workspace["lead_ids"] = [
        lead["id"]
        for lead in workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["leads"]
    ]
    response = workspace["client"].put(
        "/v1/admin/operating-policy",
        headers=workspace["headers"],
        json={
            "script_template": TEMPLATE,
            "preparation": {
                "ahead_count": 2,
                "batch_size": 2,
                "off_hours_start": 18,
                "off_hours_end": 8,
                "timezone": "America/Los_Angeles",
            },
            **changes,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "template",
    [
        {**TEMPLATE, "opener": "Hello {name.__class__}"},
        {**TEMPLATE, "opener": "Hello {secret}"},
        {**TEMPLATE, "screening_intro": "No identity supplied"},
        {**TEMPLATE, "discovery_questions": []},
    ],
)
def test_template_rejects_unsafe_or_incomplete_placeholders(workspace, template):
    response = workspace["client"].put(
        "/v1/admin/operating-policy", headers=workspace["headers"], json={"script_template": template}
    )
    assert response.status_code == 422


def test_nested_preparation_patch_preserves_schedule_and_template_version(workspace):
    original = configure(workspace)
    response = workspace["client"].put(
        "/v1/admin/operating-policy", headers=workspace["headers"], json={"preparation": {"ahead_count": 3}}
    )
    assert response.status_code == 200
    result = response.json()
    assert result["preparation"]["off_hours_start"] == 18
    assert result["preparation"]["ahead_count"] == 3
    assert result["template_version"] == original["template_version"]


def test_advance_preparation_is_bounded_offhours_and_not_replayed_on_wake(workspace, app, clock):
    configure(workspace)
    service = app.state.service
    assert service.prepare_ahead(p(workspace))["queued"] == 0
    clock.now = datetime(2026, 10, 15, 2, tzinfo=UTC)
    first = service.prepare_ahead(p(workspace))
    assert first["queued"] == 2
    assert service.prepare_ahead(p(workspace))["queued"] == 0
    clock.now += timedelta(days=2)
    assert service.prepare_ahead(p(workspace))["queued"] == 0
    with service.db.transaction(False) as s:
        jobs = list(s.scalars(select(Job).where(Job.queue == "research")))
        ids = [lead for job in jobs for lead in job.payload["lead_ids"]]
        assert len(ids) == len(set(ids)) == 2


@pytest.mark.asyncio
async def test_template_fallback_and_readiness_are_prepared_without_credentials(workspace, app):
    config = configure(workspace)
    service = app.state.service
    lead_id = workspace["lead_ids"][0]
    await service.prepare(p(workspace), [lead_id])
    packet = service.packet(p(workspace), lead_id)
    script = packet["script"]
    assert script["template_version"] == config["template_version"]
    assert script["opener"].startswith("Hello ")
    assert "Example Seller" in script["opener"]
    assert "customer service" in script["gatekeeper"]
    assert "customer service" in script["screening_intro"]
    assert script["closing_question"] == TEMPLATE["closing_question"]
    assert all(
        "{" not in text
        for text in [
            script["opener"],
            script["gatekeeper"],
            script["screening_intro"],
            *script["discovery_questions"],
            script["proof"],
            script["closing_question"],
        ]
    )
    assert packet["preparation"]["status"] == "fallback"
    status = workspace["client"].get("/v1/preparation", headers=workspace["headers"])
    assert status.status_code == 200
    assert status.json()["fallback"] == 1
    assert status.json()["prepared"] == 1
    assert status.json()["target"] == 2


@pytest.mark.asyncio
async def test_generated_slots_fill_template_without_replacing_the_seller_script(workspace, app, monkeypatch):
    configure(workspace)
    service, lead_id = app.state.service, workspace["lead_ids"][0]
    requests = []

    class Provider:
        async def complete(self, prompt, context, schema):
            requests.append(context)
            fact = context["facts"][0]
            return InferenceResult(
                {
                    "lead_id": lead_id,
                    "slots": {
                        "relevance": {"text": fact["statement"], "fact_ids": [fact["fact_id"]]},
                        "proof": {"text": "", "fact_ids": []},
                    },
                },
                {"input_tokens": 20, "output_tokens": 20},
                "template-1",
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    await service.prepare(p(workspace), [lead_id])
    packet = service.packet(p(workspace), lead_id)
    assert not packet["script"]["fallback"]
    assert packet["script"]["opener"].startswith("Hello ")
    assert packet["script"]["discovery_questions"] == ["How are you handling customer service?"]
    assert "This fictional company" in packet["script"]["opener"]
    await service.prepare(p(workspace), [lead_id])
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_unsupported_slot_is_rejected_even_without_a_claims_list(workspace, app, monkeypatch):
    configure(workspace)
    service, lead_id = app.state.service, workspace["lead_ids"][0]

    class Provider:
        async def complete(self, *args):
            return InferenceResult(
                {
                    "lead_id": lead_id,
                    "slots": {
                        "relevance": {"text": "Your advertising spend is $9000", "fact_ids": []},
                        "proof": {"text": "", "fact_ids": []},
                    },
                },
                {},
                "unsupported-template",
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    with pytest.raises(RuntimeError):
        await service.prepare(p(workspace), [lead_id])
    packet = service.packet(p(workspace), lead_id)
    assert packet["script"]["fallback"]
    assert "9000" not in str(packet["script"])
    assert packet["preparation"]["status"] == "failed"


@pytest.mark.asyncio
async def test_changed_template_invalidates_prepared_packet_but_not_active_script(workspace, app):
    configure(workspace)
    service = app.state.service
    await service.prepare(p(workspace), workspace["lead_ids"])
    call = start(workspace)
    before = service.packet(p(workspace), call["lead_id"])["script"]
    configure(workspace, script_template={**TEMPLATE, "opener": "A changed introduction from {seller}."})
    during = service.packet(p(workspace), call["lead_id"])["script"]
    assert during["script_version"] == before["script_version"]
    assert during["opener"] == before["opener"]
    workspace["client"].post("/v1/calls/" + call["id"] + "/hangup", headers=workspace["headers"])
    after = service.packet(p(workspace), call["lead_id"])
    assert after["script"]["opener"] == "A changed introduction from Example Seller."
    assert after["preparation"]["status"] == "stale"


@pytest.mark.asyncio
async def test_partial_template_failures_do_not_regenerate_successes(workspace, app, monkeypatch):
    configure(workspace)
    service = app.state.service
    attempts = {}
    lead_ids = workspace["lead_ids"][:2]

    class Provider:
        async def complete(self, prompt, context, schema):
            lead_id = context["lead_id"]
            attempts[lead_id] = attempts.get(lead_id, 0) + 1
            if lead_id == lead_ids[1] and attempts[lead_id] == 1:
                raise TimeoutError("synthetic transient failure")
            return InferenceResult(
                {
                    "lead_id": lead_id,
                    "slots": {
                        "relevance": {"text": "", "fact_ids": []},
                        "proof": {"text": "", "fact_ids": []},
                    },
                },
                {},
                lead_id + str(attempts[lead_id]),
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    with pytest.raises(RuntimeError):
        await service.prepare(p(workspace), lead_ids)
    await service.prepare(p(workspace), lead_ids)
    assert attempts == {lead_ids[0]: 1, lead_ids[1]: 2}


@pytest.mark.asyncio
async def test_concurrent_preparation_pays_for_one_generation(workspace, app, monkeypatch):
    configure(workspace)
    service, lead_id = app.state.service, workspace["lead_ids"][0]
    requests = []

    class Provider:
        async def complete(self, *args):
            requests.append(1)
            await asyncio.sleep(0.05)
            return InferenceResult(
                {
                    "lead_id": lead_id,
                    "slots": {
                        "relevance": {"text": "", "fact_ids": []},
                        "proof": {"text": "", "fact_ids": []},
                    },
                },
                {},
                "concurrent-" + str(len(requests)),
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    results = await asyncio.gather(
        service.prepare(p(workspace), [lead_id]),
        service.prepare(p(workspace), [lead_id]),
        return_exceptions=True,
    )
    assert not any(isinstance(value, Exception) for value in results), results
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_rejected_slots_never_leak_through_the_generic_fallback(workspace, app, monkeypatch):
    configure(workspace)
    service, lead_id = app.state.service, workspace["lead_ids"][0]
    await service.prepare(p(workspace), [lead_id])
    with service.db.transaction() as s:
        lead = service.required(s, "leads", lead_id, p(workspace))
        fact = next(
            f
            for f in service.scoped(s, "research_facts", p(workspace))
            if f.data["domain"] == lead.data["domain"]
        )
        update(fact, statement="We guarantee 10x revenue.")

    class Provider:
        async def complete(self, prompt, context, schema):
            fact = context["facts"][0]
            return InferenceResult(
                {
                    "lead_id": lead_id,
                    "slots": {
                        "relevance": {"text": fact["statement"], "fact_ids": [fact["fact_id"]]},
                        "proof": {"text": "", "fact_ids": []},
                    },
                },
                {},
                "rejected-filled-slot",
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    with pytest.raises(RuntimeError):
        await service.prepare(p(workspace), [lead_id])
    assert "guarantee" not in service.packet(p(workspace), lead_id)["script"]["opener"]


@pytest.mark.asyncio
async def test_template_change_during_generation_cannot_publish_old_work(workspace, app, monkeypatch):
    configure(workspace)
    service, lead_id = app.state.service, workspace["lead_ids"][0]

    class Provider:
        async def complete(self, *args):
            configure(workspace, script_template={**TEMPLATE, "opener": "New purpose from {seller}."})
            return InferenceResult(
                {
                    "lead_id": lead_id,
                    "slots": {
                        "relevance": {"text": "", "fact_ids": []},
                        "proof": {"text": "", "fact_ids": []},
                    },
                },
                {},
                "stale-template",
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    with pytest.raises(RuntimeError):
        await service.prepare(p(workspace), [lead_id])
    assert service.packet(p(workspace), lead_id)["script"]["opener"] == "New purpose from Example Seller."


def test_readiness_query_count_does_not_grow_per_candidate(workspace, app):
    configure(workspace)
    service = app.state.service
    service.import_leads(
        p(workspace),
        {
            "campaign_id": workspace["campaign_id"],
            "rows": [
                {"name": "Readiness fixture", "phone": f"+1415666{i:04d}", "timezone": "America/Los_Angeles"}
                for i in range(100)
            ],
        },
    )
    statements = []

    def count(*args):
        statements.append(1)

    event.listen(service.db.engine, "before_cursor_execute", count)
    try:
        status = service.preparation_status(p(workspace))
    finally:
        event.remove(service.db.engine, "before_cursor_execute", count)
    assert status["inspected_leads"] == 103
    assert len(statements) <= 20, len(statements)
