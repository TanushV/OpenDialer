"""One bounded lane runner, shared by the bundled authority and background process."""

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager

import httpx

from app import priority

MAINTENANCE = frozenset({"maintenance", "call_control"})
# Fallback probes for work committed by another process (connected workers).
# In-process commits wake a lane at once; these are not polling intervals.
FALLBACK_SECONDS = {"webhook": 1.0, "live_coach": 1.0, "maintenance": 1.0, "call_control": 1.0}
BACKGROUND_FALLBACK_SECONDS = 2.0
# Lanes that serve a live call run as foreground work; the rest yield to it (app.priority).
FOREGROUND_LANES = frozenset({"webhook", "live_coach"})
# Research and script work share one admission pool; a release in either can admit the other.
SHARED_POOLS = {"research": ("research", "scripts"), "scripts": ("research", "scripts")}


class AuthorityRuntime:
    def __init__(self, service, *, queues):
        self.service = service
        self.queues = tuple(dict.fromkeys(queues))
        self.stopping = asyncio.Event()
        self.tasks = []
        self.wakeups = {}
        self.loop = None
        self.listeners = None

    def _committed(self, queues):
        """Called from any committing thread; never blocks or raises."""
        lanes = {lane for queue in queues for lane in SHARED_POOLS.get(queue, (queue,))}
        for lane in lanes & self.wakeups.keys():
            try:
                self.loop.call_soon_threadsafe(self.wakeups[lane].set)
            except RuntimeError:
                return  # The loop already closed; its lanes are gone.

    async def _idle_seconds(self, queue):
        """Sleep until the fallback probe, or until known future work becomes due."""
        fallback = FALLBACK_SECONDS.get(queue, BACKGROUND_FALLBACK_SECONDS)
        jobs = getattr(self.service, "jobs", None)
        if queue in MAINTENANCE or jobs is None:
            return fallback
        try:
            due = await asyncio.to_thread(jobs.next_due, queue)
        except Exception:
            return fallback
        # Due-but-unclaimed work waits for capacity; its release commit wakes the lane.
        # The floor bounds passes if a simulated clock stands still before a due time.
        delay = (due - self.service.clock()).total_seconds() if due else fallback
        return min(fallback, max(delay, 0.02)) if delay > 0 else fallback

    async def _lane(self, queue):
        wakeup = self.wakeups[queue]
        while not self.stopping.is_set():
            # Clear before the pass: a commit during it schedules the next one.
            wakeup.clear()
            try:
                if queue in MAINTENANCE:
                    await self.service.maintenance_tick()
                elif queue in FOREGROUND_LANES:
                    with priority.GATE.work():
                        await self.service.run_jobs(queues=[queue])
                else:
                    # Background work does not start while a calling request or live job is in
                    # flight, but a bounded yield keeps it progressing under continuous calling.
                    await priority.GATE.wait_idle()
                    await self.service.run_jobs(queues=[queue])
            except Exception as exc:
                logging.getLogger(__name__).warning("%s deferred: %s", queue, type(exc).__name__)
            try:
                await asyncio.wait_for(wakeup.wait(), await self._idle_seconds(queue))
            except TimeoutError:
                pass

    async def start(self):
        if self.tasks:
            raise RuntimeError("Runtime already started")
        self.loop = asyncio.get_running_loop()
        self.wakeups = {q: asyncio.Event() for q in self.queues}
        self.listeners = getattr(getattr(self.service, "db", None), "job_listeners", None)
        if self.listeners is not None:
            self.listeners.append(self._committed)
        self.tasks = [asyncio.create_task(self._lane(q), name="opendialer:" + q) for q in self.queues]

    async def close(self):
        self.stopping.set()
        if self.listeners is not None and self._committed in self.listeners:
            self.listeners.remove(self._committed)
        for wakeup in self.wakeups.values():
            wakeup.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


@asynccontextmanager
async def authority_lifetime(service, *, queues, enabled=True):
    """API and worker share one lifetime; slow background I/O cannot fill live pools."""
    runtime = AuthorityRuntime(service, queues=queues)
    try:
        async with AsyncExitStack() as stack:
            # One shared pool; job lane capacities bound how much background work holds it.
            service.http = await stack.enter_async_context(
                httpx.AsyncClient(
                    limits=httpx.Limits(max_connections=32), timeout=httpx.Timeout(30, connect=5)
                )
            )
            try:
                if enabled:
                    await runtime.start()
                yield
            finally:
                # Finish/cancel workers before their owned transports are closed.
                await runtime.close()
    finally:
        service.raw.clear()
        service.db.engine.dispose()
