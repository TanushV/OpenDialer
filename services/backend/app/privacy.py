"""Small process-local raw-content cache. Persistent records contain only metadata."""

import base64
import copy
import heapq
import itertools
import json
import math
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import NamedTuple

from app import ops

RAW_TTL_SECONDS = 300
# D7, R10.5: transcript text stays on this computer for 30 days, then is deleted; the rep can delete it sooner.
TRANSCRIPT_DAYS = 30


def usage_metadata(value):
    """Retain finite meter readings, never arbitrary provider response fields."""
    fields = {
        "cost",
        "cost_usd",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "audio_tokens",
        "cache_write_tokens",
        "cache_read_tokens",
        "web_search_calls",
        "accepted_prediction_tokens",
        "rejected_prediction_tokens",
    }
    details = {
        "input_tokens_details",
        "output_tokens_details",
        "prompt_tokens_details",
        "completion_tokens_details",
    }
    if not isinstance(value, dict):
        return {}
    return {
        k: usage_metadata(v) if k in details else v
        for k, v in value.items()
        if k in details
        and isinstance(v, dict)
        or k in fields
        and isinstance(v, (int, float))
        and not isinstance(v, bool)
        and math.isfinite(v)
        and v >= 0
    }


class _Entry(NamedTuple):
    deadline: datetime
    mono: float
    text: str | None  # Canonical JSON; None is a deadline tombstone.
    size: int
    scope: str | None
    revision: int
    seq: int


