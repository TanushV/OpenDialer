"""Small process-local raw-content cache. Persistent records contain only metadata."""

import copy
import json
import threading
import time
from collections import OrderedDict
from datetime import timedelta

RAW_TTL_SECONDS = 300


class EphemeralBuffer:
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
        self._items = OrderedDict()
        self._lock = threading.RLock()
        self.bytes_used = 0

    def _purge(self):
        now, mono = self.clock(), time.monotonic()
        for key, (deadline, monotonic_deadline, _, size, _) in list(self._items.items()):
            if deadline <= now or monotonic_deadline <= mono:
                del self._items[key]
                self.bytes_used -= size

    def put(self, key, value, *, expires_at=None, scope=None, revision=None):
        with self._lock:
            self._purge()
            deadline = min(
                expires_at or self.clock() + timedelta(seconds=self.ttl),
                self.clock() + timedelta(seconds=self.ttl),
            )
            old = self._items.get(key)
            if revision is not None and old and old[2] and old[2].get("revision", 0) > revision:
                return
            mono = time.monotonic() + max(0, (deadline - self.clock()).total_seconds())
            if old:
                deadline, mono = min(deadline, old[0]), min(mono, old[1])
                self.bytes_used -= old[3]
            size = len(json.dumps(value, ensure_ascii=False).encode())
            # Retain a deadline tombstone for oversize revisions: retries never renew a lifetime.
            if size > (self.per_call_bytes if scope else self.max_bytes) or deadline <= self.clock():
                value, size = None, 0
            self._items[key] = (deadline, mono, copy.deepcopy(value), size, scope)
            self.bytes_used += size
            if scope:
                scoped = [(k, item[3]) for k, item in self._items.items() if item[4] == scope]
                scoped_bytes, scoped_count = sum(size for _, size in scoped), len(scoped)
                for old_key, old_size in scoped:
                    if scoped_bytes <= self.per_call_bytes and scoped_count <= self.per_call_entries:
                        break
                    self.discard(old_key)
                    scoped_bytes -= old_size
                    scoped_count -= 1
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


def persistent_document(kind, data, *, legacy=False):
    """The explicit boundary for existing stores and content-free backup/restore."""
    data = copy.deepcopy(data)
    if kind in {"transcript_segments", "transcript_revisions"}:
        return {
            k: v
            for k, v in data.items()
            if k
            in {
                "call_id",
                "segment_id",
                "revision",
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
                "effects_complete",
                "transcript_version",
            }
        }
    if kind == "coach_events":
        return {
            k: v
            for k, v in data.items()
            if k
            in {
                "call_id",
                "action",
                "kind",
                "state_version",
                "evidence_segment_ids",
                "playbook_card_ids",
                "created_at",
                "expires_at",
                "raw_expires_at",
                "expires_in_ms",
                "provider",
                "actual_provider",
                "model",
                "provider_request_id",
                "latency",
                "private",
                "user_id",
                "strategy",
                "status",
            }
        }
    if kind in {"call_attempts", "provider_events", "webhook_inbox"}:
        return event_metadata(data)
    if kind == "conversation_states":
        if legacy and data.get("privacy_contract") != 1:
            return {
                k: v for k, v in data.items() if k in {"call_id", "version", "stage", "trigger", "updated_at"}
            } | {"privacy_contract": 1}
        return {
            k: v
            for k, v in data.items()
            if k not in {"recent_dialogue", "last_remote", "asked_questions", "previous_guidance", "facts"}
        }
    if kind == "notes" and legacy and data.get("privacy_contract") != 1:
        data["notes"] = {
            "call_id": data.get("call_id"),
            "transcript_version": data.get("notes", {}).get("transcript_version", 0),
            "summary": "Earlier machine notes are unavailable under the short-retention policy.",
            "outcome": data.get("notes", {}).get("outcome", "unconfirmed"),
            "problems": [],
            "objections": [],
            "commitments": [],
            "unknowns": [],
            "review_required": True,
        }
        data.update(privacy_contract=1, suggested_update=None, semantic_trackers=[])
    if kind == "scorecards" and legacy and data.get("privacy_contract") != 1:
        data["items"] = [
            {**item, "explanation": "Original evidence expired; review required."}
            for item in data.get("items", [])
        ]
        data.update(privacy_contract=1, raw_evidence_status="expired")
    if kind == "commitments" and legacy and data.get("privacy_contract") != 1 and data.get("call_id"):
        data = {
            k: v for k, v in data.items() if k not in {"quote", "text", "verbatim_time_expression", "action"}
        }
        data.update(
            action="Review the prior call commitment", privacy_contract=1, needs_time_confirmation=True
        )
    if kind == "snippets" and legacy and data.get("call_id"):
        data = {k: v for k, v in data.items() if k not in {"quote", "text", "transcript", "recording_url"}}
        data.update(raw_evidence_status="expired", approved=False)
    return data


