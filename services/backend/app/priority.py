"""Calling work runs ahead of background work inside one authority process.

Interactive HTTP requests and the live/webhook lanes are foreground work. While any is in
flight, background lanes (post-call, preparation, projections, learning) do not start new
jobs, and foreground writers are admitted to the single SQLite writer first. Both bounds are
finite, so background work slows under load but never stops.
"""

import asyncio
import contextvars
import gc
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
YIELD_SECONDS = 0.0005
_WRITER_THREADS = set()  # Threads holding a SQLite writer admission.
_YIELD_STATE = threading.local()


def yield_to_calling_work():
    """Background CPU loops and bulk reads call this between small units of work.

    A CPU-bound background thread otherwise holds the interpreter lock for whole switch
    intervals (5 ms), and a thread making database calls waits for it again after each one.
    While another thread holds the SQLite writer (short windows), a background thread steps
    aside at every checkpoint so the write finishes quickly. While only calling work is in
    flight, which can be continuous, it pauses 0.5 ms after each 0.5 ms of its own work: the
    others wait at most that long and background work slows at most about twofold.
    Calling work and the writer's own thread never yield.
    """
    if FOREGROUND.get() or threading.get_ident() in _WRITER_THREADS:
        return
    if _WRITER_THREADS:
        time.sleep(YIELD_SECONDS)
        return
    if not GATE.active or time.perf_counter() < getattr(_YIELD_STATE, "resume_at", 0.0):
        return
    time.sleep(YIELD_SECONDS)
    _YIELD_STATE.resume_at = time.perf_counter() + YIELD_SECONDS


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
                        _WRITER_THREADS.add(threading.get_ident())
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
            _WRITER_THREADS.discard(threading.get_ident())
            self._condition.notify_all()


GC_YOUNG_THRESHOLD = 20_000


def settle_heap():
    """After startup, freeze the long-lived heap and collect young objects less often.

    Otherwise a background job that allocates heavily (a 10,000-lead forecast) triggers
    several full collections per run, each pausing every thread for tens of milliseconds.
    Reference counting still frees nearly everything at once; only cyclic garbage waits.
    """
    gc.unfreeze()  # Idempotent: a second call refreezes the current heap, not a second copy.
    gc.collect()
    gc.freeze()
    _, middle, oldest = gc.get_threshold()
    gc.set_threshold(GC_YOUNG_THRESHOLD, middle, oldest)
