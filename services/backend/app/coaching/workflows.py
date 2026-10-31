"""Proactive/private help using one bounded context and one accountable inference."""

import asyncio
import hashlib
import time
from datetime import timedelta

from sqlalchemy import or_, select

from app.coaching import engine, library, sources
from app.coaching import requests as help_requests
from app.coaching.context import (
    MeaningReviewRequired,
    build_context,
    ground_guidance,
    guidance_review_request,
    guidance_sources,
    lineage_key,
    merge_semantic_state,
    persistent_state,
    retrieve_cards,
    normalized_turn,
    reviewed_guidance,
    upgrade_state,
)
from app.coaching.contracts import card_view
from app.coaching.engine import CardRejected, finish_shielded
from app.config import LIMITS
from app.db import MODELS, doc, emit, get, put, uid, update
from app.grounding import accept_review
from app.privacy import RAW_TTL_SECONDS, persistent_document
from app.providers.inference import InferenceResult
from app.service import dt, fail
from app.telephony.reducer import TERMINAL

LINEAGE_DEPTH = 8  # Observation lineage is checkpointed by review before it grows deeper.


class LiveHelper:
    def _conversation_lineage(self, s, p, call_id):
        """Complete call history, for corrections, finalization and learning views."""
        return {
            lineage_key(r.data["id"], r.data["version"]): r.data
            for r in self.context_rows(
                s, p, "outcomes", call_id, MODELS["outcomes"].ctx_status == "conversation_lineage", limit=None
            )
        }

    def _live_lineage(self, s, p, call_id, state):
        """Only the ancestors of the current observations, read by key at each depth.

        Corrections already invalidated the complete history when they were received, so a
        live turn needs only the sources its current state can still reach.
        """
        model, lineage = MODELS["outcomes"], {}

        def ancestors(items):
            return {
                lineage_key(ref["id"], ref["version"])
                for item in items
                for ref in item.get("sources", [])
                if ref.get("kind") == "observation"
            }

        current = state if state.get("schema_version") == 2 else upgrade_state(state)
        wanted = ancestors(
            item
            for group in ("observations", "rubric_items", "tracker_matches")
            for item in current.get(group, [])
        )
        for _ in range(LINEAGE_DEPTH + 1):
            wanted -= lineage.keys()
            if not wanted:
                break
            found = {
                lineage_key(r.data["id"], r.data["version"]): r.data
                for r in s.scalars(
                    select(model).where(
                        model.tenant_id == p["tenant_id"],
                        model.seller_id == p["seller_id"],
                        model.external_key.in_([f"observation:{call_id}:{key}" for key in sorted(wanted)]),
                    )
                )
            }
            lineage.update(found)
            wanted = ancestors(found.values())
        return lineage

    def _store_conversation(self, s, p, call, state, record=None):
        lead = None
        if call.data["state"] in TERMINAL:
            # Terminal interpretation updates prior-call features on this lead.
            # Lock before flushing conversation/report changes. Do not discard a
            # caller's pending callback or human edits when taking the lock.
            with s.no_autoflush:
                lead = self.required(s, "leads", call.data["lead_id"], p)
                s.execute(select(MODELS["leads"].id).where(MODELS["leads"].id == lead.id).with_for_update())
                if not s.is_modified(lead):
                    s.refresh(lead)
        record = record or self._by_key(s, p, "conversation_states", call.id)
        for item in state.get("_lineage_updates", []):
            key = f"observation:{call.id}:{item['id']}:{item['version']}"
            prior = self._by_key(s, p, "outcomes", key)
            values = {
                **item,
                "observation_kind": item.get("observation_kind", item.get("kind")),
                "call_id": call.id,
                "kind": "conversation_lineage",
            }
            if prior:
                update(prior, **values)
            else:
                put(s, "outcomes", p["tenant_id"], p["seller_id"], values, key=key, now=self.clock())
        data = persistent_state(state)
        preserved = {
            k: v
            for k, v in (record.data if record else {}).items()
            if k
            in {
                "manual_help_requests",
                "manual_help_current",
                "coach_request_key",
                "automatic_admissions",
                "rate_limited_version",
                "rate_limited_opportunities",
                "last_request_at",
                "latest_coach_event_id",
                "review_work",
                "displayed_coach_event_ids",
                "helper_status",
            }
        }
        values = {
            **preserved,
            **data,
            "privacy_contract": 1,
            "updated_at": self.clock().isoformat(),
            "stale": False,
        }
        if record:
            record.data = values
            record.version += 1
        else:
            record = put(
                s,
                "conversation_states",
                p["tenant_id"],
                p["seller_id"],
                values,
                key=call.id,
                now=self.clock(),
            )
        if lead is not None:
            self.refresh_conversation_features_in(s, p, lead)
        return record

    def _queue_conversation_review(self, s, p, call, state):
        pending = [
            item
            for group in ("observations", "rubric_items", "tracker_matches")
            for item in state.get(group, [])
            if item.get("review_status") == "pending"
        ]
        if not pending:
            return
        due = self.clock() + timedelta(seconds=LIMITS.review_interval_seconds)
        expiries = [
            dt(ref["raw_expires_at"])
            for item in pending
            for ref in item.get("sources", [])
            if ref.get("raw_expires_at")
        ]
        if expiries:
            due = min(due, min(expiries) - timedelta(seconds=60))
        if call.data["state"] in TERMINAL | {"ENDING"}:
            due = self.clock()
        self.jobs.coalesce_live_in(
            s,
            "postcall",
            {
                "kind": "conversation_review",
                "call_id": call.id,
                "seller_id": p["seller_id"],
                "user_id": p["user_id"],
                "debounce": True,
                "pending_deadline": due.isoformat(),
            },
            f"conversation-review:{call.id}",
            tenant_id=p["tenant_id"],
            priority=2,
            available_at=max(self.clock(), due),
        )

    def _defer_help(self, s, p, call, due, marker):
        fixed_window = marker == "final" and call.data.get("transcription_source") == "direct"
        if fixed_window:
            due = self.clock() + timedelta(seconds=2)
        self.jobs.coalesce_live_in(
            s,
            "live_coach",
            {
                "call_id": call.id,
                "seller_id": p["seller_id"],
                "user_id": call.data["rep_id"],
                "state_version": call.data["transcript_version"],
                "debounce": marker == "final",
                "pending_deadline": (self.clock() + timedelta(seconds=RAW_TTL_SECONDS - 60)).isoformat(),
            },
            f"coach:{call.id}",
            tenant_id=p["tenant_id"],
            priority=100,
            available_at=due,
            fixed_window=fixed_window,
        )

    def _coach_authority(self, s, p, call=None):
        user = self.required(s, "users", p["user_id"], p)
        if user.data.get("disabled") or user.data.get("role") not in {"owner", "admin", "rep"}:
            return False
        if call is not None and user.data.get("role") == "rep" and call.data["rep_id"] != p["user_id"]:
            return False
        if p.get("session_id"):
            session = self.required(s, "sessions", p["session_id"], p)
            if session.data.get("revoked") or dt(session.data["expires_at"]) <= self.clock():
                return False
        return True

    async def practice(self, p, scenario, request_id=None):
        identity = request_id or uid()
        key = "practice:" + p["user_id"] + ":" + identity
        raw_key = p["tenant_id"] + ":" + key
        digest = hashlib.sha256(scenario.encode()).hexdigest()

        def context():
            with self.db.transaction() as s:
                user = self.required(s, "users", p["user_id"], p)
                s.refresh(user, with_for_update=True)
                if not self._coach_authority(s, p):
                    return None, {"status": "unavailable"}, None
                prior = self._by_key(s, p, "conversation_states", key)
                if prior:
                    if prior.data["request_hash"] != digest:
                        fail(409, "Practice identity already belongs to a different scenario")
                    expires = dt(prior.data["expires_at"])
                    if expires > self.clock() and not self._practice_knowledge_current(s, p, prior):
                        self.raw.discard(raw_key)
                        update(prior, status="source_changed")
                        return None, {"status": "source_changed"}, expires
                    value = self.raw.get(raw_key)
                    return (
                        None,
                        (
                            {**value, "raw_remaining_ms": self.raw.remaining_ms(raw_key) or 0}
                            if value
                            else {"status": "expired" if expires <= self.clock() else prior.data["status"]}
                        ),
                        expires,
                    )
                expires = self.clock() + timedelta(seconds=300)
                book = self.approved_playbook(s, p)
                seller = self.required(s, "sellers", p["seller_id"], p)
                examples = library.context_examples(self, s, p, scenario)
                put(
                    s,
                    "conversation_states",
                    p["tenant_id"],
                    p["seller_id"],
                    {
                        "kind": "practice",
                        "user_id": p["user_id"],
                        "request_hash": digest,
                        "expires_at": expires.isoformat(),
                        "status": "pending",
                        "privacy_contract": 1,
                        "knowledge": sources.knowledge(book, seller.data["name"], examples),
                        "personal_context": {
                            "goals_version": user.data.get("coaching_goals_version", 0),
                        },
                    },
                    key=key,
                    now=self.clock(),
                )
                return (
                    {
                        # Practice has no transcript; models number the first turn 1 (live check).
                        "state_version": 1,
                        "simulation": True,
                        "manual_help": True,
                        "private_question": scenario,
                        "seller": {
                            "name": seller.data["name"],
                            "knowledge": book.data.get("product_description", "") if book else "",
                        },
                        "approved_cards": book.data.get("cards", []) if book else [],
                        "approved_claims": book.data.get("claims", []) if book else [],
                        "prohibited_claims": book.data.get("prohibited_claims", []) if book else [],
                        "reviewed_examples": examples,
                        "practice_goals": user.data.get("coaching_goals", []),
                        "recent_dialogue": [],
                        "facts": [],
                        "context_sources": {
                            "knowledge": sources.knowledge(book, seller.data["name"], examples)
                        },
                    },
                    None,
                    expires,
                )

        request, previous, expires = await asyncio.to_thread(context)
        if previous is not None:
            return {"simulation": True, "request_id": identity, **previous}
        request["context_fingerprint"] = sources.digest(request)
        request["evidence_catalog"] = guidance_sources(request)
        request["help_request_id"] = key
        provider, route = await asyncio.to_thread(self.configured_coach, p)
        result = {"status": "unconfigured", "examples": request["approved_cards"]}
        if provider and not self.settings.simulation:
            deadline = engine.monotonic() + min(
                LIMITS.manual_full_ms / 1000,
                (expires - self.clock()).total_seconds(),
            )

            async def authorized():
                return self.coach.current(key, key) and await asyncio.to_thread(
                    self._practice_authorized, p, key
                )

            async def admit(submit, admission_deadline):
                with_authority = await authorized()
                return with_authority and (
                    not submit or await self._reserve_coach(p, key, key, "coaching_practice")
                )

            async def settle(submitted):
                await self._settle_coach(p, key, key, "coaching_practice", route, provider, submitted)

            async def publish(guidance):
                guidance = card_view(guidance)
                # Practice has no transcript: an exact context echo fixes the version and no
                # speech can be cited.
                guidance = normalized_turn(guidance, request)
                if not await authorized() or engine.monotonic() >= deadline:
                    raise CardRejected("Practice context changed")
                try:
                    ground_guidance(guidance, request)
                except MeaningReviewRequired:
                    receipt = await self._review_help(
                        p,
                        key,
                        request,
                        guidance,
                        provider,
                        route,
                        expires,
                        deadline,
                        authorize=authorized,
                        work_kind="coaching_practice_review",
                    )
                    guidance = reviewed_guidance(guidance, request, receipt)
                else:
                    guidance = {**guidance, "meaning_review": {"status": "model_suggestion"}}
                return guidance

            result = {"status": "unavailable"}
            try:
                guidance = await self.coach.suggest(
                    key,
                    request,
                    provider,
                    {"action": "NO_ACTION"},
                    deadline_ms=max(1, int((deadline - engine.monotonic()) * 1000)),
                    admit=admit,
                    settle=settle,
                    publish=publish,
                    circuit_key=(p["tenant_id"], p["seller_id"], route),
                    owner_key=(p["tenant_id"], p["seller_id"], p["user_id"]),
                )
                if guidance.get("action") == "SHOW_CARD":
                    result = {"status": "ready", "guidance": guidance}
            except (CardRejected, ValueError):
                pass
            finally:
                if asyncio.current_task().cancelling():
                    await finish_shielded(
                        asyncio.to_thread(self._finish_practice, p, key, raw_key, expires, result)
                    )
        return {
            "simulation": True,
            "request_id": identity,
            **await asyncio.to_thread(self._finish_practice, p, key, raw_key, expires, result),
        }

    def _practice_authorized(self, p, key):
        with self.db.transaction(False) as s:
            record = self._by_key(s, p, "conversation_states", key)
            return (
                self._coach_authority(s, p)
                and record is not None
                and dt(record.data["expires_at"]) > self.clock()
                and self._practice_knowledge_current(s, p, record)
            )

    def _practice_knowledge_current(self, s, p, record):
        if not sources.knowledge_current(self, s, p, record.data.get("knowledge")):
            return False
        user = self.required(s, "users", p["user_id"], p)
        return record.data.get("personal_context", {}).get("goals_version") == user.data.get(
            "coaching_goals_version", 0
        )

    def _finish_practice(self, p, key, raw_key, expires, result):
        with self.db.transaction() as s:
            record = self._by_key(s, p, "conversation_states", key)
            if not self._coach_authority(s, p) or expires <= self.clock():
                result = {"status": "unavailable"}
            elif not self._practice_knowledge_current(s, p, record):
                result = {"status": "source_changed"}
            update(
                record,
                status=result["status"] if result["status"] != "ready" else "unavailable_after_restart",
            )
            self.raw.put(
                raw_key, result, expires_at=expires, scope=p["tenant_id"] + ":practice:" + p["user_id"]
            )
        remaining = self.raw.remaining_ms(raw_key)
        return {
            **result,
            "raw_expires_at": expires.isoformat(),
            "raw_remaining_ms": remaining
            if remaining is not None
            else max(0, int((expires - self.clock()).total_seconds() * 1000)),
        }

    def coach_content(self, p, record):
        if record.get("private") and record.get("user_id") != p["user_id"]:
            return None
        return self.raw.get("help:" + p["tenant_id"] + ":" + record["id"])

    def visible_help(self, s, p, call, state):
        """Read at most the current answer and explicitly pinned answer, by identity."""
        if call.data["state"] in TERMINAL | {"ENDING"}:
            return None
        latest = state.get("latest_coach_event_id")
        pinned = call.data.get("coach_preferences", {}).get("pinned_event_id")
        manual = next(
            (r for r in state.get("manual_help_requests", []) if r["id"] == state.get("manual_help_current")),
            {},
        )
        identities = [latest, pinned] if latest and manual.get("event_id") == latest else [pinned, latest]
        for identity in dict.fromkeys(identities):
            row = get(s, "coach_events", identity, p["tenant_id"]) if identity else None
            if not row or row.seller_id != p["seller_id"] or row.data["call_id"] != call.id:
                continue
            is_pinned = identity == pinned
            deadline = dt(row.data["raw_expires_at"] if is_pinned else row.data["expires_at"])
            if deadline <= self.clock() or (
                not is_pinned and row.data["state_version"] != call.data["transcript_version"]
            ):
                continue
            content = self.coach_content(p, doc(row))
            if content and sources.current(self, s, p, row.data.get("context_sources")):
                shown = int((deadline - self.clock()).total_seconds() * 1000)
                raw = self.raw.remaining_ms("help:" + p["tenant_id"] + ":" + row.id)
                return {
                    **content,
                    "pinned": is_pinned,
                    "expires_at": deadline.isoformat(),
                    # The display deadline, never past the raw content's own lifetime.
                    "remaining_ms": max(0, min(shown, raw if raw is not None else shown)),
                }
        return None

    async def _reserve_coach(self, p, key, object_id, kind):
        # A cancelled to_thread still runs. Finish admission before deciding whether
        # the hold is unused; cleanup is shared by practice and live help.
        return await finish_shielded(
            asyncio.to_thread(
                self.reserve_optional_budget,
                p,
                key,
                0.01,
                object_id=object_id,
                work_kind=kind,
            )
        )

    async def _settle_coach(self, p, key, object_id, kind, route, provider, submitted):
        if submitted:
            await asyncio.to_thread(
                self.usage,
                p,
                route,
                kind,
                InferenceResult(
                    {}, getattr(provider, "last_usage", {}), getattr(provider, "last_request_id", None)
                ),
                object_id=object_id,
                reserved=0,
                request_key=key,
            )
        else:
            await asyncio.to_thread(self.release_unsubmitted_budget, p, key)

    def _help_allowed(self, s, p, call):
        user = self.required(s, "users", p["user_id"], p)
        return self._coach_authority(s, p) and not (
            user.data.get("role") == "rep"
            and call.data["rep_id"] != p["user_id"]
            or call.data["state"] in TERMINAL | {"ENDING"}
            or call.data.get("transcription_disabled")
        )

    def _help_context(self, p, call_id, urgent, question, identity=None, *, mode="live"):
        started = time.perf_counter()
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            # Context/receipt writes for this call share one short lock. No provider
            # I/O occurs here, and unrelated calls remain independent.
            s.refresh(call, with_for_update=True)
            if mode == "live" and not self._help_allowed(s, p, call):
                return None
            if mode != "live" and not self._coach_authority(s, p, call):
                return None
            lead = self.required(s, "leads", call.data["lead_id"], p)
            if mode == "live" and self.suppressed(s, p, lead.data["phone"]):
                return None
            preferences = call.data.get("coach_preferences", {})
            stable = [
                r
                for r in self.transcript_rows(s, p, call_id)
                if r.get("is_final") and not r.get("system_audio")
            ]
            if not stable and not urgent:
                return None
            pending = [item for item in stable if item.get("interpretation_revision") != item["revision"]]
            if not pending and not urgent:
                return None
            useful_until = self.clock() + timedelta(seconds=8 if urgent else 1.5)
            recent = [item for item in stable if dt(item["raw_expires_at"]) > useful_until or item in pending]
            # Service older unobserved chunks as well as the current turn, without growing the prompt.
            selected = {item["segment_id"] for item in [*pending[:16], *recent[-16:]]}
            stable = [item for item in stable if item["segment_id"] in selected]
            version = call.data.get("transcript_version", 0)
            state_record = self._by_key(s, p, "conversation_states", call_id)
            from app.postcall.rubrics import effective

            previous = upgrade_state(
                state_record.data if state_record else {}, call_id=call_id, rubric=effective(call.data)
            )
            previous["_lineage"] = self._live_lineage(s, p, call_id, previous)
            previous.update(
                {
                    k: v
                    for k, v in (state_record.data if state_record else {}).items()
                    if k in {"last_request_at", "manual_help_requests"}
                }
            )
            if (
                not urgent
                and state_record
                and self._automatic_admissions_in(s, p, call, state_record) is None
            ):
                return None
            seller = self.required(s, "sellers", p["seller_id"], p)
            interval = seller.data.get("operating_policy", {}).get("auto_coach_interval_seconds", 8)
            facts = [
                doc(r)
                for r in self.context_rows(
                    s,
                    p,
                    "research_facts",
                    lead.id,
                    MODELS["research_facts"].ctx_status == "supported",
                    MODELS["research_facts"].ctx_expires_at > self.clock(),
                    limit=8,
                )
            ]
            script = call.data.get("script_snapshot") or self.selected_script(s, p, lead)[0]
            facts = list({f["id"]: f for f in [*facts, *self.script_evidence(s, p, lead, script)]}.values())
            commitment_model = MODELS["commitments"]
            commitments = [
                doc(r)
                for r in self.lead_records(
                    s,
                    p,
                    "commitments",
                    lead.id,
                    or_(
                        commitment_model.op_status.is_(None),
                        commitment_model.op_status.not_in(["cancelled", "completed", "retracted"]),
                    ),
                    limit=5,
                    newest=True,
                )
            ]
            state = build_context(
                stable, version=version, facts=facts, commitments=commitments, prior=previous, at=self.clock()
            )
            if urgent:
                state["trigger"] = "manual_help"
            state_record = self._store_conversation(s, p, call, state, state_record)
            # Observe every stable chunk; coalesce paid work within the configured quiet interval.
            if (
                not urgent
                and not any(item.get("side") == "remote" for item in pending)
                and previous.get("last_request_at")
                and (self.clock() - dt(previous["last_request_at"])).total_seconds() < interval
            ):
                self._defer_help(
                    s,
                    p,
                    call,
                    dt(previous["last_request_at"]) + timedelta(seconds=interval),
                    previous["last_request_at"],
                )
                return None
            book = self.approved_playbook(s, p)
            all_cards = book.data.get("cards", []) if book else []
            retrieval_started = time.perf_counter()
            cards = retrieve_cards(state, all_cards)
            retrieval_ms = (time.perf_counter() - retrieval_started) * 1000
            # Cached knowledge is shown only while the helper is unconfigured or its circuit is
            # open. A transient failure stays quiet with an explicit helper status instead.
            fallback = {"action": "NO_ACTION", "state_version": version}
            if cards and state["last_remote"]:
                fallback = {
                    "action": "SHOW_CARD",
                    "kind": "cached_tip",
                    "say_or_ask": cards[0]["text"],
                    "reason": "An available knowledge-library example; contextual AI is unavailable.",
                    "evidence_segment_ids": state["last_remote"]["evidence_segment_ids"],
                    "playbook_card_ids": [cards[0]["id"]],
                    "state_version": version,
                    "expires_in_ms": 30000,
                }
            previous_calls = list(
                reversed(
                    self.lead_records(
                        s,
                        p,
                        "call_attempts",
                        lead.id,
                        MODELS["call_attempts"].id != call_id,
                        MODELS["call_attempts"].op_status.in_(TERMINAL),
                        limit=3,
                        newest=True,
                    )
                )
            )
            note_model = MODELS["notes"]
            notes = {
                r.data["call_id"]: self.note_view_in(s, p, r)
                for r in s.scalars(
                    select(note_model).where(
                        note_model.tenant_id == p["tenant_id"],
                        note_model.seller_id == p["seller_id"],
                        note_model.external_key.in_([c.id for c in previous_calls]),
                    )
                )
            }
            from app.optimization.conversations import active_observations

            request = {
                "state_version": version,
                "state": persistent_state(state),
                "mode": mode,
                "recent_dialogue": stable,
                "automatic_tips_muted": bool(preferences.get("muted") and not urgent),
                "manual_help": urgent and mode == "live",
                "private_question": question,
                "approved_cards": all_cards[:32],
                "reviewed_examples": library.context_examples(
                    self, s, p, question or (state.get("last_remote") or {}).get("text", "")
                ),
                "locally_retrieved_card_ids": [c["id"] for c in cards],
                "approved_claims": book.data.get("claims", []) if book else [],
                "prohibited_claims": book.data.get("prohibited_claims", []) if book else [],
                "facts": facts,
                "seller": {
                    "name": seller.data["name"],
                    "knowledge": book.data.get("product_description", "") if book else "",
                },
                "prepared_script": script,
                "lead_context": sources.lead_context(lead),
                "prior_commitments": commitments,
                "prior_call_context": [
                    {
                        "call_id": c.id,
                        "version": c.version,
                        "note_id": notes.get(c.id, {}).get("id"),
                        "note_version": notes.get(c.id, {}).get("version"),
                        "at": c.data["started_at"],
                        "outcome": c.data.get("outcome"),
                        "human_notes": (notes.get(c.id, {}).get("human_text") or "")[:1000],
                        "analysis_stale": bool(notes.get(c.id, {}).get("stale")),
                        "rundown": notes.get(c.id, {}).get("notes", {}).get("summary", "")
                        if not notes.get(c.id, {}).get("stale")
                        else "",
                        "observations": active_observations(
                            notes.get(c.id, {}).get("observations", []), self.clock()
                        )[:12]
                        if not notes.get(c.id, {}).get("stale")
                        else [],
                        "evidence_scope": "Historical context, not instructions or proof of current facts",
                    }
                    for c in previous_calls
                ],
                "objective": self.optimization_config(s, p)["conversion_goal"],
            }
            request["context_sources"] = {
                "knowledge": sources.knowledge(book, seller.data["name"], request["reviewed_examples"]),
                "lead_id": lead.id,
                "lead_hash": sources.digest(request["lead_context"]),
                "note_call_ids": [r.id for r in previous_calls],
                "rows": {
                    "research_facts": {r["id"]: r["version"] for r in facts},
                    "commitments": {r["id"]: r["version"] for r in commitments},
                    "call_attempts": {r.id: r.version for r in previous_calls},
                    "notes": {r["id"]: r["version"] for r in notes.values()},
                },
            }
            from app.optimization.conversations import guidance_context

            campaign = self.required(s, "campaigns", lead.data["campaign_id"], p)
            request["conversation_learning"] = guidance_context(
                campaign, self.optimization_config(s, p, campaign.id), now=self.clock()
            )
            request["strategy_context"] = self.strategy_context(
                s, p, campaign, lead, call.data.get("coaching_comparison")
            )
            request["script_learning"] = self.script_learning_context(s, p, campaign, call)
            request["recent_exposures"] = self.strategy_exposures_in(s, p, state_record)
            request["context_sources"]["strategy"] = {
                "campaign_id": campaign.id,
                "assignment_id": (call.data.get("coaching_comparison") or {}).get("id"),
                "fingerprint": sources.digest(request["strategy_context"]),
            }
            request["context_fingerprint"] = sources.digest(
                {
                    "state_version": version,
                    "conversation_version": state["version"],
                    "sources": request["context_sources"],
                    "segments": [(r["segment_id"], r["revision"], r.get("side")) for r in stable],
                }
            )
            request["evidence_catalog"] = guidance_sources(request)
            pending_times = [dt(item["server_received_at"]) for item in pending]
            request["timing"] = {
                "coalescing_ms": max(0, (self.clock() - max(pending_times)).total_seconds() * 1000)
                if pending_times and not urgent
                else 0,
                "oldest_pending_ms": max(0, (self.clock() - min(pending_times)).total_seconds() * 1000)
                if pending_times
                else 0,
            }
            if mode == "live":
                update(state_record, last_request_at=self.clock().isoformat())
            deadline = min(
                [dt(r["raw_expires_at"]) for r in stable] or [self.clock() + timedelta(seconds=300)]
            )
            if identity:
                original = next(
                    (r for r in previous.get("manual_help_requests", []) if r["id"] == identity), None
                )
                if not original:
                    return None
                deadline = min(deadline, dt(original["expires_at"]))
            return (
                request,
                fallback,
                deadline,
                (time.perf_counter() - started) * 1000,
                retrieval_ms,
                call.data.get("coaching_comparison"),
            )

    def cancel_help(self, p, call_id, request_id):
        return help_requests.cancel(self, p, call_id, request_id)

    async def coach_turn(self, p, call_id, urgent=False, question=None, request_id=None):
        identity = request_id or uid() if urgent else None
        result = {"action": "NO_ACTION", "status": "cancelled"}
        if identity:
            try:
                prior = await finish_shielded(
                    asyncio.to_thread(
                        help_requests.begin,
                        self,
                        p,
                        call_id,
                        identity,
                        question,
                    )
                )
            except asyncio.CancelledError:
                await finish_shielded(asyncio.to_thread(self.cancel_help, p, call_id, identity))
                raise
            if prior is not None:
                return {**prior, "request_id": identity, "private": bool(question)}
        try:
            result = await self._coach_turn(p, call_id, urgent, question, identity)
        finally:
            if identity:
                result = await finish_shielded(
                    asyncio.to_thread(
                        help_requests.finish,
                        self,
                        p,
                        call_id,
                        identity,
                        result,
                    )
                )
        return {**result, "request_id": identity, "private": bool(question)}

    async def _coach_turn(self, p, call_id, urgent=False, question=None, identity=None):
        request_started = engine.monotonic()
        prepared = await asyncio.to_thread(self._help_context, p, call_id, urgent, question, identity)
        if not prepared:
            return {"action": "NO_ACTION", "status": "no_action", "private": bool(question)}
        request, fallback, expires, state_ms, retrieval_ms, comparison = prepared
        remaining = (expires - self.clock()).total_seconds()
        allowed_ms = LIMITS.manual_full_ms if urgent else LIMITS.automatic_deadline_ms
        # Every ceiling is clipped to the original raw-speech expiry.
        monotonic_deadline = min(request_started + allowed_ms / 1000, engine.monotonic() + remaining)
        if remaining <= 0:
            return {"action": "NO_ACTION", "status": "expired", "private": bool(question)}
        # The config read is file I/O: keep it off the event loop.
        provider, route = await asyncio.to_thread(self.configured_coach, p)
        if provider:
            # The first-useful ceiling bounds the first displayable content only; generation
            # and any review of a factual answer may run to the full ceiling.
            first_useful = min(request_started + LIMITS.manual_first_ms / 1000, monotonic_deadline)
            provider.first_useful_ms = max(1, int((first_useful - engine.monotonic()) * 1000))
            provider.connect_ms = LIMITS.manual_connect_ms
        if (
            (comparison or {}).get("arm") == "control"
            and not (comparison or {}).get("strategy_id")
            and not urgent
        ):
            request["guidance_display"] = "approved_static_only"
        result, source = fallback, "cached_knowledge"
        request_key = help_requests.request_key(call_id, identity or uid())
        request["manual_request_id"] = identity
        began = time.perf_counter()
        if provider and not self.settings.simulation:
            request["help_request_id"] = request_key
            progress_id = final_id = None

            async def publish_progress(result):
                nonlocal progress_id
                shown = await self._validated_help(
                    p,
                    call_id,
                    request,
                    result,
                    route,
                    provider,
                    expires,
                    state_ms,
                    retrieval_ms,
                    (time.perf_counter() - began) * 1000,
                    progressive=True,
                    deadline=monotonic_deadline,
                )
                progress_id = shown.get("id")
                return bool(progress_id)

            if question:

                async def progress(result):
                    # A cancelled storage thread still runs: retain its identity
                    # before propagating cancellation so finally can withdraw it.
                    return await finish_shielded(publish_progress(result))

                provider.on_progress = progress

            async def admit(submit, admission_deadline):
                if not await asyncio.to_thread(self._admit_help, p, call_id, request):
                    return False
                if not submit:
                    return True
                allowed = await self._reserve_coach(p, request_key, call_id, "live_coach")
                if not allowed or (expires - self.clock()).total_seconds() <= 0:
                    return False
                return await asyncio.to_thread(
                    self._admit_help, p, call_id, request, True, admission_deadline
                )

            async def settle(submitted):
                await self._settle_coach(p, request_key, call_id, "live_coach", route, provider, submitted)

            async def publish(result):
                nonlocal final_id
                cached = result is fallback  # Only while the route's circuit is open.
                final = await self._validated_help(
                    p,
                    call_id,
                    request,
                    result,
                    source if cached else route,
                    provider,
                    expires,
                    state_ms,
                    retrieval_ms,
                    (time.perf_counter() - began) * 1000,
                    event_id=None if cached else progress_id,
                    deadline=monotonic_deadline,
                )
                final_id = final.get("id")
                return final

            try:
                result = await self.coach.suggest(
                    call_id,
                    request,
                    provider,
                    fallback,
                    deadline_ms=max(1, int((monotonic_deadline - engine.monotonic()) * 1000)),
                    admit=admit,
                    settle=settle,
                    publish=publish,
                    circuit_key=(p["tenant_id"], p["seller_id"], route),
                    owner_key=(p["tenant_id"], p["seller_id"], p["user_id"]),
                )
            finally:
                if progress_id and final_id != progress_id:
                    await finish_shielded(asyncio.to_thread(self._withdraw_help, p, call_id, progress_id))
            if not urgent and result.get("status") in {"busy", "manual_pending"}:
                await asyncio.to_thread(self._defer_busy_help, p, call_id)
            return {**result, "private": bool(question)}
        return await self._validated_help(
            p,
            call_id,
            request,
            result,
            source,
            provider,
            expires,
            state_ms,
            retrieval_ms,
            (time.perf_counter() - began) * 1000,
        )

    def _admit_help(self, p, call_id, request, count_admission=False, admission_deadline=None):
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            s.refresh(call, with_for_update=True)
            lead = self.required(s, "leads", call.data["lead_id"], p)
            state = self._by_key(s, p, "conversation_states", call_id)
            if (
                not self.coach.current(call_id, request["help_request_id"])
                or not help_requests.current(self, state, request)
                or not self._help_allowed(s, p, call)
                or call.data["transcript_version"] != request["state_version"]
                or self.suppressed(s, p, lead.data["phone"])
                or not sources.current(self, s, p, request.get("context_sources"))
                # A late admission is rejected before it is counted as a spent request.
                or admission_deadline is not None
                and engine.monotonic() >= admission_deadline
            ):
                return False
            if count_admission and not request.get("manual_help"):
                admitted = self._automatic_admissions_in(s, p, call, state)
                if admitted is None:
                    return False
                if not any(item["id"] == request["help_request_id"] for item in admitted):
                    admitted.append({"id": request["help_request_id"], "at": self.clock().isoformat()})
                    update(state, automatic_admissions=admitted)
            update(state, coach_request_key=request["help_request_id"])
            return True

    def _automatic_admissions_in(self, s, p, call, state):
        admitted = [
            item
            for item in state.data.get("automatic_admissions", [])
            if dt(item["at"]) > self.clock() - timedelta(seconds=60)
        ]
        if call.data.get("transcription_source") == "direct" and admitted:
            next_at = max(dt(item["at"]) for item in admitted) + timedelta(seconds=2)
            if self.clock() < next_at:
                self._defer_help(s, p, call, next_at, "cadence")
                return None
        if len(admitted) < LIMITS.automatic_per_call_minute:
            return admitted
        version = call.data["transcript_version"]
        if state.data.get("rate_limited_version") != version:
            update(
                state,
                rate_limited_version=version,
                rate_limited_opportunities=state.data.get("rate_limited_opportunities", 0) + 1,
            )
        self._defer_help(s, p, call, dt(admitted[0]["at"]) + timedelta(seconds=60), "rate")
        return None

    def _defer_busy_help(self, p, call_id):
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            if call.data["state"] not in TERMINAL | {"ENDING"}:
                due = self.clock() + timedelta(milliseconds=LIMITS.prospect_debounce_ms)
                self._defer_help(s, p, call, due, "capacity:" + due.isoformat())

    async def _validated_help(
        self,
        p,
        call_id,
        request,
        result,
        source,
        provider,
        expires,
        state_ms,
        retrieval_ms,
        inference_ms,
        *,
        progressive=False,
        event_id=None,
        deadline=None,
    ):
        review_ms = 0.0
        try:
            result = normalized_turn(card_view(result), request)
            try:
                ground_guidance(result, request, cached=source == "cached_knowledge", progressive=progressive)
            except MeaningReviewRequired:
                if progressive or provider is None or deadline is None:
                    raise CardRejected("Guidance is awaiting independent review") from None
                review_started = time.perf_counter()
                receipt = await self._review_help(
                    p, call_id, request, result, provider, source, expires, deadline
                )
                review_ms = (time.perf_counter() - review_started) * 1000
                result = reviewed_guidance(result, request, receipt)
            else:
                if result.get("action") == "SHOW_CARD":
                    result = {
                        **result,
                        "meaning_review": {
                            "status": "seller_approved"
                            if source == "cached_knowledge"
                            else "model_suggestion"
                        },
                    }
        except (CardRejected, ValueError):
            # An unsafe suggestion need not lose a separately validated interpretation.
            result = {
                "action": "NO_ACTION",
                "state_version": request["state_version"],
                "state_update": result.get("state_update")
                if isinstance(result, dict) and result.get("state_version") == request["state_version"]
                else None,
            }
        unavailable = result.get("status") == "unavailable"  # A failed request, not a quiet one.
        result = {
            **result,
            "private": bool(request.get("private_question")),
            "status": ("streaming" if progressive else "ready")
            if result["action"] != "NO_ACTION"
            else "unavailable"
            if unavailable
            else "no_action",
        }
        try:
            return await asyncio.to_thread(
                self._publish_help,
                p,
                call_id,
                request,
                result,
                source,
                provider,
                expires,
                state_ms,
                retrieval_ms,
                inference_ms,
                event_id,
                review_ms,
            )
        except CardRejected:
            return {
                "action": "NO_ACTION",
                "status": "no_action",
                "private": bool(request.get("private_question")),
            }

    async def _review_help(
        self,
        p,
        call_id,
        request,
        candidate,
        provider,
        route,
        expires,
        deadline,
        *,
        authorize=None,
        work_kind="live_coach_review",
    ):
        """Optional independent review in the existing live slot, with its own liability."""
        if not hasattr(provider, "review"):
            raise CardRejected("Independent live review unavailable")
        review = guidance_review_request(candidate, request)
        key = f"{request.get('help_request_id', call_id)}:review:{uid()}"
        generation = {
            name: getattr(provider, name, None)
            for name in ("last_usage", "last_request_id", "last_provider", "last_model")
        }
        submitted = False

        async def current():
            return (
                await authorize()
                if authorize
                else await asyncio.to_thread(self._admit_help, p, call_id, request)
            )

        def remaining():
            return min(deadline - engine.monotonic(), (expires - self.clock()).total_seconds())

        try:
            if remaining() <= 0 or not await current():
                raise CardRejected("Review context expired or changed")
            if not await self._reserve_coach(p, key, call_id, work_kind):
                raise CardRejected("Review budget unavailable")
            if remaining() <= 0 or not await current():
                raise CardRejected("Review context expired before submission")
            # Never attribute the generation receipt to a review that failed before
            # receiving a response, including provider adapters supplied by tests.
            provider.last_usage, provider.last_request_id = {}, None
            submitted = True
            result = await asyncio.wait_for(
                provider.review(review, deadline_ms=max(1, int(remaining() * 1000))), remaining()
            )
            if remaining() <= 0 or not await current():
                raise CardRejected("Review completed after its original context deadline")
            if str(getattr(provider, "last_provider", "") or "").casefold().split("/")[0] != "cerebras":
                raise CardRejected("Review did not verify the Cerebras provider")
            # Per-field rejections are reported, so a separately audited nonassertive field
            # can still be shown; reviewed_guidance never displays a rejected factual field.
            return accept_review(review, result, getattr(provider, "last_request_id", None), partial=True)
        except Exception as exc:
            raise CardRejected("Independent meaning review unavailable or unsupported") from exc
        finally:
            try:
                await finish_shielded(
                    self._settle_coach(p, key, call_id, work_kind, route, provider, submitted)
                )
            finally:
                for name, value in generation.items():
                    setattr(provider, name, value)

    def _withdraw_help(self, p, call_id, event_id):
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            event = self.required(s, "coach_events", event_id, p)
            if event.data["call_id"] != call.id:
                raise CardRejected("Incorrect guidance identity")
            update(event, expires_at=self.clock().isoformat(), status="withdrawn")
            self.raw.discard("help:" + p["tenant_id"] + ":" + event_id)
            emit(s, p["tenant_id"], "coach.cancelled", call, self.clock())

    def _publish_help(
        self,
        p,
        call_id,
        request,
        result,
        source,
        provider,
        expires,
        state_ms,
        retrieval_ms,
        inference_ms,
        event_id=None,
        review_ms=0.0,
    ):
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            s.refresh(call, with_for_update=True)
            key = request.get("help_request_id")
            state_record = self._by_key(s, p, "conversation_states", call_id)
            lead = self.required(s, "leads", call.data["lead_id"], p)
            if (
                not self._help_allowed(s, p, call)
                or not sources.current(self, s, p, request.get("context_sources"))
                or self.suppressed(s, p, lead.data["phone"])
                or not help_requests.current(self, state_record, request)
                or key
                and (
                    state_record.data.get("coach_request_key") != key or not self.coach.current(call_id, key)
                )
            ):
                return {"action": "NO_ACTION", "status": "superseded", "private": result["private"]}
            # Only live segment metadata, not the whole call's history; expired rows are moot.
            metadata = self.context_rows(
                s,
                p,
                "transcript_segments",
                call_id,
                MODELS["transcript_segments"].ctx_expires_at > self.clock(),
                limit=self.raw.per_call_entries,
            )
            current = {row.data["segment_id"]: row for row in metadata}
            if expires <= self.clock() or any(
                item["segment_id"] not in current
                or current[item["segment_id"]].data["revision"] != item["revision"]
                or not self.raw.has(self.raw_key(p, call_id, item["segment_id"]))
                for item in request["recent_dialogue"]
            ):
                return {"action": "NO_ACTION", "status": "expired_or_corrected", "private": result["private"]}
            # A metadata-only helper receipt: failed, cached-knowledge mode or contextual help.
            helper = (
                "unavailable"
                if result.get("status") == "unavailable"
                else "cached_knowledge"
                if source == "cached_knowledge"
                else "available"
            )
            if (state_record.data.get("helper_status") or {}).get("status") != helper:
                update(
                    state_record,
                    helper_status={
                        "status": helper,
                        "reason": result.get("failure") if helper == "unavailable" else None,
                        "at": self.clock().isoformat(),
                    },
                )
            if (
                result.get("state_update") is not None
                and result.get("state_version") == request["state_version"]
            ):
                state = merge_semantic_state(
                    {**state_record.data, "_lineage": self._live_lineage(s, p, call_id, state_record.data)},
                    result["state_update"],
                    request["recent_dialogue"],
                    request_id=key,
                    at=self.clock(),
                    hash_sources=True,
                    catalog=request.get("evidence_catalog", []),
                    call_at=dt(call.data.get("started_at")),
                    timezone=lead.data.get("timezone"),
                    exposures=request.get("recent_exposures", []),
                )
                state["transcript_version"] = call.data["transcript_version"]
                state_record = self._store_conversation(s, p, call, state, state_record)
                self._queue_conversation_review(s, p, call, state)
                for item in state.get("_interpreted_segments", []):
                    row = current[item["id"]]
                    update(
                        row, interpretation_revision=item["version"], interpreted_at=self.clock().isoformat()
                    )
                if call.data["state"] not in TERMINAL | {"ENDING"} and any(
                    item.get("interpretation_revision") != item["revision"]
                    for item in self.transcript_rows(s, p, call_id, metadata=metadata)
                ):
                    seller = self.required(s, "sellers", p["seller_id"], p)
                    interval = seller.data.get("operating_policy", {}).get("auto_coach_interval_seconds", 8)
                    self._defer_help(
                        s, p, call, self.clock() + timedelta(seconds=interval), state_record.version
                    )
            emit(s, p["tenant_id"], "conversation.updated", call, self.clock())
            if (
                call.data["transcript_version"] != request["state_version"]
                or call.data["state"] in TERMINAL | {"ENDING"}
                or (call.data.get("coach_preferences", {}).get("muted") and not request["manual_help"])
                or request.get("guidance_display") == "approved_static_only"
                and source != "cached_knowledge"
            ):
                return {"action": "NO_ACTION", "status": "context_updated", "private": result["private"]}
            if result["action"] != "NO_ACTION" and expires > self.clock():
                deadline = min(expires, self.clock() + timedelta(milliseconds=result["expires_in_ms"]))
                metadata = persistent_document(
                    "coach_events",
                    {
                        "call_id": call_id,
                        "context_sources": request["context_sources"],
                        **result,
                        "user_id": p["user_id"],
                        "created_at": self.clock().isoformat(),
                        "expires_at": deadline.isoformat(),
                        "raw_expires_at": expires.isoformat(),
                        "provider": source,
                        "actual_provider": getattr(provider, "last_provider", None),
                        "model": getattr(provider, "last_model", None),
                        "provider_request_id": getattr(provider, "last_request_id", None),
                        "strategy_exposure": {
                            **{
                                k: v
                                for k, v in request.get("strategy_context", {}).items()
                                if k != "approach"
                            },
                            "generated_at": self.clock().isoformat(),
                            "displayed_at": None,
                            "used_by_rep": None,
                            "observed_in_speech": [],
                            "conversion": None,
                            "sentence_propensity": None,
                        },
                        "latency": {
                            **request.get("timing", {}),
                            "state_ms": max(0, state_ms - retrieval_ms),
                            "retrieval_ms": retrieval_ms,
                            "state_retrieval_ms": state_ms,
                            "generation_ms": inference_ms,
                            "review_ms": review_ms,
                            "inference_ms": inference_ms + review_ms,
                            "total_server_ms": request.get("timing", {}).get("coalescing_ms", 0)
                            + state_ms
                            + inference_ms
                            + review_ms,
                            "stt_ms": None,
                        },
                    },
                )
                if event_id:
                    obj = self.required(s, "coach_events", event_id, p)
                    if obj.data["call_id"] != call_id or obj.data.get("user_id") != p["user_id"]:
                        raise CardRejected("Incorrect guidance identity")
                    metadata["created_at"] = obj.data["created_at"]
                    metadata["strategy_exposure"] = obj.data.get(
                        "strategy_exposure", metadata["strategy_exposure"]
                    )
                    metadata["latency"]["first_useful_ms"] = obj.data["latency"]["inference_ms"]
                    update(obj, **metadata)
                else:
                    obj = put(s, "coach_events", p["tenant_id"], p["seller_id"], metadata, now=self.clock())
                update(state_record, latest_coach_event_id=obj.id)
                if request.get("manual_request_id"):
                    receipts = [dict(r) for r in state_record.data.get("manual_help_requests", [])]
                    for receipt in receipts:
                        if receipt["id"] == request["manual_request_id"]:
                            receipt["event_id"] = obj.id
                    update(state_record, manual_help_requests=receipts)
                self.raw.put(
                    "help:" + p["tenant_id"] + ":" + obj.id,
                    {**doc(obj), **result},
                    expires_at=expires,
                    scope=p["tenant_id"] + ":" + call_id,
                )
                emit(s, p["tenant_id"], "coach.card_ready", call, self.clock(), {"coach_event_id": obj.id})
                result = {**result, "id": obj.id, "latency": metadata["latency"]}
        return result
