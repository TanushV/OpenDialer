"""One bounded lane runner, shared by the bundled authority and background process."""

import asyncio
import logging


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
