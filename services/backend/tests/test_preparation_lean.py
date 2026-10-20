"""W6: preparation ticks hold the writer briefly, retry only failures, and research stays bounded."""

import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from app.db import Job
from sqlalchemy import event, select
from test_preparation_completion import configure
from test_remediation_rep_workflow import p

IN_WINDOW = datetime(2026, 10, 15, 2, tzinfo=UTC)  # 19:00 in Los Angeles, inside 18:00-08:00


class WriterSpans:
    """Count write transactions, their wall time, and the statements each one runs."""

    def __init__(self, service, monkeypatch):
        self.spans, self.local = [], threading.local()
        original = service.db.transaction

        @contextmanager
        def tracked(write=True, *, session=None):
            if not write or session is not None:
                with original(write, session=session) as s:
                    yield s
                return
            span = {"statements": 0}
            self.local.span = span
            began = time.perf_counter()
            try:
                with original(write) as s:
                    yield s
            finally:
                span["seconds"] = time.perf_counter() - began
                self.local.span = None
                self.spans.append(span)

        def count(*args):
            span = getattr(self.local, "span", None)
            if span is not None:
                span["statements"] += 1

        monkeypatch.setattr(service.db, "transaction", tracked)
        event.listen(service.db.engine, "before_cursor_execute", count)
        self.engine, self.count = service.db.engine, count

    def reset(self):
        self.spans.clear()

    def close(self):
        event.remove(self.engine, "before_cursor_execute", self.count)


def seed(service, workspace, count):
    result = service.import_leads(
        p(workspace),
        {
            "campaign_id": workspace["campaign_id"],
            "rows": [
                {"name": f"Seeded {i}", "phone": f"+1415777{i:04d}", "timezone": "America/Los_Angeles"}
                for i in range(count)
            ],
        },
    )
    assert result["imported"] == count, result


@pytest.fixture
def seeded(workspace, app, clock, monkeypatch):
    configure(workspace)
    service = app.state.service
    seed(service, workspace, 300)
    spans = WriterSpans(service, monkeypatch)
    yield service, spans
    spans.close()


def test_idle_preparation_ticks_never_open_the_writer(seeded, workspace, clock):
    service, spans = seeded
    # Outside the configured window: the tick returns before any write transaction.
    assert service.prepare_ahead(p(workspace))["queued"] == 0
    assert spans.spans == []
    clock.now = IN_WINDOW
    first = service.prepare_ahead(p(workspace))
    assert first["queued"] == 2
    assert len(spans.spans) == 1, spans.spans
    # Claiming two candidates is a short write; it never rescans the 300-lead horizon.
    assert spans.spans[0]["statements"] <= 12, spans.spans
    spans.reset()
    # Satisfied (both queued): no paid work and no write transaction.
    assert service.prepare_ahead(p(workspace))["queued"] == 0
    assert spans.spans == []


def test_disabled_preparation_tick_never_opens_the_writer(seeded, workspace, clock):
    service, spans = seeded
    configure(workspace, preparation={"enabled": False})
    spans.reset()
    clock.now = IN_WINDOW
    assert service.prepare_ahead(p(workspace))["queued"] == 0
    assert spans.spans == []


def test_tick_rechecks_selected_candidates_in_its_short_write(seeded, workspace, clock, monkeypatch):
    """A candidate queued between the read and the claim is not queued twice."""
    service, spans = seeded
    clock.now = IN_WINDOW
    original = service.preparation_plan

    def racing(principal):
        plan = original(principal)
        # Another worker queues the first selected lead after this tick's read.
        service.queue_research(principal, [plan[0]["id"]])
        return plan

    monkeypatch.setattr(service, "preparation_plan", racing)
    spans.reset()
    assert service.prepare_ahead(p(workspace))["queued"] == 1
    with service.db.transaction(False) as s:
        jobs = list(s.scalars(select(Job).where(Job.queue == "research")))
        ids = [lead for job in jobs for lead in job.payload["lead_ids"]]
        assert len(ids) == len(set(ids)) == 2


def test_preparation_status_read_is_bounded_by_the_target_window(seeded, workspace, clock):
    service, spans = seeded
    status = service.preparation_status(p(workspace))
    assert spans.spans == []
    assert status["target"] == 2
    # Readiness inspects the prepared-ahead window, not every lead in the horizon.
    assert status["inspected_rows"] < 100, status
    assert status["omitted_rows"] > 0
    assert status["missing"] == 2


class FailingScripts:
    """A provider that is configured but always fails, counting paid script requests per lead."""

    def __init__(self):
        self.calls = {}

    async def complete(self, instructions, context, schema):
        lead_id = context.get("lead_id")
        self.calls[lead_id] = self.calls.get(lead_id, 0) + 1
        raise TimeoutError("synthetic provider outage")


