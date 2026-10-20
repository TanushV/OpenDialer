"""Advance template preparation acceptance, written before the implementation."""

import asyncio
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.db import Job, put, update
from app.providers.inference import InferenceResult
from conftest import save_operating_policy, start
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
    from conftest import ThreadWork

    service = app.state.service
    active, peak, finished = 0, 0, []
    offloaded = ThreadWork(monkeypatch)

    async def unit(principal, identity, owner):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await offloaded.settle()  # Held until every admissible sibling has claimed and started.
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
    # Per-lead failures are recorded attempt state; the job itself completes.
    result = await service.prepare(p(workspace), ids)
    assert sorted(result["failed"]) == sorted(failures)
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

    from conftest import ThreadWork

    offloaded = ThreadWork(monkeypatch)

    async def prepare(principal, identity, owner):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await offloaded.settle()  # Held until every admissible sibling has claimed and started.
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
        try:
            # Ends when the heartbeat notices the replacement owner and cancels this unit,
            # instead of hoping a 0.15 s sleep outlasts the renewal thread on a loaded machine.
            await asyncio.wait_for(asyncio.Event().wait(), 60)
        except TimeoutError:
            pass  # An unnoticed lost lease lets the unit finish normally, failing the test.
        return True

    monkeypatch.setattr(service, "prepare_one", prepare)
    assert (await service.prepare(p(workspace), [lead_id]))["failed"] == [lead_id]
    with service.db.transaction(False) as s:
        lead = service.required(s, "leads", lead_id, p(workspace))
        assert lead.data["preparation_lease"]["owner"] == "replacement"
        # The former owner records no attempt over the replacement's work.
        assert (lead.data.get("preparation") or {}).get("status") != "failed"


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
    assert (await service.prepare(p(workspace), [lead_id]))["failed"] == [lead_id]
    assert service.packet(p(workspace), lead_id)["script"]["fallback"] is True