class EphemeralBuffer:
    """Raw content with a fixed lifetime from its original receipt.

    Expiry is ordered by deadline, so a purge touches only expired entries. Values are stored
    as JSON text: each read returns a fresh copy without a Python deep copy.
    """

    def __init__(
        self,
        *,
        clock,
        ttl_seconds=RAW_TTL_SECONDS,
        max_bytes=4_000_000,
        max_entries=4096,
        per_call_bytes=256_000,
        per_call_entries=256,
    ):
        if not 0 < ttl_seconds <= RAW_TTL_SECONDS or max_bytes < 1:
            raise ValueError("Raw content must expire within five minutes")
        self.clock, self.ttl, self.max_bytes = clock, ttl_seconds, max_bytes
        self.max_entries = max_entries
        self.per_call_bytes, self.per_call_entries = min(per_call_bytes, max_bytes), per_call_entries
        self._items = OrderedDict()  # Original receipt order; a revision keeps its place.
        self._expiry, self._mono_expiry = [], []  # (deadline, seq, key); stale seqs are skipped.
        self._scopes = {}  # scope -> {key: size}, in receipt order
        self._scope_bytes = {}
        self._revoked = OrderedDict()  # scope -> monotonic deadline; one TTL, so ordered.
        self._seq = itertools.count()
        self._lock = threading.RLock()
        self.bytes_used = 0

    def _remove(self, key):
        item = self._items.pop(key, None)
        if item is None:
            return
        self.bytes_used -= item.size
        if item.scope is not None:
            keys = self._scopes[item.scope]
            keys.pop(key, None)
            self._scope_bytes[item.scope] -= item.size
            if not keys:
                del self._scopes[item.scope], self._scope_bytes[item.scope]

    def _purge(self):
        now, mono = self.clock(), time.monotonic()
        while self._revoked and next(iter(self._revoked.values())) <= mono:
            self._revoked.popitem(last=False)
        for heap, limit in ((self._expiry, now), (self._mono_expiry, mono)):
            while heap and heap[0][0] <= limit:
                _, seq, key = heapq.heappop(heap)
                item = self._items.get(key)
                if item is not None and item.seq == seq:
                    self._remove(key)
        if len(self._expiry) > 2 * len(self._items) + 1024:
            # Revisions and evictions leave superseded heap records; rebuild occasionally.
            self._expiry = [(i.deadline, i.seq, k) for k, i in self._items.items()]
            self._mono_expiry = [(i.mono, i.seq, k) for k, i in self._items.items()]
            heapq.heapify(self._expiry)
            heapq.heapify(self._mono_expiry)

    def put(self, key, value, *, expires_at=None, scope=None, revision=None):
        with self._lock:
            self._purge()
            if scope in self._revoked:
                return
            now = self.clock()
            ceiling = now + timedelta(seconds=self.ttl)
            deadline = min(expires_at or ceiling, ceiling)
            old = self._items.get(key)
            if revision is not None and old and old.text is not None and old.revision > revision:
                return
            mono = time.monotonic() + max(0, (deadline - now).total_seconds())
            if old:
                # A revision never renews the original receipt's lifetime.
                deadline, mono = min(deadline, old.deadline), min(mono, old.mono)
                if old.scope != scope:
                    self._remove(key)
                else:
                    self.bytes_used -= old.size
                    if scope is not None:
                        self._scope_bytes[scope] -= old.size
            text = json.dumps(value, ensure_ascii=False)
            size = len(text.encode())
            # Retain a deadline tombstone for oversize revisions: retries never renew a lifetime.
            if size > (self.per_call_bytes if scope else self.max_bytes) or deadline <= now:
                text, size = None, 0
            seq = next(self._seq)
            stored = value.get("revision", 0) if isinstance(value, dict) else 0
            self._items[key] = _Entry(deadline, mono, text, size, scope, stored, seq)
            heapq.heappush(self._expiry, (deadline, seq, key))
            heapq.heappush(self._mono_expiry, (mono, seq, key))
            self.bytes_used += size
            if scope is not None:
                keys = self._scopes.setdefault(scope, {})
                keys[key] = size
                self._scope_bytes[scope] = self._scope_bytes.get(scope, 0) + size
                while scope in self._scopes and (
                    self._scope_bytes[scope] > self.per_call_bytes or len(keys) > self.per_call_entries
                ):
                    self._remove(next(iter(keys)))
            while self.bytes_used > self.max_bytes or len(self._items) > self.max_entries:
                self._remove(next(iter(self._items)))

    def get(self, key):
        with self._lock:
            self._purge()
            item = self._items.get(key)
            return json.loads(item.text) if item and item.text is not None else None

    def remaining_ms(self, key):
        """Time left on the earlier of the wall and monotonic deadlines; None once gone.

        A frozen or shifted business clock cannot restart a client's countdown."""
        with self._lock:
            self._purge()
            item = self._items.get(key)
            if item is None or item.text is None:
                return None
            wall = (item.deadline - self.clock()).total_seconds()
            return max(0, int(min(wall, item.mono - time.monotonic()) * 1000))

    def has(self, key):
        """Presence without copying the content."""
        with self._lock:
            self._purge()
            item = self._items.get(key)
            return bool(item and item.text is not None)

    def discard(self, key):
        with self._lock:
            self._remove(key)

    def purge(self):
        with self._lock:
            self._purge()

    def clear(self):
        with self._lock:
            self._items.clear()
            self._expiry.clear()
            self._mono_expiry.clear()
            self._scopes.clear()
            self._scope_bytes.clear()
            self._revoked.clear()
            self.bytes_used = 0

    def revoke_scope(self, scope):
        with self._lock:
            self._purge()
            self._revoked.pop(scope, None)
            self._revoked[scope] = time.monotonic() + self.ttl
            for key in list(self._scopes.get(scope, ())):
                self._remove(key)


def event_metadata(payload):
    """Only carrier correlation/control metadata crosses the durable boundary."""
    fields = {
        "call_control_id",
        "call_leg_id",
        "call_session_id",
        "connection_id",
        "command_id",
        "direction",
        "result",
        "from",
        "to",
        "recording_id",
        "digit",
        "digits",
        "occurred_at",
        "timing_source",
        "hangup_cause",
        "hangup_source",
        "sip_hangup_cause",
    }
    clean = {
        k: v
        for k, v in payload.items()
        if k in fields and isinstance(v, (str, int, float, bool)) and len(str(v)) <= 512
    }
    # Our own correlation envelope has IDs only. Opaque provider extensions never persist.
    context = payload.get("client_state")
    if isinstance(context, str) and len(context) <= 4096:
        try:
            parsed = json.loads(base64.b64decode(context, validate=True))
            parsed = {
                k: v
                for k, v in parsed.items()
                if k in {"call_id", "tenant_id", "agent_leg_id", "inbound_task", "attempt"}
                and isinstance(v, str)
                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", v)
            }
            if parsed:
                clean["client_state"] = base64.b64encode(json.dumps(parsed, sort_keys=True).encode()).decode()
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
            pass
    return clean


