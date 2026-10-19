"""A25: latest help, manual priority and bounded admission under slow providers."""

import asyncio
from datetime import timedelta

import pytest
from app.coaching.engine import CoachEngine
from test_live_memory import setup, turn

FALLBACK = {"action": "NO_ACTION"}


@pytest.mark.asyncio
async def test_continuous_final_chunks_cannot_starve_earlier_pending_context(
    workspace, app, clock, monkeypatch
):
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "oldest")
    clock.now += timedelta(seconds=239.9)
    turn(service, p, call, "recent")
    clock.now += timedelta(milliseconds=200)
    turn(service, p, call, "newest")
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    assert len(requests) == 1
    assert "oldest" in {r["segment_id"] for r in requests[0]["recent_dialogue"]}


@pytest.mark.asyncio
async def test_unchanged_final_revision_does_not_rebill_interpretation(workspace, app, clock, monkeypatch):
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "unchanged")
    clock.now += timedelta(milliseconds=301)
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    assert len(requests) == 1
    turn(service, p, call, "unchanged", revision=2)
    clock.now += timedelta(seconds=10)
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    assert len(requests) == 1
    assert service.analysis(p, call["id"])["interpretation_coverage"]["interpreted_segments"] == 1


@pytest.mark.asyncio
async def test_rep_only_refresh_is_paced_but_not_lost(workspace, app, clock, monkeypatch):
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "seller-1", side="rep")
    clock.now += timedelta(milliseconds=301)
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    for i in range(3):
        clock.now += timedelta(seconds=1)
        turn(service, p, call, f"seller-{i + 2}", side="rep")
        clock.now += timedelta(milliseconds=301)
        await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    assert len(requests) == 1
    clock.now += timedelta(seconds=8)
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    assert len(requests) == 2
    assert requests[-1]["recent_dialogue"][-1]["segment_id"] == "seller-4"


def test_many_final_chunks_keep_only_one_latest_pending_context(workspace, app, monkeypatch):
    from app.db import Job
    from sqlalchemy import select

    call, p, _, _ = setup(workspace, app, monkeypatch)
    for i in range(40):
        turn(app.state.service, p, call, f"coalesced-{i}")
    with app.state.db.transaction(False) as session:
        jobs = list(session.scalars(select(Job).where(Job.queue == "live_coach", Job.status == "queued")))
        assert len(jobs) == 1
        assert jobs[0].payload["state_version"] == 40


@pytest.mark.asyncio
async def test_optional_review_holds_the_original_live_capacity_slot():
    engine, reviewing, finish = CoachEngine(), asyncio.Event(), asyncio.Event()
    engine.capacity = asyncio.Semaphore(1)
    submitted = []

    class Provider:
        async def suggest(self, request, **kwargs):
            submitted.append(request["id"])
            return {"action": "SHOW_CARD"}

    async def publish(result):
        reviewing.set()
        await finish.wait()
        return result

    task = asyncio.create_task(
        engine.suggest("first", {"id": "first"}, Provider(), FALLBACK, deadline_ms=1000, publish=publish)
    )
    try:
        await asyncio.wait_for(reviewing.wait(), 1)
        other = await engine.suggest("other", {"id": "other"}, Provider(), FALLBACK, deadline_ms=30)
        assert other.get("status") == "busy"
        assert submitted == ["first"]
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_prospect_debounce_and_rate_limit(workspace, app, clock, monkeypatch):
    """C002: a final prospect turn must not wait behind the rep-only 8-second pace."""
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "first")
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    assert not requests, "Coalesce final chunks for the declared 300 ms before submission"
    clock.now += timedelta(milliseconds=299)
    turn(service, p, call, "latest")
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    assert not requests
    clock.now += timedelta(milliseconds=301)
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    assert len(requests) == 1
    assert requests[-1]["recent_dialogue"][-1]["segment_id"] == "latest"
    clock.now += timedelta(seconds=1)
    turn(service, p, call, "next-question")
    clock.now += timedelta(milliseconds=301)
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    assert len(requests) == 2, "A distinct prospect question must not inherit an 8-second delay"
    # A duplicate final is not another opportunity and must not spend again.
    turn(service, p, call, "next-question")
    clock.now += timedelta(milliseconds=301)
    await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    assert len(requests) == 2
    for i in range(32):
        turn(service, p, call, f"fast-{i}")
        clock.now += timedelta(milliseconds=501)
        await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    assert len(requests) == 30
    coverage = service.analysis(p, call["id"])["interpretation_coverage"]
    with service.db.transaction(False) as session:
        memory = service._by_key(session, p, "conversation_states", call["id"])
        assert memory.data["rate_limited_opportunities"] >= 4
    assert coverage.get("rate_limited_opportunities", 0) >= 4


