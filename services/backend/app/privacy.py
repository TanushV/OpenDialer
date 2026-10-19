"""Small process-local raw-content cache. Persistent records contain only metadata."""

import base64
import copy
import json
import math
import re
import threading
import time
from collections import OrderedDict
from datetime import timedelta

RAW_TTL_SECONDS = 300


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
        self._revoked = {}
        self._lock = threading.RLock()
        self.bytes_used = 0

    def _purge(self):
        now, mono = self.clock(), time.monotonic()
        self._revoked = {scope: until for scope, until in self._revoked.items() if until > mono}
        for key, (deadline, monotonic_deadline, _, size, _) in list(self._items.items()):
            if deadline <= now or monotonic_deadline <= mono:
                del self._items[key]
                self.bytes_used -= size

    def put(self, key, value, *, expires_at=None, scope=None, revision=None):
        with self._lock:
            self._purge()
            if scope in self._revoked:
                return
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
            self._revoked.clear()
            self.bytes_used = 0

    def revoke_scope(self, scope):
        with self._lock:
            self._purge()
            self._revoked[scope] = time.monotonic() + self.ttl
            for key, item in list(self._items.items()):
                if item[4] == scope:
                    self.discard(key)


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


def persistent_document(kind, data, *, legacy=False):
    """The explicit boundary for existing stores and content-free backup/restore."""
    data = copy.deepcopy(data)
    if kind == "usage_ledger" and "usage" in data:
        data["usage"] = usage_metadata(data["usage"])
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
                "semantic_hash",
                "transport_revision",
                "effects_complete",
                "transcript_version",
                "interpretation_revision",
                "interpreted_at",
                "question_count",
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
                "strategy_exposure",
                "status",
                "context_sources",
                "context_fingerprint",
                "meaning_review",
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
    if kind == "snippets" and legacy and data.get("call_id") and data.get("privacy_contract") != 1:
        data = {k: v for k, v in data.items() if k not in {"quote", "text", "transcript", "recording_url"}}
        data.update(raw_evidence_status="expired", approved=False)
    return data