def bus_metadata(payload):
    fields = {
        "state",
        "lead_id",
        "segment_id",
        "transcript_version",
        "kind",
        "retracted",
        "permission_changed",
        "coach_event_id",
        "_seller_id",
        "_rep_id",
    }
    return {
        k: v for k, v in payload.items() if k in fields and (v is None or isinstance(v, (str, int, bool)))
    }


def job_metadata(payload):
    # Job bodies are typed internal work references. Non-speech preparation and numeric
    # training inputs have separate bounded contracts at their producer/consumer.
    fields = {
        "kind",
        "seller_id",
        "user_id",
        "role",
        "call_id",
        "lead_id",
        "lead_ids",
        "receipt_id",
        "campaign_id",
        "capacity",
        "list_name",
        "epoch",
        "rows",
        "source_dataset_id",
        "goal",
        "baseline",
        "window_days",
        "input_fingerprint",
        "internal",
        "definition",
        "namespace",
        "table",
        "policy_version",
        "cursor",
        "phase",
        "horizon",
        "desired_generation",
        "claimed_generation",
        "inspected",
        "submitted",
        "reserved_micro_usd",
        "intent_id",
        "custom_id",
        "result_fingerprint",
        "output",
        "source_kind",
        "source_id",
        "deleted",
        "target_namespace",
        "rebuild_lease",
        "job_id",
        "lease_owner",
        "cursor_generation",
        "completed_generation",
        "pending_deadline",
        "debounce",
        "superseded_by",
        "context_fingerprint",
        "state_version",
        "transcript_version",
        "request_id",
        "deadline",
        "raw_expires_at",
        "context_version",
        "packet_version",
    }
    return {k: v for k, v in payload.items() if k in fields}


def paraphrased(text, segments):
    """Reject copied source sentences/long spans in retained machine-derived text."""
    import re

    normalized = " ".join(re.findall(r"\w+", str(text).casefold()))
    for source in segments:
        words = re.findall(r"\w+", source.get("text", "").casefold())
        if len(words) >= 3 and " ".join(words) in normalized:
            return False
        if any(" ".join(words[i : i + 8]) in normalized for i in range(max(0, len(words) - 7))):
            return False
    return True


def script_section_of(service, s, p, call, segment):
    """Keep match metadata alongside the transcript revision that supports it."""
    if segment.get("side") != "rep" or not segment.get("is_final") or segment.get("system_audio"):
        return {"script_section": None, "section_score": None}
    script = call.data.get("script_snapshot")
    if not script:
        return {}
    from app.platform.db import MODELS
    from app.script_follow import match, sequence

    model = MODELS["transcript_segments"]
    rows = ops.context_rows(
        service,
        s,
        p,
        "transcript_segments",
        call.id,
        model.data["is_final"].as_boolean().is_(True),
        model.data["segment_id"].as_string() != segment["segment_id"],
        model.ctx_expires_at > service.clock(),
        limit=8,
    )
    earlier = sequence([row.data for row in rows] + [segment])
    earlier = earlier[: next(i for i, row in enumerate(earlier) if row is segment)]
    key, score = match(script, segment, earlier)
    return {"script_section": key, "section_score": score, "script_match_version": 1}