def run_jobs_once(service, principal):
    """Run every queued research job the way the worker does, completing it on return."""
    import asyncio

    ran = 0
    while job := service.jobs.claim("research", "lean-worker", tenant_id=principal["tenant_id"]):
        try:
            asyncio.run(service.prepare(principal, job["payload"]["lead_ids"]))
        except Exception as exc:
            service.jobs.fail(job["id"], "lean-worker", type(exc).__name__, retry_seconds=0)
        else:
            service.jobs.complete(job["id"], "lean-worker")
        ran += 1
    return ran


@pytest.mark.asyncio
async def test_failing_provider_costs_a_bounded_number_of_paid_calls_per_lead(
    workspace, app, clock, monkeypatch
):
    from app.preparation.workflows import MAX_ATTEMPTS

    configure(workspace)
    service, principal = app.state.service, p(workspace)
    provider = FailingScripts()
    monkeypatch.setattr(service, "inference", lambda *args: provider)
    lead_id = workspace["lead_ids"][0]
    for _ in range(3 * MAX_ATTEMPTS):
        # Job reruns, explicit repeats and later ticks all share one per-lead attempt budget.
        result = await service.prepare(principal, [lead_id])
        assert set(result) >= {"prepared", "failed", "skipped"}
        clock.now += timedelta(minutes=31)
    assert provider.calls == {lead_id: MAX_ATTEMPTS}
    packet = service.packet(principal, lead_id)
    assert packet["script"]["fallback"] is True
    state = packet["preparation"]
    assert state["status"] == "failed" and state["attempts"] == MAX_ATTEMPTS
    assert state["next_attempt_at"] is None


@pytest.mark.asyncio
async def test_explicit_research_request_grants_one_new_bounded_budget(workspace, app, clock, monkeypatch):
    from app.preparation.workflows import MAX_ATTEMPTS

    configure(workspace)
    service, principal = app.state.service, p(workspace)
    provider = FailingScripts()
    monkeypatch.setattr(service, "inference", lambda *args: provider)
    lead_id = workspace["lead_ids"][0]
    for _ in range(MAX_ATTEMPTS + 1):
        await service.prepare(principal, [lead_id])
        clock.now += timedelta(minutes=31)
    assert provider.calls[lead_id] == MAX_ATTEMPTS
    service.queue_research(principal, [lead_id])
    for _ in range(MAX_ATTEMPTS + 1):
        await service.prepare(principal, [lead_id])
        clock.now += timedelta(minutes=31)
    assert provider.calls[lead_id] == 2 * MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_failed_batch_completes_with_per_lead_failures_instead_of_raising(
    workspace, app, clock, monkeypatch
):
    configure(workspace)
    service, principal = app.state.service, p(workspace)
    provider = FailingScripts()
    monkeypatch.setattr(service, "inference", lambda *args: provider)
    result = await service.prepare(principal, workspace["lead_ids"][:2])
    assert sorted(result["failed"]) == sorted(workspace["lead_ids"][:2])
    # An immediate rerun of the same job waits for backoff: no second paid request.
    again = await service.prepare(principal, workspace["lead_ids"][:2])
    assert sorted(again["skipped"]) == sorted(workspace["lead_ids"][:2])
    assert sum(provider.calls.values()) == 2
    with service.db.transaction(False) as s:
        for lead_id in workspace["lead_ids"][:2]:
            state = service.required(s, "leads", lead_id, principal).data["preparation"]
            assert state["status"] == "failed" and state["attempts"] == 1
            assert datetime.fromisoformat(state["next_attempt_at"]) == clock.now + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_pre_publish_exception_records_lead_attempt_state(workspace, app, monkeypatch):
    configure(workspace)
    service, principal = app.state.service, p(workspace)
    lead_id = workspace["lead_ids"][0]

    async def broken(*args):
        raise RuntimeError("synthetic research failure before publication")

    monkeypatch.setattr(service, "research_company", broken)
    result = await service.prepare(principal, [lead_id])
    assert result["failed"] == [lead_id]
    with service.db.transaction(False) as s:
        lead = service.required(s, "leads", lead_id, principal)
        assert not lead.data.get("preparation_lease")
        assert lead.data["preparation"]["status"] == "failed"
        assert lead.data["preparation"]["attempts"] == 1
        assert service.preparation_state(s, principal, lead)["status"] == "failed"
    assert service.packet(principal, lead_id)["script"]["fallback"] is True


