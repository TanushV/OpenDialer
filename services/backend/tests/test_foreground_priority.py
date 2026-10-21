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
    try:
        started = time.monotonic()
        assert admission.acquire(timeout=0.1) is False
        assert time.monotonic() - started < 1
    finally:
        admission.release()


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


def test_background_planning_yields_while_calling_work_is_in_flight(monkeypatch):
    """A CPU-bound planner thread made every database call of a request queue for the GIL."""
    import app.priority as priority
    from test_time_capacity import lead, number, window
    from test_unified_planning import plan

    gate = priority.ForegroundGate()
    monkeypatch.setattr(priority, "GATE", gate)
    pauses = []
    monkeypatch.setattr(priority.time, "sleep", lambda seconds: pauses.append(seconds))
    monkeypatch.setattr(priority, "_YIELD_STATE", threading.local())
    monkeypatch.setattr(priority, "_WRITER_THREADS", set())
    rows = [lead(f"l-{i}") for i in range(40)]
    plan(rows, [number()], staffed_windows=[window(minutes=120)])
    assert pauses == [], "Idle authorities plan at full speed"
    with gate.work():
        token = priority.FOREGROUND.set(False)  # The planner runs on a background thread.
        try:
            plan(rows, [number()], staffed_windows=[window(minutes=120)])
        finally:
            priority.FOREGROUND.reset(token)
    assert pauses, "The planner yields while calling work is in flight"
    pauses.clear()
    with gate.work():
        plan(rows, [number()], staffed_windows=[window(minutes=120)])
    assert pauses == [], "Calling work itself never yields"


def test_background_bulk_reads_yield_but_never_inside_a_write(workspace, app, monkeypatch):
    """Decrypting a page of rows yields to calling work; a held writer is never left sleeping."""
    import app.priority as priority
    from app.db import rows

    service, gate, pauses = app.state.service, priority.ForegroundGate(), []
    monkeypatch.setattr(priority, "GATE", gate)
    monkeypatch.setattr(priority.time, "sleep", lambda seconds: pauses.append(seconds))
    monkeypatch.setattr(priority, "_YIELD_STATE", threading.local())
    monkeypatch.setattr(priority, "_WRITER_THREADS", set())
    with gate.work():
        token = priority.FOREGROUND.set(False)
        try:
            with service.db.transaction(False) as s:
                read = len(rows(s, "leads"))
            assert read and pauses, "Background bulk reads yield while calling work is in flight"
            pauses.clear()
            with service.db.transaction() as s:
                assert rows(s, "leads")
            assert pauses == [], "A write transaction never sleeps while it holds the writer"
        finally:
            priority.FOREGROUND.reset(token)


def test_settled_heap_freezes_startup_objects_and_collects_young_objects_less_often():
    import gc

    from app.priority import GC_YOUNG_THRESHOLD, settle_heap

    before = gc.get_threshold()
    try:
        settle_heap()
        assert gc.get_freeze_count() > 0
        assert gc.get_threshold() == (GC_YOUNG_THRESHOLD, before[1], before[2])
    finally:
        gc.unfreeze()
        gc.set_threshold(*before)


def test_serving_authority_settles_its_heap_after_startup(monkeypatch, tmp_path):
    import sys

    import app.cli as cli
    import app.priority as priority

    calls = []
    monkeypatch.setattr(priority, "settle_heap", lambda: calls.append("settled"))
    monkeypatch.setitem(sys.modules, "uvicorn", type(sys)("uvicorn"))
    sys.modules["uvicorn"].run = lambda *args, **kwargs: calls.append("served")
    monkeypatch.setenv("OPENDIALER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENDIALER_SECRET_KEY", "synthetic-test-" + "b" * 48)
    monkeypatch.setenv("OPENDIALER_LOCAL_TOKEN", "synthetic-test-local-token")
    monkeypatch.setenv("OPENDIALER_DATABASE_URL", "sqlite:///" + str(tmp_path / "authority.db"))
    monkeypatch.setattr(sys, "argv", ["opendialer-authority", "serve", "--port", "0"])
    cli.main()
    assert calls == ["settled", "served"]


def test_background_cpu_work_also_steps_aside_while_another_thread_holds_the_writer(monkeypatch):
    """Every write holds the single SQLite writer; background CPU loops must not slow its body."""
    import threading

    import app.priority as priority

    monkeypatch.setattr(priority, "GATE", priority.ForegroundGate())
    pauses = []
    monkeypatch.setattr(priority.time, "sleep", lambda seconds: pauses.append(seconds))
    monkeypatch.setattr(priority, "_YIELD_STATE", threading.local())
    monkeypatch.setattr(priority, "_WRITER_THREADS", set())
    admission = priority.WriterAdmission()
    priority.yield_to_calling_work()
    assert pauses == [], "Nothing in flight: background work runs at full speed"
    held, done = threading.Event(), threading.Event()

    def writer():
        assert admission.acquire(timeout=1)
        held.set()
        done.wait(5)
        admission.release()

    thread = threading.Thread(target=writer)
    thread.start()
    assert held.wait(5)
    priority.yield_to_calling_work()
    assert pauses, "Another thread's write is in flight"
    done.set()
    thread.join(5)
    pauses.clear()
    priority.yield_to_calling_work()
    assert pauses == []
    assert admission.acquire(timeout=1)
    try:
        priority.yield_to_calling_work()
        assert pauses == [], "The writer's own thread never sleeps while holding the writer"
    finally:
        admission.release()


def test_a_read_nested_inside_a_write_never_yields_while_the_writer_is_held(workspace, app, monkeypatch):
    """A helper that opens its own read session inside a write still holds that write's locks."""
    import app.priority as priority
    from app.db import rows

    service, gate, pauses = app.state.service, priority.ForegroundGate(), []
    monkeypatch.setattr(priority, "GATE", gate)
    monkeypatch.setattr(priority.time, "sleep", lambda seconds: pauses.append(seconds))
    monkeypatch.setattr(priority, "_YIELD_STATE", threading.local())
    monkeypatch.setattr(priority, "_WRITER_THREADS", set())
    with gate.work():
        token = priority.FOREGROUND.set(False)
        try:
            with service.db.transaction() as outer:
                assert rows(outer, "leads")
                with service.db.transaction(False) as inner:
                    assert rows(inner, "leads")
            assert pauses == [], "Sleeping here would hold the writer (or PostgreSQL row locks)"
        finally:
            priority.FOREGROUND.reset(token)


def test_yielding_is_duty_cycled_so_background_work_slows_at_most_about_twofold(monkeypatch):
    """Continuous HTTP traffic (connected mode runs post-call work beside it) must not stall it."""
    import threading

    import app.priority as priority

    gate, clock, pauses = priority.ForegroundGate(), [0.0], []
    monkeypatch.setattr(priority, "GATE", gate)
    monkeypatch.setattr(priority, "_YIELD_STATE", threading.local())
    monkeypatch.setattr(priority, "_WRITER_THREADS", set())
    monkeypatch.setattr(priority.time, "perf_counter", lambda: clock[0])

    def sleep(seconds):
        pauses.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(priority.time, "sleep", sleep)
    with gate.work():
        token = priority.FOREGROUND.set(False)
        try:
            for _ in range(1000):  # 1000 checkpoints of 0.1 ms background work each.
                clock[0] += 0.0001
                priority.yield_to_calling_work()
        finally:
            priority.FOREGROUND.reset(token)
    worked, slept = 1000 * 0.0001, sum(pauses)
    assert pauses, "Calling work still gets the interpreter"
    assert slept <= worked * 1.05, f"{slept:.4f}s asleep for {worked:.4f}s of work"
