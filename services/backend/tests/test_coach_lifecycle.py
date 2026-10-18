"""A25/A26/A49: admission, publication and costs span the whole helper turn."""

import asyncio
import threading
from datetime import timedelta

import pytest
from app.db import get, update
from test_live_memory import setup, turn


def holds(service, p):
    with service.db.transaction(False) as s:
        return [
            r.data for r in service.scoped(s, "usage_ledger", p) if r.data.get("work_kind") == "live_coach"
        ]


@pytest.mark.asyncio
async def test_rejected_local_admission_never_reserves_or_submits_paid_work(workspace, app, monkeypatch):
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "busy")
    service.coach.max_pending = 0
    result = await service.coach_turn(p, call["id"])
    assert result["action"] == "NO_ACTION"
    assert requests == [] and holds(service, p) == []
    with service.db.transaction(False) as s:
        # The pending chunk must remain explicitly unobserved, not consumed by admission.
        assert not service.transcript_metadata(s, p, call["id"])[0].data.get("interpretation_revision")


@pytest.mark.asyncio
async def test_cancelled_inference_settles_its_already_received_usage(workspace, app, monkeypatch):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, entered = app.state.service, asyncio.Event()
    turn(service, p, call, "billing")

    class Provider:
        last_usage, last_request_id = {}, None

        async def suggest(self, *args, **kwargs):
            self.last_usage, self.last_request_id = {"cost": 0.003}, "cancelled-receipt"
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(service, "configured_coach", lambda _: (Provider(), "openrouter"))
    task = asyncio.create_task(service.coach_turn(p, call["id"], urgent=True, question="Clarify?"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(holds(service, p)) == 1
    assert holds(service, p)[0]["status"] == "settled"
    assert holds(service, p)[0]["cost_usd"] == 0.003


@pytest.mark.asyncio
async def test_call_end_stops_inflight_helper_and_keeps_unknown_cost(workspace, app, monkeypatch):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, entered, cancelled = app.state.service, asyncio.Event(), asyncio.Event()
    turn(service, p, call, "ending")

    class Provider:
        last_usage, last_request_id = {}, None

        async def suggest(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    monkeypatch.setattr(service, "configured_coach", lambda _: (Provider(), "openrouter"))
    task = asyncio.create_task(service.coach_turn(p, call["id"], urgent=True, question="Clarify?"))
    await asyncio.wait_for(entered.wait(), 2)

    def finish():
        with service.db.transaction() as s:
            service.finish_call(s, p, get(s, "call_attempts", call["id"]))

    try:
        await asyncio.to_thread(finish)
        await asyncio.wait_for(cancelled.wait(), 0.5)
        assert (await task)["action"] == "NO_ACTION"
        entry = holds(service, p)[0]
        assert entry["status"] == "uncertain" and entry["reserved_usd"] == 0.01
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_newer_question_fences_an_old_publication_already_in_a_thread(workspace, app, monkeypatch):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, entered, release = app.state.service, threading.Event(), threading.Event()
    turn(service, p, call, "publication")
    publish = service._publish_help

    def delayed(*args, **kwargs):
        if args[2].get("private_question") == "First?":
            entered.set()
            assert release.wait(3)
        return publish(*args, **kwargs)

    monkeypatch.setattr(service, "_publish_help", delayed)
    old = asyncio.create_task(service.coach_turn(p, call["id"], urgent=True, question="First?"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        result = await service.coach_turn(p, call["id"], urgent=True, question="Second?")
        assert result["action"] == "SHOW_CARD"
        release.set()
        await asyncio.gather(old, return_exceptions=True)
        # to_thread cancellation cannot stop an already executing DB function.
        await asyncio.sleep(0.1)
        with service.db.transaction(False) as s:
            events = service.scoped(s, "coach_events", p)
            assert len(events) == 1 and events[0].id == result["id"]
    finally:
        release.set()
        await asyncio.gather(old, return_exceptions=True)


@pytest.mark.asyncio
async def test_automatic_wait_for_manual_help_is_not_billed_and_is_rescheduled(
    workspace, app, clock, monkeypatch
):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, entered, release, requests = app.state.service, asyncio.Event(), asyncio.Event(), []
    turn(service, p, call, "manual")
    configured = service.configured_coach

    def provider(p):
        target, route = configured(p)
        original = target.suggest

        async def delayed(request, **kwargs):
            requests.append(request)
            entered.set()
            await release.wait()
            return await original(request, **kwargs)

        target.suggest = delayed
        return target, route

    monkeypatch.setattr(service, "configured_coach", provider)
    manual = asyncio.create_task(service.coach_turn(p, call["id"], urgent=True, question="Clarify?"))
    await asyncio.wait_for(entered.wait(), 2)
    clock.now += timedelta(seconds=9)
    turn(service, p, call, "new-turn")
    try:
        result = await service.coach_turn(p, call["id"])
        assert result["action"] == "NO_ACTION" and len(requests) == 1
        assert len(holds(service, p)) == 1
        release.set()
        await manual
        clock.now += timedelta(seconds=9)
        await service.run_jobs(p["tenant_id"], queues=["live_coach"])
        assert len(requests) == 2
    finally:
        release.set()
        await asyncio.gather(manual, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_during_budget_storage_releases_proven_unsent_hold(workspace, app, monkeypatch):
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service, entered, release = app.state.service, threading.Event(), threading.Event()
    turn(service, p, call, "budget-storage")
    reserve = service.reserve_optional_budget

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return reserve(*args, **kwargs)

    monkeypatch.setattr(service, "reserve_optional_budget", slow)
    task = asyncio.create_task(service.coach_turn(p, call["id"], urgent=True))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_private_practice_uses_the_same_capacity_before_billing(workspace, app, monkeypatch):
    _, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    service.coach.max_pending = 0
    await service.practice(p, "How should I clarify a process constraint?")
    assert requests == []
    with service.db.transaction(False) as s:
        assert not [r for r in service.scoped(s, "usage_ledger", p) if r.data.get("work_kind") == "coaching_practice"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "transcription_disabled", "rep_changed"])
async def test_authority_changed_during_inference_cannot_publish_private_advice(workspace, app, monkeypatch, change):
    call, p, _, changes = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "authority")

    def revoke():
        with service.db.transaction() as s:
            if change == "disabled":
                update(get(s, "users", p["user_id"]), disabled=True)
            elif change == "rep_changed":
                update(get(s, "users", p["user_id"]), role="rep")
                update(get(s, "call_attempts", call["id"]), rep_id="another-rep")
            else:
                update(get(s, "call_attempts", call["id"]), transcription_disabled=True)

    changes["during"] = revoke
    assert (await service.coach_turn(p, call["id"], urgent=True, question="Clarify?"))["action"] == "NO_ACTION"
    with service.db.transaction(False) as s:
        assert not service.scoped(s, "coach_events", p)


@pytest.mark.asyncio
async def test_provider_breaker_keeps_current_cached_knowledge_without_more_charges(workspace, app, monkeypatch):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service, requests = app.state.service, []
    turn(service, p, call, "fallback", text="We have no budget for this.")

    class Provider:
        last_usage, last_request_id = {}, None

        async def suggest(self, *args, **kwargs):
            requests.append(True)
            raise TimeoutError()

    monkeypatch.setattr(service, "configured_coach", lambda _: (Provider(), "openrouter"))
    for _ in range(4):
        result = await service.coach_turn(p, call["id"], urgent=True)
        assert result["action"] == "SHOW_CARD" and result["kind"] == "cached_tip"
    assert len(requests) == 3 and len(holds(service, p)) == 3
        await asyncio.sleep(0.05)
        assert requests == []
        assert holds(service, p)[0]["status"] == "released"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