def test_failing_ticks_retry_the_same_leads_instead_of_churning_new_ones(
    seeded, workspace, clock, monkeypatch
):
    from app.preparation.workflows import MAX_ATTEMPTS

    service, _ = seeded
    principal = p(workspace)
    provider = FailingScripts()
    monkeypatch.setattr(service, "inference", lambda *args: provider)
    clock.now = IN_WINDOW
    assert service.prepare_ahead(principal)["queued"] == 2
    first = set()
    for attempt in range(MAX_ATTEMPTS):
        run_jobs_once(service, principal)
        first |= set(provider.calls)
        # A failed lead in backoff occupies its prepared-ahead slot; nothing new is selected.
        assert service.prepare_ahead(principal)["queued"] == 0
        clock.now += timedelta(minutes=31)
        expected = 2 if attempt < MAX_ATTEMPTS - 1 else 0
        assert service.prepare_ahead(principal)["queued"] == expected
    run_jobs_once(service, principal)
    assert set(provider.calls) == first and len(first) == 2
    assert all(count == MAX_ATTEMPTS for count in provider.calls.values()), provider.calls
    status = service.preparation_status(principal)
    assert status["failed"] == 2 and status["retry_exhausted"] == 2


@pytest.mark.asyncio
async def test_unconfigured_research_takes_no_company_lease(workspace, app, monkeypatch):
    service, principal = app.state.service, p(workspace)
    monkeypatch.setattr(service.settings, "simulation", False)
    monkeypatch.setattr(service, "inference", lambda *args: None)
    lead = workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["leads"][0]
    await service.prepare(principal, [lead["id"]])
    with service.db.transaction(False) as s:
        assert not service.scoped(s, "companies", principal), "No provider means no research claim"
    assert service.packet(principal, lead["id"])["preparation"]["status"] == "fallback"


class Extractor:
    """Extracts every supplied source as one fact, with whole-field review approval."""

    def __init__(self, stability="stable"):
        self.extractions, self.stability = [], stability

    async def complete(self, instructions, context, schema):
        from app.providers.inference import InferenceResult
        from test_grounding_review import review_response

        if "fingerprint" in context:
            # Provider request IDs are unique receipts; a repeated ID is a conflicting charge.
            return InferenceResult(
                review_response(context), {"cost": 0.001}, f"lean-review-{len(self.extractions)}"
            )
        self.extractions.append(context)
        stability = self.stability
        return InferenceResult(
            {
                "facts": [
                    {
                        "statement": source["text"],
                        "quote": source["text"],
                        "source_id": source["id"],
                        "stability": stability(source) if callable(stability) else stability,
                        "conflicts": [],
                    }
                    for source in context["sources"]
                ],
                "unknowns": [],
            },
            {"cost": 0.001},
            f"lean-extract-{len(self.extractions)}",
        )


def company(service, principal, domain):
    with service.db.transaction(False) as s:
        row = service._by_key(s, principal, "companies", domain)
        return dict(row.data) if row else None