def persistent_document(kind, data):
    """The boundary that keeps raw speech and quotes out of every persisted document."""
    data = copy.deepcopy(data)
    if kind == "usage_ledger" and "usage" in data:
        data["usage"] = usage_metadata(data["usage"])
    if kind == "transcript_segments":
        return {
            k: v
            for k, v in data.items()
            if k
            in {
                "call_id",
                "segment_id",
                "revision",
                "text",
                "side",
                "speaker_id",
                "start_ms",
                "end_ms",
                "is_final",
                "confidence",
                "system_audio",
                "server_received_at",
                "raw_expires_at",
                "content_hash",
                "semantic_hash",
                "transport_revision",
                "effects_complete",
                "transcript_version",
                "question_count",
                "word_count",
                "script_section",
                "section_score",
                "script_match_version",
            }
        }
    if kind in {"provider_events", "webhook_inbox"}:
        fields = {
            "call_id",
            "event_id",
            "event_type",
            "hash",
            "payload",
            "signature_verified",
            "status",
            "received_at",
            "occurred_at",
            "raw_expires_at",
            "effects_complete",
            "processed_at",
            "lease_owner",
            "lease_until",
            "attempts",
            "ignored_reason",
            "error",
            "next_attempt_at",
        }
        return {k: event_metadata(v) if k == "payload" else v for k, v in data.items() if k in fields}
    if kind == "call_attempts":
        # Call data is application-owned; carrier bodies use the narrower envelope above.
        for key in ("recording_url", "recording_urls", "recording_download_url", "transcript", "text"):
            data.pop(key, None)
        return data
    return data


def raw_key(service, p, call_id, segment_id):
    return f"speech:{p['tenant_id']}:{p['seller_id']}:{call_id}:{segment_id}"


def speech_revoked(service, s, p, call):
    from app.platform.db import update

    # An opt-out during the call sets this flag (compliance/suppressions.revoke_effects).
    if call.data.get("transcription_disabled"):
        if not call.data.get("transcription_disabled"):
            update(
                call,
                transcription_disabled=True,
                transcript_version=call.data.get("transcript_version", 0) + 1,
            )
        service.raw.revoke_scope(p["tenant_id"] + ":" + call.id)
        ops.forget_transcript(service, s, p, call.id)
        return True
    return False


def forget_transcript(service, s, p, call_id):
    """Delete a call's saved transcript text (opt-out, the rep's Delete, or 30 days passing)."""
    from sqlalchemy import select

    from app.platform.db import MODELS

    model = MODELS["transcript_segments"]
    for row in s.scalars(select(model).where(model.tenant_id == p["tenant_id"], model.ctx_key == call_id)):
        if "text" in row.data:
            row.data = {k: v for k, v in row.data.items() if k != "text"}
    service.raw.revoke_scope(p["tenant_id"] + ":" + call_id)


def purge_transcripts(service):
    """R10.5: saved transcript text older than TRANSCRIPT_DAYS is deleted; the timing metadata stays."""
    from sqlalchemy import select

    from app.platform.db import MODELS

    model = MODELS["transcript_segments"]
    with service.db.transaction() as s:
        expired = s.scalars(
            select(model)
            .where(model.ctx_expires_at <= service.clock(), model.data["text"].as_string().is_not(None))
            .limit(5000)
        )
        for row in expired:
            if "text" in row.data:
                row.data = {k: v for k, v in row.data.items() if k != "text"}


def transcript_metadata(service, s, p, call_id):
    from sqlalchemy import select

    from app.platform.db import MODELS

    model = MODELS["transcript_segments"]
    return list(
        s.scalars(
            select(model).where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.external_key.startswith(call_id + ":"),
            )
        )
    )


def transcript_rows(service, s, p, call_id, *, metadata=None):
    if metadata is None:
        from app.platform.db import MODELS

        metadata = ops.context_rows(
            service,
            s,
            p,
            "transcript_segments",
            call_id,
            MODELS["transcript_segments"].ctx_expires_at > service.clock(),
            limit=service.raw.per_call_entries,
        )
    result = []
    for row in metadata:
        key = ops.raw_key(service, p, call_id, row.data["segment_id"])
        # The saved text (30 days) is the record; the in-memory copy only serves the live helper.
        item = service.raw.get(key) or (row.data if "text" in row.data else None)
        if item is not None:
            # The section the rep read from is worked out when the segment is saved, so it comes from the row.
            result.append(
                {
                    **item,
                    "script_section": row.data.get("script_section"),
                    "section_score": row.data.get("section_score"),
                    "transcript_version": row.data.get("transcript_version", 0),
                    "raw_remaining_ms": service.raw.remaining_ms(key) or 0,
                }
            )
    return sorted(result, key=lambda item: (item.get("server_received_at", ""), item["segment_id"]))