@pytest.mark.asyncio
async def test_provider_ignoring_cancel_cannot_publish_obsolete_same_version_advice():
    engine, entered = CoachEngine(), asyncio.Event()

    class Provider:
        async def suggest(self, request, **kwargs):
            if request["id"] == "old":
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pass  # An upstream completion raced cancellation.
            return {"action": "SHOW_CARD", "say_or_ask": request["id"], "state_version": 1}

    provider = Provider()
    old = asyncio.create_task(engine.suggest("call", {"id": "old"}, provider, FALLBACK))
    await entered.wait()
    new = await engine.suggest("call", {"id": "new"}, provider, FALLBACK)
    assert new["say_or_ask"] == "new"
    assert (await old)["action"] == "NO_ACTION", "Transcript version alone cannot fence two private questions"
    assert not engine.active


@pytest.mark.asyncio
async def test_automatic_turn_cannot_cancel_a_manual_answer_in_progress():
    engine, entered, release, requests = CoachEngine(), asyncio.Event(), asyncio.Event(), []

    class Provider:
        async def suggest(self, request, **kwargs):
            requests.append(request)
            entered.set()
            await release.wait()
            return {"action": "SHOW_CARD", "say_or_ask": "A contextual answer"}

    provider = Provider()
    manual = asyncio.create_task(
        engine.suggest("call", {"private_question": "Explain?", "manual_help": True}, provider, FALLBACK)
    )
    await entered.wait()
    try:
        automatic = await engine.suggest("call", {"state_version": 2}, provider, FALLBACK, deadline_ms=30)
        assert automatic["action"] == "NO_ACTION"
        assert len(requests) == 1, "Background interpretation displaced explicit rep help"
        release.set()
        assert (await manual)["action"] == "SHOW_CARD"
    finally:
        release.set()
        await asyncio.gather(manual, return_exceptions=True)


@pytest.mark.asyncio
async def test_local_capacity_wait_is_not_a_provider_failure_or_global_circuit_break():
    engine, entered, release = CoachEngine(), asyncio.Event(), asyncio.Event()
    engine.capacity = asyncio.Semaphore(1)

    class Provider:
        async def suggest(self, request, **kwargs):
            if request.get("hold"):
                entered.set()
                await release.wait()
            return {"action": "SHOW_CARD"}

    provider = Provider()
    active = asyncio.create_task(
        engine.suggest("active", {"hold": True}, provider, FALLBACK, deadline_ms=1000)
    )
    await entered.wait()
    try:
        for i in range(3):
            assert (await engine.suggest(f"waiting-{i}", {}, provider, FALLBACK, deadline_ms=15))[
                "action"
            ] == "NO_ACTION"
        assert engine.failures == 0 and engine.open_until == 0
        release.set()
        assert (await active)["action"] == "SHOW_CARD"
        assert (await engine.suggest("next", {}, provider, FALLBACK))["action"] == "SHOW_CARD"
    finally:
        release.set()
        await asyncio.gather(active, return_exceptions=True)


