"""Calling work runs ahead of background work inside one authority process.

Interactive HTTP requests and the live/webhook lanes are foreground work. While any is in
flight, background lanes do not start new jobs (for at most a couple of seconds), and CPU-bound
background loops briefly yield. Background work slows under load but never stops.
"""

import asyncio
import contextvars
import gc
import threading
import time
from contextlib import contextmanager

FOREGROUND = contextvars.ContextVar("opendialer_foreground", default=False)


class ForegroundGate:
    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0

    @contextmanager
    def work(self):
        with self._lock:
            self.active += 1
        token = FOREGROUND.set(True)
        try:
            yield
        finally:
            FOREGROUND.reset(token)
            with self._lock:
                self.active -= 1

    async def wait_idle(self, max_wait=2.0):
        deadline = time.monotonic() + max_wait
        while self.active and time.monotonic() < deadline:
            await asyncio.sleep(0.02)


GATE = ForegroundGate()


def yield_to_calling_work():
    """Called between small units of background CPU work: step aside while calls are in flight."""
    if GATE.active and not FOREGROUND.get():
        time.sleep(0.0005)


def settle_heap():
    """After startup, freeze the long-lived heap and collect young objects less often."""
    gc.unfreeze()
    gc.collect()
    gc.freeze()
    _, middle, oldest = gc.get_threshold()
    gc.set_threshold(20_000, middle, oldest)