def raw_status(service, call, segments):
    if call.data.get("transcription_disabled"):
        return "permission_revoked"
    if segments:
        return "available_temporarily"
    from app.service import dt

    deadline = call.data.get("raw_expires_at")
    return (
        "unavailable_after_restart"
        if deadline and dt(deadline) > service.clock()
        else "expired"
        if deadline
        else "not_received"
    )


def _accept_transcript(service, p, call_id, segment, received_at, *, session=None, raw_writes):
    import hashlib

    from sqlalchemy import select

    from app.platform.db import MODELS, emit, put, update
    from app.service import dt, fail

    with service.db.transaction(session=session) as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        model = MODELS["transcript_segments"]
        row = s.scalar(
            select(model).where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.external_key == call_id + ":" + segment["segment_id"],
            )
        )
        content_hash = hashlib.sha256(json.dumps(segment, sort_keys=True).encode()).hexdigest()
        semantic_hash = hashlib.sha256(
            json.dumps({k: v for k, v in segment.items() if k != "revision"}, sort_keys=True).encode()
        ).hexdigest()
        if (
            row
            and segment["revision"] >= row.data["revision"]
            and row.data.get("semantic_hash") == semantic_hash
        ):
            # Transport-only revisions retain the canonical content revision and
            # its original TTL. They neither invalidate memory nor purchase interpretation.
            update(
                row,
                transport_revision=max(
                    segment["revision"], row.data.get("transport_revision", row.data["revision"])
                ),
            )
            phone = ops.required(service, s, "leads", call.data["lead_id"], p).data["phone"]
            return {
                "duplicate": True,
                "effects_complete": row.data.get("effects_complete", False),
                "transcript_version": call.data["transcript_version"],
                "expired": dt(row.data["raw_expires_at"]) <= service.clock(),
            }, phone
        if row and segment["revision"] <= row.data.get("transport_revision", row.data["revision"]):
            if segment["revision"] == row.data["revision"] and content_hash == row.data.get("content_hash"):
                phone = ops.required(service, s, "leads", call.data["lead_id"], p).data["phone"]
                return {
                    "duplicate": True,
                    "effects_complete": row.data.get("effects_complete", False),
                    "transcript_version": row.data.get("transcript_version", call.data["transcript_version"]),
                    "expired": dt(row.data["raw_expires_at"]) <= service.clock(),
                }, phone
            fail(409, "Transcript revision is obsolete")
        received_at = min(received_at or service.clock(), service.clock())
        received = dt(row.data["server_received_at"]) if row else received_at
        # The live helper reads a short-lived copy in memory; the saved text is kept for TRANSCRIPT_DAYS.
        expires = received + timedelta(seconds=RAW_TTL_SECONDS)
        kept_until = received + timedelta(days=TRANSCRIPT_DAYS)
        full = {
            **segment,
            "server_received_at": received.isoformat(),
            "raw_expires_at": kept_until.isoformat(),
        }
        metadata = persistent_document(
            "transcript_segments",
            {
                **full,
                "call_id": call_id,
                "content_hash": content_hash,
                "semantic_hash": semantic_hash,
                "transport_revision": segment["revision"],
                "effects_complete": False,
                "transcript_version": call.data.get("transcript_version", 0) + 1,
                "question_count": segment.get("text", "").count("?"),
                "word_count": len(segment.get("text", "").split()),
                **script_section_of(
                    service,
                    s,
                    p,
                    call,
                    {
                        **full,
                        "transcript_version": call.data.get("transcript_version", 0) + 1,
                    },
                ),
            },
        )
        if row:
            update(row, **metadata)
        else:
            put(
                s,
                "transcript_segments",
                p["tenant_id"],
                p["seller_id"],
                metadata,
                key=call_id + ":" + segment["segment_id"],
                now=service.clock(),
            )
        raw_writes.append(
            (
                ops.raw_key(service, p, call_id, segment["segment_id"]),
                full,
                expires,
                p["tenant_id"] + ":" + call_id,
            )
        )
        update(
            call,
            transcript_version=call.data.get("transcript_version", 0) + 1,
            raw_expires_at=max(call.data.get("raw_expires_at", ""), kept_until.isoformat()),
        )
        version = call.data["transcript_version"]
        emit(
            s,
            p["tenant_id"],
            "transcript.updated",
            call,
            service.clock(),
            {"segment_id": segment["segment_id"], "transcript_version": version},
        )
        phone = ops.required(service, s, "leads", call.data["lead_id"], p).data["phone"]
        return {
            "segment_id": segment["segment_id"],
            "transcript_version": version,
            "expired": expires <= service.clock(),
        }, phone


