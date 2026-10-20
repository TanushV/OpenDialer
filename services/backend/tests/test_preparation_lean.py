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
