"""Small process-local raw-content cache. Persistent records contain only metadata."""

import copy
import json
import threading
import time
from collections import OrderedDict
from datetime import timedelta

RAW_TTL_SECONDS = 300


class EphemeralBuffer:
    def __init__(self, *, clock, ttl_seconds=RAW_TTL_SECONDS, max_bytes=4_000_000, max_entries=4096):
        if not 0 < ttl_seconds <= RAW_TTL_SECONDS or max_bytes < 1:
            raise ValueError("Raw content must expire within five minutes")
        self.clock, self.ttl, self.max_bytes = clock, ttl_seconds, max_bytes
        self.max_entries = max_entries
        self._items = OrderedDict()
        self._lock = threading.RLock()
        self.bytes_used = 0

    def _purge(self):
        now, mono = self.clock(), time.monotonic()
        for key, (deadline, monotonic_deadline, _, size) in list(self._items.items()):
            if deadline <= now or monotonic_deadline <= mono:
                del self._items[key]
                self.bytes_used -= size

    def put(self, key, value, *, expires_at=None):
        with self._lock:
            self._purge()
            deadline = min(
                expires_at or self.clock() + timedelta(seconds=self.ttl),
                self.clock() + timedelta(seconds=self.ttl),
            )
            old = self._items.get(key)
            mono = time.monotonic() + max(0, (deadline - self.clock()).total_seconds())
            if old:
                deadline, mono = min(deadline, old[0]), min(mono, old[1])
                self.bytes_used -= old[3]
            size = len(json.dumps(value, ensure_ascii=False).encode())
            # Retain a deadline tombstone for oversize revisions: retries never renew a lifetime.
            if size > self.max_bytes or deadline <= self.clock():
                value, size = None, 0
            self._items[key] = (deadline, mono, copy.deepcopy(value), size)
            self.bytes_used += size
            while self.bytes_used > self.max_bytes or len(self._items) > self.max_entries:
                _, removed = self._items.popitem(last=False)
                self.bytes_used -= removed[3]

    def get(self, key):
        with self._lock:
            self._purge()
            item = self._items.get(key)
            return copy.deepcopy(item[2]) if item else None

    def discard(self, key):
        with self._lock:
            item = self._items.pop(key, None)
            if item:
                self.bytes_used -= item[3]

    def purge(self):
        with self._lock:
            self._purge()

    def clear(self):
        with self._lock:
            self._items.clear()
            self.bytes_used = 0


def event_metadata(payload):
    """Allow operational event fields; remove carrier transcript/recording content recursively."""
    forbidden = {"transcript", "text", "recording_urls", "recording_url", "recording_download_url"}
    if isinstance(payload, dict):
        return {k: event_metadata(v) for k, v in payload.items() if k not in forbidden}
    if isinstance(payload, list):
        return [event_metadata(v) for v in payload]
    return payload