def configure(workspace, **changes):
    workspace["lead_ids"] = [
        lead["id"]
        for lead in workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["leads"]
    ]
    response = save_operating_policy(
        workspace["client"],
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
    response = save_operating_policy(
        workspace["client"],
        "/v1/admin/operating-policy",
        headers=workspace["headers"],
        json={"script_template": template},
    )
    assert response.status_code == 422


def test_nested_preparation_patch_preserves_schedule_and_template_version(workspace):
    original = configure(workspace)
    response = save_operating_policy(
        workspace["client"],
        "/v1/admin/operating-policy",
        headers=workspace["headers"],
        json={"preparation": {"ahead_count": 3}},
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
    assert (await service.prepare(p(workspace), [lead_id]))["failed"] == [lead_id]
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
async def test_partial_template_failures_do_not_regenerate_successes(workspace, app, monkeypatch, clock):
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
    assert (await service.prepare(p(workspace), lead_ids))["failed"] == [lead_ids[1]]
    # A failed lead is retried after its backoff, never by an immediate job rerun.
    assert (await service.prepare(p(workspace), lead_ids))["skipped"] == [lead_ids[1]]
    clock.now += timedelta(minutes=5)
    await service.prepare(p(workspace), lead_ids)
    assert attempts == {lead_ids[0]: 1, lead_ids[1]: 2}


@pytest.mark.asyncio
@pytest.mark.database
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
    from test_grounding_review import review_response

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
        update(fact, statement="We do not guarantee any revenue increase.")

    class Provider:
        async def complete(self, prompt, context, schema):
            if "fingerprint" in context:
                return InferenceResult(
                    review_response(context, verdict="CONTRADICTED"), {"cost": 0.001}, "rejected-slot-review"
                )
            fact = context["facts"][0]
            return InferenceResult(
                {
                    "lead_id": lead_id,
                    "slots": {
                        "relevance": {"text": "We guarantee 10x revenue.", "fact_ids": [fact["fact_id"]]},
                        "proof": {"text": "", "fact_ids": []},
                    },
                },
                {},
                "rejected-filled-slot",
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    assert (await service.prepare(p(workspace), [lead_id]))["failed"] == [lead_id]
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
    # A changed template supersedes work; it must not count as a provider failure.
    await service.prepare(p(workspace), [lead_id])
    assert service.packet(p(workspace), lead_id)["script"]["opener"] == "New purpose from Example Seller."
    with service.db.transaction(False) as s:
        pending = [
            j
            for j in s.scalars(select(Job))
            if j.queue == "scripts" and lead_id in j.payload.get("lead_ids", [])
        ]
        assert pending and all(j.status == "queued" and not j.attempts for j in pending)


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
    # Readiness reads the prepared-ahead window (target 2), not all 103 horizon leads.
    assert status["inspected_leads"] == status["target"] == 2
    assert status["inspected_rows"] <= 20 and status["omitted_rows"] == 103 - status["inspected_rows"]
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


@pytest.mark.asyncio
async def test_runway_detects_changed_used_fact_and_queues_only_affected_lead(workspace, app, monkeypatch):
    configure(workspace, preparation={"ahead_count": 3})
    service, principal = app.state.service, p(workspace)

    class Provider:
        async def complete(self, instructions, context, schema):
            fact = context["facts"][0]
            return InferenceResult(
                {
                    "lead_id": context["lead_id"],
                    "slots": {
                        "relevance": {"text": fact["statement"], "fact_ids": [fact["fact_id"]]},
                        "proof": {"text": "", "fact_ids": []},
                    },
                },
                {"cost": 0.001},
                "runway-" + context["lead_id"],
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    await service.prepare(principal, workspace["lead_ids"])
    lead_id = workspace["lead_ids"][0]
    packet = service.packet(principal, lead_id)
    with service.db.transaction() as s:
        fact = service.required(s, "research_facts", packet["script"]["fact_ids_used"][0], principal)
        update(fact, statement="Corrected company information.")
    status = service.preparation_status(principal)
    assert status["stale"] == 1 and status["ready"] == 2, status
    result = service.prepare_ahead(principal)
    assert result["queued"] == 1
    with service.db.transaction(False) as s:
        jobs = list(s.scalars(select(Job).where(Job.queue == "research", Job.status == "queued")))
        assert [identity for job in jobs for identity in job.payload["lead_ids"]] == [lead_id]
    assert service.prepare_ahead(principal)["queued"] == 0


def test_runway_target_uses_declared_staff_and_workspace_cap(workspace, app, clock):
    configure(workspace, preparation={"ahead_count": 20, "workspace_ahead_cap": 30})
    service, principal = app.state.service, p(workspace)
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], principal)
        update(
            campaign,
            optimization={
                "horizon_hours": 24,
                "handling_seconds": 120,
                "staffed_windows": [
                    {
                        "start_at": (clock.now + timedelta(hours=10)).isoformat(),
                        "end_at": (clock.now + timedelta(hours=18)).isoformat(),
                        "reps": 2,
                    }
                ],
            },
        )
    status = service.preparation_status(principal)
    assert status["target"] == 30 and status["available_reps"] == 2, status
    assert status["declared_rep_work_rate"] == 40  # 16 rep-hours / 24 wall-hours, seconds per minute
    assert status["runway_minutes"] == 0
    assert status["horizon_end"] == (clock.now + timedelta(hours=24)).isoformat()


@pytest.mark.asyncio
async def test_runway_time_and_no_staffing_are_explicit(workspace, app):
    configure(workspace, preparation={"ahead_count": 20})
    service, principal = app.state.service, p(workspace)
    await service.prepare(principal, workspace["lead_ids"])
    status = service.preparation_status(principal)
    assert status["runway_minutes"] == 18  # three 120-second attempts, eight hours / 24 declared
    assert "single_rep_default" in status["assumptions"]
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], principal)
        update(campaign, optimization={"rep_minutes": 0})
    status = service.preparation_status(principal)
    assert status["target"] == 0 and status["runway_minutes"] is None
    assert "no_declared_rep_work" in status["assumptions"]


def test_preparation_horizon_excludes_far_future_work_and_keeps_free_fallback(workspace, app, clock):
    configure(workspace, preparation={"ahead_count": 20})
    service, principal = app.state.service, p(workspace)
    with service.db.transaction() as s:
        lead = service.required(s, "leads", workspace["lead_ids"][0], principal)
        update(lead, next_attempt_at=(clock.now + timedelta(days=4)).isoformat())
        for budget in service.scoped(s, "budgets", principal):
            update(budget, daily_limit_usd=0, monthly_limit_usd=0)
    assert service.preparation_status(principal)["inspected_leads"] == 2
    assert service.prepare_ahead(principal)["queued"] == 2, "Approved generic work is free"


