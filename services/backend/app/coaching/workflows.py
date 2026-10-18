"""Proactive/private help using one bounded context and one accountable inference."""

import asyncio
import hashlib
import time
from datetime import timedelta

from sqlalchemy import or_, select

from app.coaching import library, sources
from app.coaching import requests as help_requests
from app.coaching.context import (
    build_context,
    ground_guidance,
    merge_semantic_state,
    persistent_state,
    retrieve_cards,
)
from app.coaching.engine import CardRejected, finish_shielded
from app.db import MODELS, doc, emit, get, put, uid, update
from app.privacy import persistent_document
from app.providers.inference import InferenceResult
from app.service import dt, fail
from app.telephony.reducer import TERMINAL


class LiveHelper:
    def _defer_help(self, s, p, call, due, marker):
        self.jobs.enqueue_in(
            s,
            "live_coach",
            {
                "call_id": call.id,
                "seller_id": p["seller_id"],
                "user_id": call.data["rep_id"],
                "state_version": call.data["transcript_version"],
            },
            f"coach-due:{call.id}:{call.data['transcript_version']}:{marker}",
            tenant_id=p["tenant_id"],
            priority=100,
            available_at=due,
        )

    def _coach_authority(self, s, p):
        user = self.required(s, "users", p["user_id"], p)
        if user.data.get("disabled") or user.data.get("role") not in {"owner", "admin", "rep"}:
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
                            {
                                **value,
                                "raw_remaining_ms": max(
                                    0, int((expires - self.clock()).total_seconds() * 1000)
                                ),
                            }
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
                        "state_version": 0,
                        "simulation": True,
                        "manual_help": True,
                        "private_question": scenario,
                        "seller": {
                            "name": seller.data["name"],
                            "knowledge": book.data.get("product_description", "") if book else "",
                        },
                        "approved_cards": book.data.get("cards", []) if book else [],
                        "approved_claims": book.data.get("claims", []) if book else [],
                        "reviewed_examples": examples,
                        "practice_goals": user.data.get("coaching_goals", []),
                        "recent_dialogue": [],
                        "facts": [],
                    },
                    None,
                    expires,
                )

        request, previous, expires = await asyncio.to_thread(context)
        if previous is not None:
            return {"simulation": True, "request_id": identity, **previous}
        provider, route = self.configured_coach(p)
        result = {"status": "unconfigured", "examples": request["approved_cards"]}
        if provider and not self.settings.simulation:

            async def admit(submit):
                with_authority = await asyncio.to_thread(self._practice_authorized, p)
                return with_authority and (
                    not submit or await self._reserve_coach(p, key, key, "coaching_practice")
                )

            async def settle(submitted):
                await self._settle_coach(p, key, key, "coaching_practice", route, provider, submitted)

            result = {"status": "unavailable"}
            try:
                guidance = await self.coach.suggest(
                    key,
                    request,
                    provider,
                    {"action": "NO_ACTION"},
                    deadline_ms=8000,
                    admit=admit,
                    settle=settle,
                    circuit_key=(p["tenant_id"], p["seller_id"], route),
                    owner_key=(p["tenant_id"], p["seller_id"], p["user_id"]),
                )
                ground_guidance(guidance, request)
                if guidance.get("action") == "SHOW_CARD":
                    result = {"status": "ready", "guidance": guidance}
            except CardRejected:
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

    def _practice_authorized(self, p):
        with self.db.transaction(False) as s:
            return self._coach_authority(s, p)

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
        return {
            **result,
            "raw_expires_at": expires.isoformat(),
            "raw_remaining_ms": max(0, int((expires - self.clock()).total_seconds() * 1000)),
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
                return {
                    **content,
                    "pinned": is_pinned,
                    "expires_at": deadline.isoformat(),
                    "remaining_ms": max(0, int((deadline - self.clock()).total_seconds() * 1000)),
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

    def _help_context(self, p, call_id, urgent, question, identity=None):
        started = time.perf_counter()
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            # Context/receipt writes for this call share one short lock. No provider
            # I/O occurs here, and unrelated calls remain independent.
            s.refresh(call, with_for_update=True)
            if not self._help_allowed(s, p, call):
                return None
            lead = self.required(s, "leads", call.data["lead_id"], p)
            if self.suppressed(s, p, lead.data["phone"]):
                return None
            preferences = call.data.get("coach_preferences", {})
            stable = [
                r
                for r in self.transcript_rows(s, p, call_id)
                if r.get("is_final") and not r.get("system_audio")
            ]
            if not stable and not question:
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
            previous = state_record.data if state_record else {}
            seller = self.required(s, "sellers", p["seller_id"], p)
            interval = seller.data.get("operating_policy", {}).get("auto_coach_interval_seconds", 8)
            facts = [
                doc(r)
                for r in self.context_rows(
                    s,
                    p,
                    "research_facts",
                    lead.data.get("domain"),
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
                stable, version=version, facts=facts, commitments=commitments, prior=previous
            )
            if urgent:
                state["trigger"] = "manual_help"
            values = {
                "call_id": call_id,
                **persistent_state(state),
                "privacy_contract": 1,
                "updated_at": self.clock().isoformat(),
            }
            if state_record:
                update(state_record, **values)
            else:
                state_record = put(
                    s,
                    "conversation_states",
                    p["tenant_id"],
                    p["seller_id"],
                    values,
                    key=call_id,
                    now=self.clock(),
                )
            # Observe every stable chunk; coalesce paid work within the configured quiet interval.
            if (
                not urgent
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
                r.data["call_id"]: doc(r)
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
                "recent_dialogue": stable,
                "automatic_tips_muted": bool(preferences.get("muted") and not urgent),
                "manual_help": urgent,
                "private_question": question,
                "approved_cards": all_cards[:32],
                "reviewed_examples": library.context_examples(
                    self, s, p, question or (state.get("last_remote") or {}).get("text", "")
                ),
                "locally_retrieved_card_ids": [c["id"] for c in cards],
                "approved_claims": book.data.get("claims", []) if book else [],
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
        prepared = await asyncio.to_thread(self._help_context, p, call_id, urgent, question, identity)
        if not prepared:
            return {"action": "NO_ACTION", "status": "no_action", "private": bool(question)}
        request, fallback, expires, state_ms, retrieval_ms, comparison = prepared
        remaining = (expires - self.clock()).total_seconds()
        monotonic_deadline = time.monotonic() + remaining
        if remaining <= 0:
            return {"action": "NO_ACTION", "status": "expired", "private": bool(question)}
        provider, route = self.configured_coach(p)
        if (comparison or {}).get("arm") == "control" and not urgent:
            provider = None
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
                )
                progress_id = shown.get("id")
                return bool(progress_id)

            if question:

                async def progress(result):
                    # A cancelled storage thread still runs: retain its identity
                    # before propagating cancellation so finally can withdraw it.
                    return await finish_shielded(publish_progress(result))

                provider.on_progress = progress

            async def admit(submit):
                if not await asyncio.to_thread(self._admit_help, p, call_id, request):
                    return False
                if not submit:
                    return True
                allowed = await self._reserve_coach(p, request_key, call_id, "live_coach")
                if (
                    not allowed
                    or min((expires - self.clock()).total_seconds(), monotonic_deadline - time.monotonic())
                    <= 0
                ):
                    return False
                return await asyncio.to_thread(self._admit_help, p, call_id, request)

            async def settle(submitted):
                await self._settle_coach(p, request_key, call_id, "live_coach", route, provider, submitted)

            async def publish(result):
                nonlocal final_id
                final = await self._validated_help(
                    p,
                    call_id,
                    request,
                    result,
                    route if result is not fallback else source,
                    provider,
                    expires,
                    state_ms,
                    retrieval_ms,
                    (time.perf_counter() - began) * 1000,
                    event_id=progress_id if result is not fallback else None,
                )
                final_id = final.get("id")
                return final

            try:
                result = await self.coach.suggest(
                    call_id,
                    request,
                    provider,
                    fallback,
                    deadline_ms=min(int(remaining * 1000), 8000 if urgent else 1500),
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

    def _admit_help(self, p, call_id, request):
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
            ):
                return False
            update(state, coach_request_key=request["help_request_id"])
            return True

    def _defer_busy_help(self, p, call_id):
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            if call.data["state"] not in TERMINAL | {"ENDING"}:
                due = self.clock() + timedelta(seconds=8)
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
    ):
        try:
            ground_guidance(result, request)
        except CardRejected:
            # An unsafe suggestion need not lose a separately validated interpretation.
            result = {
                "action": "NO_ACTION",
                "state_version": request["state_version"],
                "state_update": result.get("state_update")
                if result.get("state_version") == request["state_version"]
                else None,
            }
        result = {
            **result,
            "private": bool(request.get("private_question")),
            "status": ("streaming" if progressive else "ready")
            if result["action"] != "NO_ACTION"
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
            )
        except CardRejected:
            return {
                "action": "NO_ACTION",
                "status": "no_action",
                "private": bool(request.get("private_question")),
            }

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
            metadata = self.transcript_metadata(s, p, call_id)
            current = {row.data["segment_id"]: row for row in metadata}
            if expires <= self.clock() or any(
                item["segment_id"] not in current
                or current[item["segment_id"]].data["revision"] != item["revision"]
                or self.raw.get(self.raw_key(p, call_id, item["segment_id"])) is None
                for item in request["recent_dialogue"]
            ):
                return {"action": "NO_ACTION", "status": "expired_or_corrected", "private": result["private"]}
            if (
                result.get("state_update") is not None
                and result.get("state_version") == request["state_version"]
            ):
                state = merge_semantic_state(
                    state_record.data, result["state_update"], request["recent_dialogue"]
                )
                state["version"] = call.data["transcript_version"]
                update(
                    state_record,
                    **persistent_state(state),
                    stale=False,
                    privacy_contract=1,
                    updated_at=self.clock().isoformat(),
                )
                for item in request["recent_dialogue"]:
                    row = current[item["segment_id"]]
                    update(
                        row, interpretation_revision=item["revision"], interpreted_at=self.clock().isoformat()
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
                        "latency": {
                            "state_ms": max(0, state_ms - retrieval_ms),
                            "retrieval_ms": retrieval_ms,
                            "state_retrieval_ms": state_ms,
                            "inference_ms": inference_ms,
                            "stt_ms": None,
                        },
                    },
                )
                if event_id:
                    obj = self.required(s, "coach_events", event_id, p)
                    if obj.data["call_id"] != call_id or obj.data.get("user_id") != p["user_id"]:
                        raise CardRejected("Incorrect guidance identity")
                    metadata["created_at"] = obj.data["created_at"]
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
                result = {**result, "id": obj.id}
        return result
