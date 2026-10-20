"""Calling work runs ahead of background work inside one authority process.

Interactive HTTP requests and the live/webhook lanes are foreground work. While any is in
flight, background lanes (post-call, preparation, projections, learning) do not start new
jobs, and foreground writers are admitted to the single SQLite writer first. Both bounds are
finite, so background work slows under load but never stops.
"""

import asyncio
import contextvars
import threading
import time
from contextlib import contextmanager

FOREGROUND = contextvars.ContextVar("opendialer_foreground", default=False)
BACKGROUND_YIELD_SECONDS = 2.0


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

    async def wait_idle(self, max_wait=BACKGROUND_YIELD_SECONDS):
        """True once no foreground work is in flight; False when the yield ceiling elapsed."""
        deadline = time.monotonic() + max_wait
        while self.active:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.02)
        return True


GATE = ForegroundGate()


class WriterAdmission:
    """One SQLite writer at a time, foreground first.

    A background writer defers to waiting foreground writers until it has waited
    `background_aging_seconds`; after that, foreground writers wait for it instead, so
    background writes are delayed under load but never starved.
    """

    def __init__(self, background_aging_seconds=1.0):
        self._condition = threading.Condition()
        self._held = False
        self._waiting_foreground = 0
        self._aged_background = 0
        self.background_aging_seconds = background_aging_seconds

    def acquire(self, timeout=30):
        foreground = FOREGROUND.get()
        started = time.monotonic()
        deadline = started + timeout
        aged = False
        with self._condition:
            if foreground:
                self._waiting_foreground += 1
            try:
                while True:
                    now = time.monotonic()
                    if not foreground and not aged and now - started >= self.background_aging_seconds:
                        aged = True
                        self._aged_background += 1
                    if not self._held and (
                        (foreground and not self._aged_background)
                        or (not foreground and (aged or not self._waiting_foreground))
                    ):
                        self._held = True
                        return True
                    remaining = deadline - now
                    if remaining <= 0:
                        return False
                    wait = remaining
                    if not foreground and not aged:
                        wait = min(remaining, max(0.001, started + self.background_aging_seconds - now))
                    self._condition.wait(wait)
            finally:
                if foreground:
                    self._waiting_foreground -= 1
                if aged:
                    self._aged_background -= 1
                    self._condition.notify_all()

    def release(self):
        with self._condition:
            self._held = False
            self._condition.notify_all()