@pytest.mark.asyncio
async def test_template_retains_only_current_approved_final_voicemail_reference(workspace, app):
    service, principal = app.state.service, p(workspace)
    asset = "https://assets.example.test/approved-message.wav"
    response = save_operating_policy(
        workspace["client"],
        "/v1/admin/operating-policy",
        headers=workspace["headers"],
        json={"script_template": {**TEMPLATE, "final_voicemail_asset": asset}},
    )
    assert response.status_code == 422
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], principal)
        policy = service.required(s, "policy_versions", campaign.data["policy_id"], principal)
        update(policy, approved_assets=[asset], voicemail_enabled=True)
    configure(workspace, script_template={**TEMPLATE, "final_voicemail_asset": asset})
    lead_id = workspace["lead_ids"][0]
    await service.prepare(principal, [lead_id])
    assert service.packet(principal, lead_id)["script"]["final_voicemail_asset"] == asset
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], principal)
        policy = service.required(s, "policy_versions", campaign.data["policy_id"], principal)
        update(policy, approved_assets=[])
    assert service.packet(principal, lead_id)["script"]["final_voicemail_asset"] is None


@pytest.mark.asyncio
async def test_disabled_requester_cannot_publish_immediate_preparation(workspace, app, monkeypatch):
    configure(workspace)
    service, principal = app.state.service, p(workspace)

    class Provider:
        async def complete(self, instructions, context, schema):
            with service.db.transaction() as s:
                update(service.required(s, "users", principal["user_id"], principal), disabled=True)
            return InferenceResult(
                {
                    "lead_id": context["lead_id"],
                    "slots": {
                        "relevance": {"text": "", "fact_ids": []},
                        "proof": {"text": "", "fact_ids": []},
                    },
                },
                {"cost": 0.001},
                "revoked-preparation",
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    assert (await service.prepare(principal, workspace["lead_ids"][:1]))["failed"] == workspace["lead_ids"][
        :1
    ]
    with service.db.transaction(False) as s:
        assert not service.scoped(s, "script_versions", principal)


@pytest.mark.asyncio
async def test_hundred_lead_preparation_keeps_health_responsive(workspace, app, monkeypatch, record_property):
    configure(workspace)
    service, principal = app.state.service, p(workspace)
    with service.db.transaction() as s:
        original = service.scoped(s, "leads", principal)[0]
        ids = [
            put(
                s,
                "leads",
                original.tenant_id,
                original.seller_id,
                {**original.data, "domain": "throughput.example.test"},
                key=f"throughput-{i}",
            ).id
            for i in range(100)
        ]
    active, peak = 0, 0

    class Provider:
        async def complete(self, instructions, context, schema):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.03)
                return InferenceResult(
                    {
                        "lead_id": context["lead_id"],
                        "slots": {
                            "relevance": {"text": "", "fact_ids": []},
                            "proof": {"text": "", "fact_ids": []},
                        },
                    },
                    {"cost": 0},
                    "throughput-" + context["lead_id"],
                )
            finally:
                active -= 1

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    from conftest import LoopResponsiveness
    from sqlalchemy import event

    loop = LoopResponsiveness()

    def statement(*args):
        # Every preparation/health statement must leave the loop serving other requests.
        loop.wait_for_loop()

    started = time.monotonic()
    event.listen(service.db.engine, "before_cursor_execute", statement)
    try:
        task = asyncio.gather(*(service.prepare(principal, ids[i : i + 20]) for i in range(0, 100, 20)))
        delays = []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            while not task.done():
                before = time.monotonic()
                assert (await client.get("/health")).status_code == 200
                delays.append(time.monotonic() - before)
                await asyncio.sleep(0.03)
        await task
    finally:
        event.remove(service.db.engine, "before_cursor_execute", statement)
    elapsed = time.monotonic() - started
    p95 = sorted(delays)[int((len(delays) - 1) * 0.95)]
    # Wall-clock figures are retained as measurements, not as a load-sensitive pass/fail bound.
    record_property("preparation_100_seconds", elapsed)
    record_property("health_p95_seconds", p95)
    record_property("health_samples", len(delays))
    assert delays, "Health must be served while preparation is in flight"
    assert peak <= 2, peak
    loop.assert_responsive()
    with service.db.transaction(False) as s:
        assert all(
            service.required(s, "leads", identity, principal).data["preparation"]["status"] == "ready"
            for identity in ids
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["SUPPORTED", "CONTRADICTED"])
async def test_template_personalization_uses_review_not_exact_wording(workspace, app, monkeypatch, verdict):
    from test_grounding_review import review_response

    configure(workspace)
    service, lead_id = app.state.service, workspace["lead_ids"][0]
    requests = []
    novel = "This business is a fictional example used for testing."

    class Provider:
        async def complete(self, prompt, context, schema):
            requests.append(context)
            if "fingerprint" in context:
                assert {f["field"] for f in context["fields"]} == {"opener"}
                assert novel in context["fields"][0]["value"]
                return InferenceResult(
                    review_response(context, verdict=verdict), {"cost": 0.002}, "slot-review"
                )
            fact = context["facts"][0]
            return InferenceResult(
                {
                    "lead_id": lead_id,
                    "slots": {
                        "relevance": {"text": novel, "fact_ids": [fact["fact_id"]]},
                        "proof": {"text": "", "fact_ids": []},
                    },
                },
                {"cost": 0.001},
                "slot-generation",
            )

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    result = await service.prepare(p(workspace), [lead_id])
    assert result["failed"] == ([] if verdict == "SUPPORTED" else [lead_id])
    assert len(requests) == 2, "Meaning review must decide novel personalization, not string equality"
    packet = service.packet(p(workspace), lead_id)
    assert (novel in packet["script"]["opener"]) is (verdict == "SUPPORTED")
    assert packet["script"]["fallback"] is (verdict != "SUPPORTED")