def _transcript_in(service, s, p, call_id, segment, received_at, raw_writes, *, queue_helper=True):
    import re

    from sqlalchemy import select

    from app.platform.db import MODELS, update

    service.db.authorize_stream(s, p)
    call = ops.required(service, s, "call_attempts", call_id, p)
    s.refresh(call, with_for_update=True)
    if ops.speech_revoked(service, s, p, call):
        return {"unavailable": "permission_revoked", "expired": True}, False

    result, phone = ops._accept_transcript(
        service, p, call_id, segment, received_at, session=s, raw_writes=raw_writes
    )
    if result.get("effects_complete"):
        return result, False
    if segment.get("side") == "remote" and re.search(
        r"\b(do not call|don't call|stop calling|remove (?:me|my number)|not call (?:me|again))\b",
        segment.get("text", ""),
        re.I,
    ):
        ops.suppress(
            service,
            p,
            phone,
            "opt_out" if segment["is_final"] else "temporary_hold",
            event_id=f"transcript-optout:{call_id}:{segment['segment_id']}:{segment['revision']}",
            session=s,
        )
    eligible = bool(segment.get("is_final") and not result["expired"])
    if eligible:
        from app.calling.state import TERMINAL

        if call.data["state"] in TERMINAL | {"ENDING"}:
            ops.enqueue_aftercall(service, s, p, call)
        elif (
            queue_helper
            and segment.get("side") == "remote"
            and not segment.get("system_audio")
            and not result.get("duplicate")
            and segment.get("text", "").strip()
        ):
            from app import live_help

            live_help.queue(service, s, p, call, result["transcript_version"])
    model = MODELS["transcript_segments"]
    row = s.scalar(
        select(model).where(
            model.tenant_id == p["tenant_id"],
            model.seller_id == p["seller_id"],
            model.external_key == call_id + ":" + segment["segment_id"],
        )
    )
    if row and (row.data["revision"] == segment["revision"] or result.get("duplicate")):
        update(row, effects_complete=True)
    return result, eligible


def _publish_transcripts(service, writes):
    # Publish only committed revisions. A later committing worker may publish first.
    for key, content, expires, scope in writes:
        service.raw.put(key, content, expires_at=expires, scope=scope, revision=content["revision"])


def _receive_transcript(service, p, call_id, segment, received_at, queue_helper):
    writes = []
    with service.db.transaction() as s:
        result, eligible = ops._transcript_in(
            service, s, p, call_id, segment, received_at, writes, queue_helper=queue_helper
        )
    ops._publish_transcripts(service, writes)
    return result, eligible


async def transcript(service, p, call_id, segment, *, queue_helper=True, received_at=None):
    import asyncio

    result, eligible = await asyncio.to_thread(
        ops._receive_transcript, service, p, call_id, segment, received_at, queue_helper
    )
    return result
