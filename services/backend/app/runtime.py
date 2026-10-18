"""One bounded lane runner, shared by the bundled authority and background process."""

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx


class AuthorityRuntime:
    def __init__(self, service, *, queues):
        self.service = service
        self.queues = tuple(dict.fromkeys(queues))
        self.stopping = asyncio.Event()
        self.tasks = []

    async def _lane(self, queue):
        while not self.stopping.is_set():
            try:
                if queue in {"maintenance", "call_control"}:
                    await self.service.maintenance_tick()
                else:
                    await self.service.run_jobs(queues=[queue])
            except Exception as exc:
                logging.getLogger(__name__).warning("%s deferred: %s", queue, type(exc).__name__)
            try:
                await asyncio.wait_for(
                    self.stopping.wait(), 0.1 if queue in {"webhook", "live_coach"} else 0.5
                )
            except TimeoutError:
                pass

    async def start(self):
        if self.tasks:
            raise RuntimeError("Runtime already started")
        self.tasks = [asyncio.create_task(self._lane(q), name="opendialer:" + q) for q in self.queues]

    async def close(self):
        self.stopping.set()
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