def test_explicit_free_preparation_survives_exhausted_optional_budget(workspace, app):
    configure(workspace)
    with app.state.service.db.transaction() as s:
        for budget in app.state.service.scoped(s, "budgets", p(workspace)):
            update(budget, daily_limit_usd=0, monthly_limit_usd=0)
    result = workspace["client"].post(
        "/v1/research/batches", headers=workspace["headers"], json={"lead_ids": workspace["lead_ids"]}
    )
    assert result.status_code == 200, result.text
    assert result.json()["work_units"] > 0


@pytest.mark.parametrize(
    "instants",
    [
        ["2026-11-01T08:30:00+00:00", "2026-11-01T09:30:00+00:00"],
        ["2027-03-14T09:30:00+00:00", "2027-03-14T10:30:00+00:00"],
    ],
)
def test_preparation_dst_window_catchup_does_not_duplicate_jobs(workspace, app, clock, instants):
    configure(workspace, preparation={"ahead_count": 2, "off_hours_start": 0, "off_hours_end": 4})
    clock.now = datetime.fromisoformat(instants[0])
    assert app.state.service.prepare_ahead(p(workspace))["queued"] == 2
    clock.now = datetime.fromisoformat(instants[1])
    assert app.state.service.prepare_ahead(p(workspace))["queued"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_failed", [False, True])
async def test_changed_next_call_context_requeues_without_spending_failure_attempts(
    workspace, app, monkeypatch, provider_failed
):
    """A normal post-call revision supersedes work; an actual provider failure does not vanish."""
    service, principal = app.state.service, p(workspace)
    configure(workspace)
    lead_id = workspace["lead_ids"][0]
    await service.prepare(principal, [lead_id])
    with service.db.transaction() as s:
        lead = service.required(s, "leads", lead_id, principal)
        old_script = lead.data["preparation"]["script_id"]
        service.refresh_packet_in(s, principal, lead)
    job = service.jobs.claim("scripts", "revision-test", tenant_id=principal["tenant_id"])
    assert job and job["payload"]["kind"] == "packet_refresh"

    class FailedProvider:
        async def complete(self, *args):
            raise TimeoutError("Synthetic provider timeout during the context change")

    def change_after_input_snapshot(*args):
        with service.db.transaction() as s:
            lead = service.required(s, "leads", lead_id, principal)
            service.refresh_packet_in(s, principal, lead)
        return FailedProvider() if provider_failed else None

    monkeypatch.setattr(service, "inference", change_after_input_snapshot)
    # The provider failure is reported for this lead; it is not charged to the newer
    # revision's attempts, and the job completes rather than replaying the batch.
    result = await service.prepare(principal, [lead_id])
    assert result["failed" if provider_failed else "prepared"] == [lead_id]
    service.jobs.complete(job["id"], "revision-test")
    current = service.jobs.get(job["id"])
    assert current["status"] == "queued"
    assert current["attempts"] == 0 and not current["error"]
    with service.db.transaction(False) as s:
        lead = service.required(s, "leads", lead_id, principal)
        assert lead.data["preparation"]["script_id"] == old_script
        assert not lead.data.get("preparation_lease")
        assert service.preparation_state(s, principal, lead)["status"] == "stale"

    monkeypatch.setattr(service, "inference", lambda *args: None)
    retry = service.jobs.claim("scripts", "current-revision", tenant_id=principal["tenant_id"])
    assert retry["id"] == job["id"]
    await service.prepare(principal, [lead_id])
    service.jobs.complete(retry["id"], "current-revision")
    with service.db.transaction(False) as s:
        lead = service.required(s, "leads", lead_id, principal)
        assert lead.data["preparation"]["key"] == service.preparation_key(s, principal, lead)
        assert service.preparation_state(s, principal, lead)["status"] == "fallback"
    assert service.jobs.get(job["id"])["status"] == "completed"