class SpeechPrivacy:
    def raw_key(self, p, call_id, segment_id):
        return f"speech:{p['tenant_id']}:{p['seller_id']}:{call_id}:{segment_id}"

    def transcript_rows(self, s, p, call_id):
        from sqlalchemy import select

        from app.db import MODELS

        model = MODELS["transcript_segments"]
        metadata = s.scalars(
            select(model).where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.external_key.startswith(call_id + ":"),
            )
        )
        result = []
        for row in metadata:
            item = self.raw.get(self.raw_key(p, call_id, row.data["segment_id"]))
            if item is not None:
                from app.service import dt

                result.append(
                    {
                        **item,
                        "raw_remaining_ms": max(
                            0, int((dt(item["raw_expires_at"]) - self.clock()).total_seconds() * 1000)
                        ),
                    }
                )
        return sorted(result, key=lambda item: (item.get("server_received_at", ""), item["segment_id"]))

    def raw_status(self, call, segments):
        if segments:
            return "available_temporarily"
        from app.service import dt

        deadline = call.data.get("raw_expires_at")
        return (
            "unavailable_after_restart"
            if deadline and dt(deadline) > self.clock()
            else "expired"
            if deadline
            else "not_received"
        )

    def _accept_transcript(self, p, call_id, segment, received_at, *, session=None, raw_writes):
        import hashlib

        from sqlalchemy import select

        from app.db import MODELS, emit, put, update
        from app.service import dt, fail

        with self.db.transaction(session=session) as s:
            call = self.required(s, "call_attempts", call_id, p)
            model = MODELS["transcript_segments"]
            row = s.scalar(
                select(model).where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.external_key == call_id + ":" + segment["segment_id"],
                )
            )
            content_hash = hashlib.sha256(json.dumps(segment, sort_keys=True).encode()).hexdigest()
            if row and segment["revision"] <= row.data["revision"]:
                if segment["revision"] == row.data["revision"] and content_hash == row.data.get(
                    "content_hash"
                ):
                    phone = self.required(s, "leads", call.data["lead_id"], p).data["phone"]
                    return {
                        "duplicate": True,
                        "effects_complete": row.data.get("effects_complete", False),
                        "transcript_version": row.data.get(
                            "transcript_version", call.data["transcript_version"]
                        ),
                        "expired": dt(row.data["raw_expires_at"]) <= self.clock(),
                    }, phone
                fail(409, "Transcript revision is obsolete")
            received_at = min(received_at or self.clock(), self.clock())
            received = dt(row.data["server_received_at"]) if row else received_at
            expires = received + timedelta(seconds=RAW_TTL_SECONDS)
            full = {
                **segment,
                "server_received_at": received.isoformat(),
                "raw_expires_at": expires.isoformat(),
            }
            metadata = persistent_document(
                "transcript_segments",
                {
                    **full,
                    "call_id": call_id,
                    "content_hash": content_hash,
                    "effects_complete": False,
                    "transcript_version": call.data.get("transcript_version", 0) + 1,
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
                    now=self.clock(),
                )
            raw_writes.append(
                (
                    self.raw_key(p, call_id, segment["segment_id"]),
                    full,
                    expires,
                    p["tenant_id"] + ":" + call_id,
                )
            )
            update(
                call,
                transcript_version=call.data.get("transcript_version", 0) + 1,
                raw_expires_at=max(call.data.get("raw_expires_at", ""), expires.isoformat()),
            )
            version = call.data["transcript_version"]
            if row:
                from app.postcall.commitments import invalidate

                invalidate(self, s, p, call, segment["segment_id"])
            preferences = call.data.get("coach_preferences", {})
            pinned_id = preferences.get("pinned_event_id")
            if row and pinned_id:
                pinned = self.required(s, "coach_events", pinned_id, p)
                if segment["segment_id"] in pinned.data.get("evidence_segment_ids", []):
                    update(call, coach_preferences={**preferences, "pinned": False, "pinned_event_id": None})
            # Corrections invalidate machine interpretations; human edits remain authoritative.
            for kind in ("conversation_states", "notes", "scorecards"):
                for record in self.scoped(s, kind, p):
                    if record.external_key == call_id:
                        if kind == "conversation_states":
                            from app.coaching.context import STATE_FIELDS

                            update(
                                record,
                                **{
                                    key: [
                                        item
                                        for item in record.data.get(key, [])
                                        if segment["segment_id"] not in item.get("evidence_segment_ids", [])
                                    ]
                                    for key in STATE_FIELDS
                                },
                                observations=[],
                                stale=True,
                            )
                        else:
                            update(record, stale=True, source_version=version)
                            if kind == "notes" and row:
                                observations = [dict(i) for i in record.data.get("observations", [])]
                                for observation in observations:
                                    if segment["segment_id"] in observation.get("evidence", []):
                                        observation.update(
                                            status="invalidated", version=observation["version"] + 1
                                        )
                                update(record, observations=observations)
            if row:
                from app.optimization.conversations import invalidate_report

                lead = self.required(s, "leads", call.data["lead_id"], p)
                invalidate_report(self, s, p, lead.data["campaign_id"])
            emit(
                s,
                p["tenant_id"],
                "transcript.updated",
                call,
                self.clock(),
                {"segment_id": segment["segment_id"], "transcript_version": version},
            )
            phone = self.required(s, "leads", call.data["lead_id"], p).data["phone"]
            return {
                "segment_id": segment["segment_id"],
                "transcript_version": version,
                "expired": expires <= self.clock(),
            }, phone

    def _transcript_in(self, s, p, call_id, segment, received_at, raw_writes, *, queue_coach=True):
        import re

        from sqlalchemy import select

        from app.db import MODELS, update

        result, phone = self._accept_transcript(
            p, call_id, segment, received_at, session=s, raw_writes=raw_writes
        )
        if result.get("effects_complete"):
            return result, False
        if segment.get("side") == "remote" and re.search(
            r"\b(do not call|don't call|stop calling|remove (?:me|my number)|not call (?:me|again))\b",
            segment.get("text", ""),
            re.I,
        ):
            self.suppress(
                p,
                phone,
                "opt_out" if segment["is_final"] else "temporary_hold",
                event_id=f"transcript-optout:{call_id}:{segment['segment_id']}:{segment['revision']}",
                session=s,
            )
        eligible = bool(segment.get("is_final") and not result["expired"])
        if eligible and queue_coach:
            self.jobs.enqueue_in(
                s,
                "live_coach",
                {
                    "call_id": call_id,
                    "seller_id": p["seller_id"],
                    "user_id": p["user_id"],
                    "state_version": result["transcript_version"],
                },
                "coach:" + call_id + ":" + str(result["transcript_version"]),
                tenant_id=p["tenant_id"],
                priority=100,
            )
        model = MODELS["transcript_segments"]
        row = s.scalar(
            select(model).where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.external_key == call_id + ":" + segment["segment_id"],
            )
        )
        if row and row.data["revision"] == segment["revision"]:
            update(row, effects_complete=True)
        return result, eligible

    def _publish_transcripts(self, writes):
        # Publish only committed revisions. A later committing worker may publish first.
        for key, content, expires, scope in writes:
            self.raw.put(key, content, expires_at=expires, scope=scope, revision=content["revision"])

    def _receive_transcript(self, p, call_id, segment, received_at, defer_coach):
        writes = []
        with self.db.transaction() as s:
            result, eligible = self._transcript_in(
                s, p, call_id, segment, received_at, writes, queue_coach=defer_coach
            )
        self._publish_transcripts(writes)
        return result, eligible

    async def transcript(self, p, call_id, segment, *, defer_coach=False, received_at=None):
        import asyncio

        result, eligible = await asyncio.to_thread(
            self._receive_transcript, p, call_id, segment, received_at, defer_coach
        )
        if eligible and not defer_coach:
            await self.coach_turn(p, call_id)
        return result

    def purge_legacy_raw_content(self):
        from sqlalchemy import select

        from app.db import MODELS, Event, Job

        changed = 0
        with self.db.transaction() as s:
            for kind in (
                "transcript_segments",
                "transcript_revisions",
                "conversation_states",
                "coach_events",
                "notes",
                "scorecards",
                "commitments",
                "snippets",
                "call_attempts",
                "provider_events",
                "webhook_inbox",
            ):
                for row in s.scalars(select(MODELS[kind])).yield_per(250):
                    clean = persistent_document(kind, row.data, legacy=True)
                    if clean != row.data:
                        row.data = clean
                        changed += 1
            for event in s.scalars(select(Event)).yield_per(250):
                clean = (
                    persistent_document("coach_events", event.payload)
                    if event.type == "coach.card_ready"
                    else event_metadata(event.payload)
                )
                clean.update({k: v for k, v in event.payload.items() if k.startswith("_")})
                event.payload = clean
            for job in s.scalars(select(Job)).yield_per(250):
                job.payload = event_metadata(job.payload)
        if self.db.sqlite and changed:
            with self.db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"sanitized_records": changed}

    def privacy_cutover(self):
        from sqlalchemy import select

        from app.db import MODELS, put

        marker = MODELS["audit_events"]
        with self.db.transaction(False) as s:
            done = s.scalar(
                select(marker).where(marker.tenant_id == "", marker.external_key == "raw-retention-v1")
            )
        if done:
            return
        result = self.purge_legacy_raw_content()
        with self.db.transaction() as s:
            put(
                s,
                "audit_events",
                "",
                "",
                {"action": "raw_retention_cutover", **result},
                key="raw-retention-v1",
                now=self.clock(),
            )


def retained_analysis(value, segments):
    if isinstance(value, dict):
        return {
            k: retained_analysis(v, segments)
            for k, v in value.items()
            if k not in {"quote", "verbatim_time_expression"}
        }
    if isinstance(value, list):
        return [retained_analysis(v, segments) for v in value]
    if isinstance(value, str) and not paraphrased(value, segments):
        return "Source wording omitted; review the derived observation."
    return value
