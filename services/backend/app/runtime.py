"""One bounded lane runner, shared by the bundled authority and background process."""

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx

MAINTENANCE = frozenset({"maintenance", "call_control"})
# Fallback probes for work committed by another process (connected workers).
# In-process commits wake a lane at once; these are not polling intervals.
FALLBACK_SECONDS = {"webhook": 1.0, "live_coach": 1.0, "maintenance": 1.0, "call_control": 1.0}
BACKGROUND_FALLBACK_SECONDS = 2.0
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
                else:
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


class _NoProviderCookies(DefaultCookiePolicy):
    """Provider pools serve multiple accounts; only per-request credentials are allowed."""

    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


@asynccontextmanager
async def authority_lifetime(service, *, queues, enabled=True):
    """API and worker share one lifetime; slow background I/O cannot fill live pools."""
    runtime = AuthorityRuntime(service, queues=queues)
    try:
        async with AsyncExitStack() as stack:
            # Engineering admission bounds, not provider quotas or a capacity claim.
            for name, maximum in (("control_http", 16), ("coach_http", 12), ("background_http", 4)):
                client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        limits=httpx.Limits(max_connections=maximum, max_keepalive_connections=maximum),
                        timeout=httpx.Timeout(30, connect=5, pool=1),
                        cookies=CookieJar(policy=_NoProviderCookies()),
                        follow_redirects=False,
                    )
                )
                setattr(service, name, client)
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