@pytest.mark.asyncio
async def test_research_page_fetches_never_use_the_shared_background_pool(workspace, app, monkeypatch):
    import httpx
    from app.research import fetch

    service, principal = app.state.service, p(workspace)
    monkeypatch.setattr(service.settings, "simulation", False)
    shared = []
    monkeypatch.setattr(
        service,
        "background_http",
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: shared.append(r) or httpx.Response(500))),
        raising=False,
    )
    monkeypatch.setattr(fetch, "validate_url", lambda url: ["93.184.216.34"])
    original, clients = httpx.AsyncClient, []

    def page(request):
        links = "<a href='/services'>Services</a><a href='/about'>About</a>"
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text=f"<p>Page {request.url.path}</p>{links}"
        )

    def factory(**kwargs):
        client = original(transport=httpx.MockTransport(page), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(fetch.httpx, "AsyncClient", factory)
    monkeypatch.setattr(service, "inference", lambda *args: Extractor())
    domain = "pooled.example.test"
    await service.research_company(principal, domain, {"company": "Pooled", "domain": domain})
    assert not shared, "Page fetches must not occupy the shared LLM/background connections"
    assert len(clients) == 3 and all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_company_fetch_deadline_bounds_a_slow_unit_and_records_lost_sources(
    workspace, app, monkeypatch
):
    import asyncio

    import app.intelligence as intelligence
    from app.research.fetch import SafeFetcher

    service, principal = app.state.service, p(workspace)
    monkeypatch.setattr(service.settings, "simulation", False)
    monkeypatch.setattr(intelligence, "COMPANY_FETCH_SECONDS", 0.3)
    domain = "slow.example.test"

    async def fetch(fetcher, url):
        if url == f"https://{domain}":
            return {
                "url": url,
                "title": "Slow",
                "text": "The company repairs commercial ovens.",
                "links": [{"url": f"https://{domain}/services", "text": "Services"}],
            }
        await asyncio.sleep(30)  # A drip that each per-fetch timer alone would keep renewing.

    monkeypatch.setattr(SafeFetcher, "fetch", fetch)
    extractor = Extractor()
    monkeypatch.setattr(service, "inference", lambda *args: extractor)
    await asyncio.wait_for(
        service.research_company(principal, domain, {"company": "Slow", "domain": domain}), 10
    )
    assert len(extractor.extractions) == 1 and len(extractor.extractions[0]["sources"]) == 1
    lost = company(service, principal, domain)["sources_lost"]
    assert lost == [{"url": f"https://{domain}/services", "error": "deadline"}]


@pytest.mark.asyncio
async def test_home_page_failure_is_recorded_not_cached_as_empty_research(workspace, app, monkeypatch):
    from app.research.fetch import SafeFetcher, UnsafeURL

    service, principal = app.state.service, p(workspace)
    monkeypatch.setattr(service.settings, "simulation", False)
    domain = "down.example.test"

    async def fetch(fetcher, url):
        raise UnsafeURL("synthetic unreachable origin")

    monkeypatch.setattr(SafeFetcher, "fetch", fetch)
    extractor = Extractor()
    monkeypatch.setattr(service, "inference", lambda *args: extractor)
    with pytest.raises(RuntimeError, match="sources"):
        await service.research_company(principal, domain, {"company": "Down", "domain": domain})
    state = company(service, principal, domain)
    assert state["sources_lost"] == [{"url": f"https://{domain}", "error": "UnsafeURL"}]
    assert not state.get("empty_research_key"), "Lost sources are a failed attempt, not an empty result"
    assert not extractor.extractions


@pytest.mark.asyncio
async def test_expired_time_sensitive_fact_refreshes_without_duplicating_stable_facts(
    workspace, app, clock, monkeypatch
):
    from app.research.fetch import SafeFetcher

    service, principal = app.state.service, p(workspace)
    monkeypatch.setattr(service.settings, "simulation", False)
    domain = "fresh.example.test"
    stable, offer = "The company repairs commercial roofs.", "Roof inspections are discounted this week."

    async def fetch(fetcher, url):
        if url == f"https://{domain}":
            return {
                "url": url,
                "title": "Fresh",
                "text": stable,
                "links": [{"url": f"https://{domain}/services", "text": "Services"}],
            }
        return {"url": url, "title": "Offer", "text": offer, "links": []}

    monkeypatch.setattr(SafeFetcher, "fetch", fetch)
    extractor = Extractor(lambda source: "time_sensitive" if source["text"] == offer else "stable")
    monkeypatch.setattr(service, "inference", lambda *args: extractor)
    lead = {"company": "Fresh", "domain": domain}
    await service.research_company(principal, domain, lead)
    await service.research_company(principal, domain, lead)
    assert len(extractor.extractions) == 1, "Current facts are reused"
    clock.now += timedelta(hours=25)
    # The stable fact is still current, but the time-sensitive one expired: refresh this company.
    await service.research_company(principal, domain, lead)
    assert len(extractor.extractions) == 2
    with service.db.transaction(False) as s:
        current = [
            f.data
            for f in service.scoped(s, "research_facts", principal)
            if f.data["domain"] == domain and datetime.fromisoformat(f.data["expires_at"]) > clock.now
        ]
    assert sorted(f["statement"] for f in current) == sorted([stable, offer])


def test_empty_template_slot_does_not_claim_its_fact_ids():
    from app.preparation.templates import fill
    from test_preparation_completion import TEMPLATE

    facts = [{"fact_id": "fact-1", "statement": "The company repairs roofs."}]
    candidate = {
        "lead_id": "lead-1",
        "slots": {
            "relevance": {"text": "", "fact_ids": ["fact-1"]},
            "proof": {"text": "", "fact_ids": []},
        },
    }
    filled = fill(candidate, TEMPLATE, lead={"id": "lead-1"}, seller="Seller", rep="Rep", facts=facts)
    assert filled["fact_ids_used"] == [] and filled["claims"] == []


@pytest.mark.asyncio
async def test_empty_slot_with_cited_fact_publishes_the_template_instead_of_failing(
    workspace, app, monkeypatch
):
    from app.providers.inference import InferenceResult

    configure(workspace)
    service, principal = app.state.service, p(workspace)
    lead_id = workspace["lead_ids"][0]

    class Provider:
        async def complete(self, instructions, context, schema):
            fact = context["facts"][0]["fact_id"]
            slots = {"relevance": {"text": "", "fact_ids": [fact]}, "proof": {"text": "", "fact_ids": []}}
            return InferenceResult({"lead_id": lead_id, "slots": slots}, {"cost": 0.001}, "empty-slot")

    monkeypatch.setattr(service, "inference", lambda *args: Provider())
    assert (await service.prepare(principal, [lead_id]))["prepared"] == [lead_id]
    packet = service.packet(principal, lead_id)
    assert packet["preparation"]["status"] == "ready" and packet["script"]["fact_ids_used"] == []
