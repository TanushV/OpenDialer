"""Advance template preparation acceptance, written before the implementation."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from app.db import Job, put, update
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


@pytest.mark.asyncio
async def test_bounded_parallel_preparation(workspace, app, monkeypatch):
    """C015: two simultaneous batches share two company slots, not one cap each."""
    service = app.state.service
    active, peak, finished = 0, 0, []

    async def unit(principal, identity, owner):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        if identity in failures:
            raise RuntimeError("Synthetic per-company failure")
        finished.append(identity)
        return True

    monkeypatch.setattr(service, "prepare_one", unit)
    with service.db.transaction() as s:
        original = service.scoped(s, "leads", p(workspace))[0]
        ids = [
            put(
                s,
                "leads",
                original.tenant_id,
                original.seller_id,
                {**original.data, "domain": f"company-{i}.example.test"},
                key=f"parallel-{i}",
            ).id
            for i in range(100)
        ]
    failures = {ids[1], ids[77]}
    with pytest.raises(RuntimeError, match="2 objects"):
        await service.prepare(p(workspace), ids)
    assert sorted(finished) == sorted(set(ids) - failures)
    assert peak == 2, "Preparation should overlap independent work, but cap the authority total at two"


@pytest.mark.asyncio
async def test_concurrent_preparation_jobs_share_authority_capacity(workspace, app, monkeypatch):
    service = app.state.service
    active, peak = 0, 0
    with service.db.transaction() as s:
        original = service.scoped(s, "leads", p(workspace))[0]
        ids = [
            put(
                s,
                "leads",
                original.tenant_id,
                original.seller_id,
                {**original.data, "domain": f"parallel-{i}.example.test"},
                key=f"shared-cap-{i}",
            ).id
            for i in range(12)
        ]

    async def prepare(principal, identity, owner):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.04)
            return True
        finally:
            active -= 1

    monkeypatch.setattr(service, "prepare_one", prepare)
    await asyncio.gather(*(service.prepare(p(workspace), ids[i : i + 4]) for i in range(0, 12, 4)))
    assert peak == 2, "Separate queued jobs must share the same authority limit"
    with service.db.transaction(False) as s:
        assert not any(
            service.required(s, "leads", i, p(workspace)).data.get("preparation_lease") for i in ids
        )


@pytest.mark.asyncio
async def test_running_preparation_renews_active_lease_and_cancellation_releases_it(
    workspace, app, clock, monkeypatch
):
    import app.preparation.workflows as workflows

    service = app.state.service
    lead_id = workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["leads"][0]["id"]
    entered, wait = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(workflows, "LEASE_HEARTBEAT_SECONDS", 0.01, raising=False)

    async def prepare(principal, identity, owner):
        entered.set()
        await wait.wait()
        return True

    monkeypatch.setattr(service, "prepare_one", prepare)
    task = asyncio.create_task(service.prepare(p(workspace), [lead_id]))
    await asyncio.wait_for(entered.wait(), 3)
    try:
        clock.now += timedelta(minutes=4)
        await asyncio.sleep(0.1)
        with service.db.transaction(False) as s:
            lease = service.required(s, "leads", lead_id, p(workspace)).data["preparation_lease"]
            assert datetime.fromisoformat(lease["expires_at"]) > clock() + timedelta(minutes=4)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    with service.db.transaction(False) as s:
        assert service.required(s, "leads", lead_id, p(workspace)).data.get("preparation_lease") is None


@pytest.mark.asyncio
async def test_lost_preparation_lease_is_failed_work_and_preserves_new_owner(workspace, app, monkeypatch):
    import app.preparation.workflows as workflows

    service = app.state.service
    lead_id = workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["leads"][0]["id"]
    monkeypatch.setattr(workflows, "LEASE_HEARTBEAT_SECONDS", 0.01, raising=False)

    async def prepare(principal, identity, owner):
        with service.db.transaction() as s:
            lead = service.required(s, "leads", identity, principal)
            update(
                lead,
                preparation_lease={
                    "owner": "replacement",
                    "expires_at": (service.clock() + timedelta(minutes=5)).isoformat(),
                },
            )
        await asyncio.sleep(0.15)
        return True

    monkeypatch.setattr(service, "prepare_one", prepare)
    with pytest.raises(RuntimeError, match="1 objects"):
        await service.prepare(p(workspace), [lead_id])
    with service.db.transaction(False) as s:
        assert (
            service.required(s, "leads", lead_id, p(workspace)).data["preparation_lease"]["owner"]
            == "replacement"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["withdraw", "statement", "delete"])
async def test_immediate_publication_rechecks_fact_after_inference(workspace, app, monkeypatch, change):
    configure(workspace)
    service = app.state.service
    lead_id = workspace["lead_ids"][0]
    # Produce ordinary first-party simulation facts before installing the racing provider.
    await service.prepare(p(workspace), [lead_id])
    packet = service.packet(p(workspace), lead_id)
    fact = packet["research"]["facts"][0]

    class Provider:
        async def complete(self, instructions, context, schema):
            assert any(f["fact_id"] == fact["fact_id"] for f in context["facts"])
            with service.db.transaction() as s:
                row = service.required(s, "research_facts", fact["id"], p(workspace))
                if change == "delete":
                    s.delete(row)
                else:
                    update(
                        row,
                        **(
                            {"status": "withdrawn"}
                            if change == "withdraw"
                            else {"statement": "Corrected company evidence."}
                        ),
                    )
            return InferenceResult(
                {
                    "lead_id": lead_id,
                    "slots": {
                        "relevance": {"text": fact["statement"], "fact_ids": [fact["fact_id"]]},
                        "proof": {"text": "", "fact_ids": []},
                    },
                },
                {"cost": 0.001},
                "stale-fact-result",
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    with pytest.raises(RuntimeError):
        await service.prepare(p(workspace), [lead_id])
    assert service.packet(p(workspace), lead_id)["script"]["fallback"] is True


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
async def test_preparation_cache_uses_selected_pointer_not_history_timestamp(workspace, app, monkeypatch):
    from app.db import get, put

    configure(workspace)
    service, lead_id = app.state.service, workspace["lead_ids"][0]
    requests = []

    class Provider:
        async def complete(self, prompt, context, schema):
            requests.append(context)
            return InferenceResult(
                {
                    "lead_id": lead_id,
                    "slots": {
                        "relevance": {"text": "", "fact_ids": []},
                        "proof": {"text": "", "fact_ids": []},
                    },
                },
                {"cost": 0.001},
                f"selected-cache-{len(requests)}",
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    await service.prepare(p(workspace), [lead_id])
    with service.db.transaction() as s:
        lead = get(s, "leads", lead_id)
        selected = get(s, "script_versions", lead.data["preparation"]["script_id"])
        put(
            s,
            "script_versions",
            lead.tenant_id,
            lead.seller_id,
            {**selected.data, "input_cache_key": "historical-unselected-input"},
            now=service.clock() + timedelta(seconds=1),
        )
    await service.prepare(p(workspace), [lead_id])
    assert len(requests) == 1, "An unselected historical script must not cause another paid request"


@pytest.mark.asyncio
async def test_returning_to_an_existing_template_repoints_the_prepared_packet(workspace, app):
    configure(workspace)
    service, lead_id = app.state.service, workspace["lead_ids"][0]
    await service.prepare(p(workspace), [lead_id])
    first = service.packet(p(workspace), lead_id)["script"]["script_version"]
    configure(workspace, script_template={**TEMPLATE, "closing_question": "Would another discussion help?"})
    await service.prepare(p(workspace), [lead_id])
    assert service.packet(p(workspace), lead_id)["script"]["script_version"] != first
    configure(workspace)
    await service.prepare(p(workspace), [lead_id])
    packet = service.packet(p(workspace), lead_id)
    assert packet["preparation"]["status"] == "fallback"
    assert packet["script"]["script_version"] == first


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


@pytest.mark.asyncio
async def test_published_script_reverts_when_supported_fact_content_changes(workspace, app, monkeypatch):
    configure(workspace)
    service = app.state.service
    lead_id = workspace["lead_ids"][0]

    class Provider:
        async def complete(self, instructions, context, schema):
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
                "published-source",
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    await service.prepare(p(workspace), [lead_id])
    packet = service.packet(p(workspace), lead_id)
    assert packet["script"]["fallback"] is False
    with service.db.transaction() as s:
        fact = service.required(s, "research_facts", packet["script"]["fact_ids_used"][0], p(workspace))
        update(fact, statement="The source was corrected.")
    packet = service.packet(p(workspace), lead_id)
    assert packet["script"]["fallback"] is True
    assert packet["preparation"]["status"] == "stale"