@pytest.mark.asyncio
async def test_admission_bounds_waiting_calls_instead_of_accumulating_a_backlog():
    engine, release = CoachEngine(), asyncio.Event()
    engine.capacity = asyncio.Semaphore(1)
    engine.max_pending = 4

    class Provider:
        async def suggest(self, *args, **kwargs):
            await release.wait()
            return FALLBACK

    tasks = [
        asyncio.create_task(engine.suggest(str(i), {}, Provider(), FALLBACK, deadline_ms=1000))
        for i in range(12)
    ]
    try:
        await asyncio.sleep(0.03)
        assert len(engine.active) <= 4, "Active and queued helper requests exceed the declared bound"
        assert sum(t.done() for t in tasks) >= 8
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_call_end_cancels_from_the_storage_thread_and_fences_late_provider_output():
    engine, entered = CoachEngine(), asyncio.Event()

    class Provider:
        async def suggest(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return {"action": "SHOW_CARD", "say_or_ask": "Too late"}

    task = asyncio.create_task(engine.suggest("call", {}, Provider(), FALLBACK))
    await entered.wait()
    try:
        assert await asyncio.to_thread(engine.cancel, "call")
        assert (await asyncio.wait_for(task, 1))["action"] == "NO_ACTION"
        assert not engine.active
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_timeout_ignoring_provider_cannot_return_late_advice():
    class Provider:
        async def suggest(self, *args, **kwargs):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                return {"action": "SHOW_CARD", "say_or_ask": "Beyond deadline"}

    assert (await CoachEngine().suggest("call", {}, Provider(), FALLBACK, deadline_ms=10))[
        "action"
    ] == "NO_ACTION"


@pytest.mark.asyncio
async def test_breaker_is_scoped_to_the_affected_seller_route():
    engine = CoachEngine()

    class Failing:
        async def suggest(self, *args, **kwargs):
            raise TimeoutError()

    class Healthy:
        async def suggest(self, *args, **kwargs):
            return {"action": "SHOW_CARD"}

    for _ in range(3):
        await engine.suggest("a", {}, Failing(), FALLBACK, circuit_key="seller-a")
    assert (await engine.suggest("b", {}, Healthy(), FALLBACK, circuit_key="seller-b"))[
        "action"
    ] == "SHOW_CARD"
    assert (await engine.suggest("a", {}, Healthy(), FALLBACK, circuit_key="seller-a"))[
        "action"
    ] == "NO_ACTION"


@pytest.mark.asyncio
async def test_cancel_resistant_work_still_counts_against_capacity():
    engine, entered, release, requests = CoachEngine(), asyncio.Event(), asyncio.Event(), []
    engine.max_pending = 1

    class Provider:
        async def suggest(self, request, **kwargs):
            requests.append(request)
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return FALLBACK

    old = asyncio.create_task(engine.suggest("call", {}, Provider(), FALLBACK))
    await entered.wait()
    try:
        # Supersession must not forget tasks whose provider has not stopped yet.
        for _ in range(4):
            await asyncio.wait_for(engine.suggest("call", {}, Provider(), FALLBACK), 0.2)
        assert len(requests) == 1
    finally:
        release.set()
        await asyncio.gather(old, return_exceptions=True)


@pytest.mark.asyncio
async def test_one_rep_cannot_fill_the_shared_queue_with_practice_requests():
    engine, entered, release = CoachEngine(), asyncio.Event(), asyncio.Event()

    class Provider:
        async def suggest(self, request, **kwargs):
            entered.set()
            if request.get("hold"):
                await release.wait()
            return {"action": "SHOW_CARD"}

    tasks = []
    try:
        for i in range(2):
            tasks.append(
                asyncio.create_task(
                    engine.suggest(
                        str(i),
                        {"hold": True},
                        Provider(),
                        FALLBACK,
                        owner_key="rep-a",
                    )
                )
            )
        await asyncio.wait_for(entered.wait(), 0.5)
        await asyncio.sleep(0)
        denied = await engine.suggest("practice-3", {}, Provider(), FALLBACK, owner_key="rep-a")
        assert denied["action"] == "NO_ACTION" and denied["status"] == "busy"
        allowed = await engine.suggest("other", {}, Provider(), FALLBACK, owner_key="rep-b")
        assert allowed["action"] == "SHOW_CARD"
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
