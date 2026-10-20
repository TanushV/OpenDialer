"""Calling work runs ahead of background work: writer admission and lane yielding."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest


def test_waiting_foreground_writer_is_admitted_before_an_earlier_background_writer():
    from app.priority import FOREGROUND, WriterAdmission

    admission = WriterAdmission()
    assert admission.acquire(timeout=1)
    order, ready = [], threading.Barrier(3)

    def writer(name, foreground):
        token = FOREGROUND.set(foreground)
        try:
            ready.wait()
            if name == "foreground":
                time.sleep(0.05)  # Arrives after the background writer is already waiting.
            assert admission.acquire(timeout=5)
            order.append(name)
            admission.release()
        finally:
            FOREGROUND.reset(token)

    threads = [
        threading.Thread(target=writer, args=("background", False)),
        threading.Thread(target=writer, args=("foreground", True)),
    ]
    for thread in threads:
        thread.start()
    ready.wait()
    time.sleep(0.15)
    admission.release()
    for thread in threads:
        thread.join(5)
    assert order == ["foreground", "background"], "Call-path writes must not queue behind background work"


def test_background_writer_is_not_starved_by_continuous_foreground_writers():
    from app.priority import FOREGROUND, WriterAdmission

    admission = WriterAdmission(background_aging_seconds=0.2)
    stop, acquired = threading.Event(), []

    def busy_foreground():
        token = FOREGROUND.set(True)
        try:
            while not stop.is_set():
                if admission.acquire(timeout=1):
                    time.sleep(0.01)
                    admission.release()
        finally:
            FOREGROUND.reset(token)

    workers = [threading.Thread(target=busy_foreground) for _ in range(3)]
    for worker in workers:
        worker.start()
    begun = time.monotonic()
    assert admission.acquire(timeout=5), "Background writes still progress"
    acquired.append(time.monotonic() - begun)
    admission.release()
    stop.set()
    for worker in workers:
        worker.join(5)
    assert acquired[0] < 2.0


def test_writer_admission_times_out_like_a_lock():
    from app.priority import WriterAdmission

    admission = WriterAdmission()
    assert admission.acquire(timeout=1)
    started = time.monotonic()
    assert admission.acquire(timeout=0.1) is False
    assert time.monotonic() - started < 1


@pytest.mark.asyncio
async def test_background_lanes_yield_to_foreground_work_but_never_indefinitely():
    from app.priority import ForegroundGate

    gate = ForegroundGate()
    with gate.work():
        started = time.monotonic()
        waited = await gate.wait_idle(max_wait=0.2)
        assert waited is False and 0.15 <= time.monotonic() - started < 1.0
    assert await gate.wait_idle(max_wait=1.0) is True
    assert gate.active == 0


@pytest.mark.asyncio
async def test_runtime_background_lane_does_not_start_jobs_during_foreground_work(monkeypatch):
    import app.priority as priority
    from app.config import Settings
    from app.runtime import AuthorityRuntime

    gate = priority.ForegroundGate()
    monkeypatch.setattr(priority, "GATE", gate)
    calls = []

    async def run_jobs(tenant_id=None, queues=None):
        calls.append((tuple(queues), gate.active))
        return {"processed": 0, "errors": [], "status": "passed"}

    async def maintenance_tick():
        return {"status": "passed", "errors": []}

    service = SimpleNamespace(
        settings=Settings(simulation=True),
        run_jobs=run_jobs,
        maintenance_tick=maintenance_tick,
        clock=time.time,
    )
    runtime = AuthorityRuntime(service, queues=["webhook", "ml"])
    with gate.work():
        await runtime.start()
        await asyncio.sleep(0.3)
        during = [c for c in calls if c[0] == ("ml",)]
    await asyncio.sleep(2.5)
    await runtime.close()
    assert not during, "Background lanes wait while a calling request is in flight"
    assert any(c[0] == ("ml",) for c in calls), "and resume afterwards"
    webhook_states = [active for queue, active in calls if queue == ("webhook",)]
    assert webhook_states and all(state >= 1 for state in webhook_states), "Live lanes run as foreground work"


def test_http_requests_run_as_foreground_work(workspace, app, monkeypatch):
    import app.priority as priority

    service, seen = app.state.service, []
    original = service.reserve

    def reserve(*args, **kwargs):
        seen.append((priority.GATE.active, priority.FOREGROUND.get()))
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "reserve", reserve)
    workspace["client"].post(
        "/v1/queue/next", headers=workspace["headers"], json={"campaign_id": workspace["campaign_id"]}
    )
    assert seen and seen[0][0] >= 1 and seen[0][1] is True
    assert priority.GATE.active == 0


def test_manual_job_drain_runs_background_work_as_background(workspace, app, monkeypatch):
    """The admin drain route is not calling work: its jobs neither claim nor hold foreground priority."""
    import app.priority as priority
    from test_control_completion import principal

    service, p, seen = app.state.service, principal(workspace), []

    async def prepare(p, lead_ids):
        seen.append((priority.GATE.active, priority.FOREGROUND.get()))

    monkeypatch.setattr(service, "prepare", prepare)
    service.jobs.enqueue(
        "scripts",
        {"seller_id": p["seller_id"], "user_id": p["user_id"], "lead_ids": ["manual"]},
        "manual-drain",
        tenant_id=p["tenant_id"],
    )
    response = workspace["client"].post("/v1/admin/jobs/run", headers=workspace["headers"])
    assert response.status_code == 200, response.text
    assert seen == [(0, False)]