class SpeechPrivacy:
    def raw_key(self, p, call_id, segment_id):
        return f"speech:{p['tenant_id']}:{p['seller_id']}:{call_id}:{segment_id}"

    def speech_revoked(self, s, p, call):
        from app.compliance.consent import permission_state
        from app.db import update

        if (
            call.data.get("transcription_disabled")
            or permission_state(self, s, p, call.data.get("endpoint_id"), "transcription")[1]
        ):
            if not call.data.get("transcription_disabled"):
                update(
                    call,
                    transcription_disabled=True,
                    transcript_version=call.data.get("transcript_version", 0) + 1,
                )
            self.raw.revoke_scope(p["tenant_id"] + ":" + call.id)
            return True
        return False

    def transcript_metadata(self, s, p, call_id):
        from sqlalchemy import select

        from app.db import MODELS

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

    def transcript_rows(self, s, p, call_id, *, metadata=None):
        if metadata is None:
            from app.db import MODELS

            metadata = self.context_rows(
                s,
                p,
                "transcript_segments",
                call_id,
                MODELS["transcript_segments"].ctx_expires_at > self.clock(),
                limit=self.raw.per_call_entries,
            )
        result = []
        for row in metadata:
            item = self.raw.get(self.raw_key(p, call_id, row.data["segment_id"]))
            if item is not None:
                from app.service import dt

                result.append(
                    {
                        **item,
                        "interpretation_revision": row.data.get("interpretation_revision"),
                        "raw_remaining_ms": max(
                            0, int((dt(item["raw_expires_at"]) - self.clock()).total_seconds() * 1000)
                        ),
                    }
                )
        return sorted(result, key=lambda item: (item.get("server_received_at", ""), item["segment_id"]))

    def raw_status(self, call, segments):
        if call.data.get("transcription_disabled"):
            return "permission_revoked"
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
                phone = self.required(s, "leads", call.data["lead_id"], p).data["phone"]
                return {
                    "duplicate": True,
                    "effects_complete": row.data.get("effects_complete", False),
                    "transcript_version": call.data["transcript_version"],
                    "expired": dt(row.data["raw_expires_at"]) <= self.clock(),
                }, phone
            if row and segment["revision"] <= row.data.get("transport_revision", row.data["revision"]):
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
                    "semantic_hash": semantic_hash,
                    "transport_revision": segment["revision"],
                    "effects_complete": False,
                    "transcript_version": call.data.get("transcript_version", 0) + 1,
                    "question_count": segment.get("text", "").count("?"),
                    "interpretation_revision": None,
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
                record = self._by_key(s, p, kind, call_id)
                if record:
                    if kind == "conversation_states":
                        from app.coaching.context import invalidate_state

                        state = invalidate_state(
                            record.data,
                            segment["segment_id"],
                            segment["revision"],
                            at=self.clock(),
                            lineage=self._conversation_lineage(s, p, call_id),
                        )
                        state["transcript_version"] = version
                        state["finalization"]["status"] = "pending"
                        self._store_conversation(s, p, call, state, record)
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

        call = self.required(s, "call_attempts", call_id, p)
        if self.speech_revoked(s, p, call):
            return {"unavailable": "permission_revoked", "expired": True}, False

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
            from app.telephony.reducer import TERMINAL

            if call.data["state"] in TERMINAL | {"ENDING"}:
                self.jobs.enqueue_in(
                    s,
                    "postcall",
                    {"call_id": call_id, "seller_id": p["seller_id"], "user_id": p["user_id"]},
                    f"conversation-finalize:{call_id}:{call.data['transcript_version']}",
                    tenant_id=p["tenant_id"],
                )
            else:
                self._defer_help(
                    s,
                    p,
                    call,
                    self.clock() + timedelta(milliseconds=self.settings.runtime_limits.prospect_debounce_ms),
                    "final",
                )
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
            self._receive_transcript, p, call_id, segment, received_at, True
        )
        return result

    def purge_legacy_raw_content(self, *, resume=False):
        """Authenticate every row, committing at most 500 sanitations with its cursor.

        Ingress/jobs may have no seller yet, so the existing global cutover record owns
        this store-wide cursor. Startup completes it before any request can be served.
        """
        from sqlalchemy import select

        from app.db import MODELS, Event, Job, put

        marker_model = MODELS["audit_events"]
        marker_query = select(marker_model).where(
            marker_model.tenant_id == "", marker_model.external_key == "raw-retention-v2"
        )
        kinds = [*sorted(MODELS), "events", "jobs"]
        with self.db.transaction() as s:
            marker = s.scalar(marker_query)
            if marker and resume and marker.data.get("completed_at"):
                return {"sanitized_records": marker.data["sanitized_records"]}
            if not marker:
                marker = put(s, "audit_events", "", "", {}, key="raw-retention-v2", now=self.clock())
            if not resume or not marker.data:
                marker.data = {
                    "action": "raw_retention_cutover",
                    "schema_version": 2,
                    "phase": 0,
                    "last_id": None,
                    "sanitized_records": 0,
                }
        while True:
            with self.db.transaction() as s:
                marker = s.scalar(marker_query.with_for_update())
                state = dict(marker.data)
                phase = state["phase"]
                if phase == len(kinds):
                    break
                kind = kinds[phase]
                model = Event if kind == "events" else Job if kind == "jobs" else MODELS[kind]
                key = model.sequence if kind == "events" else model.id
                query = select(model).order_by(key).limit(500)
                if state["last_id"] is not None:
                    query = query.where(key > state["last_id"])
                page = list(s.scalars(query))
                for row in page:
                    original = row.payload if kind in {"events", "jobs"} else row.data
                    clean = (
                        bus_metadata(original)
                        if kind == "events"
                        else job_metadata(original)
                        if kind == "jobs"
                        else persistent_document(kind, original, legacy=True)
                    )
                    if clean != original:
                        if kind in {"events", "jobs"}:
                            row.payload = clean
                        else:
                            row.data = clean  # Preserve IDs and externally visible record versions.
                        state["sanitized_records"] += 1
                state["last_id"] = (page[-1].sequence if kind == "events" else page[-1].id) if page else None
                if len(page) < 500:
                    state.update(phase=phase + 1, last_id=None)
                marker.data = state
        self.vault.sanitize_legacy_authorization()
        if self.db.sqlite:
            with self.db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        with self.db.transaction() as s:
            marker = s.scalar(marker_query)
            marker.data = {**marker.data, "completed_at": self.clock().isoformat()}
            result = {"sanitized_records": marker.data["sanitized_records"]}
        return result

    def privacy_cutover(self):
        return self.purge_legacy_raw_content(resume=True)

    def migrate_conversation_states(self):
        """Resumable payload migration; original record IDs and visible versions survive."""
        from sqlalchemy import select

        from app.coaching.context import lineage_metadata, migrate_note_state, upgrade_state
        from app.db import MODELS, get
        from app.postcall.rubrics import effective

        with self.db.transaction(False) as s:
            scopes = [(r.tenant_id, r.id) for r in s.scalars(select(MODELS["sellers"]))]
        for tenant, seller_id in scopes:
            p = {"tenant_id": tenant, "seller_id": seller_id}
            while True:
                with self.db.transaction() as s:
                    seller = get(s, "sellers", seller_id, tenant)
                    marker = seller.data.get("completion_contracts", {}).get("conversation", {})
                    if marker.get("completed_at"):
                        break
                    table = marker.get("last_table", "conversation_states")
                    model = MODELS[table]
                    batch = list(
                        s.scalars(
                            select(model)
                            .where(
                                model.tenant_id == tenant,
                                model.seller_id == seller_id,
                                model.id > marker.get("last_id", ""),
                            )
                            .order_by(model.id)
                            .limit(500)
                        )
                    )
                    for row in batch:
                        call = get(s, "call_attempts", row.data.get("call_id"), tenant)
                        if not call or call.seller_id != seller_id:
                            continue
                        memory = (
                            row
                            if table == "conversation_states"
                            else self._by_key(s, p, "conversation_states", call.id)
                        )
                        note = row if table == "notes" else self._by_key(s, p, "notes", call.id)
                        legacy = (
                            note
                            and "derived_state_revision" not in note.data
                            and note.data.get("privacy_contract") == 1
                        )
                        if memory and memory.data.get("schema_version") == 2 and not legacy:
                            continue
                        state = upgrade_state(
                            memory.data if memory else {}, call_id=call.id, rubric=effective(call.data)
                        )
                        if legacy:
                            revisions = (
                                {
                                    r.data["segment_id"]: r.data["revision"]
                                    for r in self.transcript_metadata(s, p, call.id)
                                }
                                if call.data.get("transcript_version") is not None
                                and note.data.get("notes", {}).get("transcript_version")
                                == call.data.get("transcript_version")
                                else {}
                            )
                            card = self._by_key(s, p, "scorecards", call.id)
                            state = migrate_note_state(
                                state,
                                note.data,
                                at=self.clock(),
                                revisions=revisions,
                                card=card.data if card else None,
                            )
                            # Strip the duplicate machine narrative. Human text, record
                            # identity and original review metadata remain untouched.
                            note.data = {
                                k: v
                                for k, v in note.data.items()
                                if k not in {"observations", "semantic_trackers"}
                            } | {
                                "notes": {
                                    k: v
                                    for k, v in note.data.get("notes", {}).items()
                                    if k in {"call_id", "transcript_version", "outcome", "grounding"}
                                },
                                "suggested_update": bool(note.data.get("suggested_update")),
                                "derived_state_revision": state["version"],
                            }
                        state["_lineage_updates"] = [lineage_metadata(i) for i in state["observations"]]
                        version = memory.version if memory else None
                        memory = self._store_conversation(s, p, call, state, memory)
                        if version is not None:
                            memory.version = version
                    contracts = dict(seller.data.get("completion_contracts", {}))
                    contracts["conversation"] = {
                        "schema_version": 2,
                        "phase": "conversation",
                        "last_table": "notes"
                        if table == "conversation_states" and len(batch) < 500
                        else table,
                        "last_id": ""
                        if table == "conversation_states" and len(batch) < 500
                        else batch[-1].id
                        if batch
                        else marker.get("last_id", ""),
                        "completed_at": self.clock().isoformat()
                        if table == "notes" and len(batch) < 500
                        else None,
                    }
                    seller.data = {**seller.data, "completion_contracts": contracts}


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
