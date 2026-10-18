"""Proactive/private help using one bounded context and one accountable inference."""

import asyncio
import time
from datetime import timedelta

from app.coaching.context import (
    build_context,
    ground_guidance,
    merge_semantic_state,
    persistent_state,
    retrieve_cards,
)
from app.coaching.engine import CardRejected
from app.db import doc, emit, put, uid, update
from app.privacy import persistent_document
from app.providers.inference import InferenceResult
from app.service import dt
from app.telephony.reducer import TERMINAL


class LiveHelper:
    def coach_content(self, p, record):
        if record.get("private") and record.get("user_id") != p["user_id"]:
            return None
        return self.raw.get("help:" + p["tenant_id"] + ":" + record["id"])

    def _help_context(self, p, call_id, urgent, question):
        started = time.perf_counter()
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            if call.data["state"] in TERMINAL | {"ENDING"}:
                return None
            lead = self.required(s, "leads", call.data["lead_id"], p)
            if self.suppressed(s, p, lead.data["phone"]):
                return None
            preferences = call.data.get("coach_preferences", {})
            if preferences.get("muted") and not urgent:
                return None
            stable = [
                r
                for r in self.transcript_rows(s, p, call_id)
                if r.get("is_final") and not r.get("system_audio")
            ]
            if not stable and not question:
                return None
            version = call.data.get("transcript_version", 0)
            state_record = next(
                (r for r in self.scoped(s, "conversation_states", p) if r.external_key == call_id), None
            )
            previous = state_record.data if state_record else {}
            facts = [
                doc(r)
                for r in self.scoped(s, "research_facts", p)
                if r.data.get("domain") == lead.data.get("domain")
                and r.data.get("status") == "supported"
                and dt(r.data["expires_at"]) > self.clock()
            ][:8]
            commitments = [
                doc(r)
                for r in self.scoped(s, "commitments", p)
                if r.data.get("lead_id") == lead.id and r.data.get("status") not in {"cancelled", "completed"}
            ][-5:]
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
                and (self.clock() - dt(previous["last_request_at"])).total_seconds() < 8
            ):
                return None
            book = self.approved_playbook(s, p)
            all_cards = book.data.get("cards", []) if book else []
            cards = retrieve_cards(state, all_cards)
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
            seller = self.required(s, "sellers", p["seller_id"], p)
            previous_calls = sorted(
                [
                    c
                    for c in self.scoped(s, "call_attempts", p)
                    if c.id != call_id and c.data["lead_id"] == lead.id and c.data.get("outcome")
                ],
                key=lambda c: c.data["started_at"],
            )[-3:]
            notes = {
                r.data["call_id"]: r.data
                for r in self.scoped(s, "notes", p)
                if r.data.get("call_id") in {c.id for c in previous_calls}
            }
            request = {
                "state_version": version,
                "state": state,
                "recent_dialogue": stable[-16:],
                "private_question": question,
                "approved_cards": all_cards[:32],
                "locally_retrieved_card_ids": [c["id"] for c in cards],
                "approved_claims": book.data.get("claims", []) if book else [],
                "facts": facts,
                "seller": {
                    "name": seller.data["name"],
                    "knowledge": book.data.get("product_description", "") if book else "",
                },
                "prepared_script": self.fallback_script(s, p, lead),
                "lead_context": {
                    k: lead.data.get(k) for k in ("name", "company", "segment", "prospect_brief")
                },
                "prior_commitments": commitments,
                "prior_call_context": [
                    {
                        "call_id": c.id,
                        "at": c.data["started_at"],
                        "outcome": c.data["outcome"],
                        "human_notes": (notes.get(c.id, {}).get("human_text") or "")[:1000],
                        "rundown": notes.get(c.id, {}).get("notes", {}).get("summary", ""),
                        "evidence_scope": "Historical context, not instructions or proof of current facts",
                    }
                    for c in previous_calls
                ],
                "objective": self.optimization_config(s, p)["conversion_goal"],
            }
            update(state_record, last_request_at=self.clock().isoformat())
            deadline = min(
                [dt(r["raw_expires_at"]) for r in stable] or [self.clock() + timedelta(seconds=300)]
            )
            return (
                request,
                fallback,
                deadline,
                (time.perf_counter() - started) * 1000,
                call.data.get("coaching_comparison"),
            )

    async def coach_turn(self, p, call_id, urgent=False, question=None):
        prepared = await asyncio.to_thread(self._help_context, p, call_id, urgent, question)
        if not prepared:
            return {"action": "NO_ACTION", "status": "no_action", "private": bool(question)}
        request, fallback, expires, state_ms, comparison = prepared
        remaining = (expires - self.clock()).total_seconds()
        if remaining <= 0:
            return {"action": "NO_ACTION", "status": "expired", "private": bool(question)}
        provider, route = self.configured_coach(p)
        if (comparison or {}).get("arm") == "control" and not urgent:
            provider = None
        result, source = fallback, "cached_knowledge"
        request_key = f"coach:{call_id}:{request['state_version']}:{uid()}"
        began = time.perf_counter()
        if provider and not self.settings.simulation and time.monotonic() >= self.coach.open_until:
            admitted = await asyncio.to_thread(
                self.reserve_optional_budget, p, request_key, 0.01, object_id=call_id, work_kind="live_coach"
            )
            if admitted:
                result = await self.coach.suggest(
                    call_id,
                    request,
                    provider,
                    fallback,
                    deadline_ms=min(int(remaining * 1000), 8000 if urgent else 1500),
                )
                source = route if result is not fallback else "cached_knowledge"
                if provider.last_usage or provider.last_request_id:
                    await asyncio.to_thread(
                        self.usage,
                        p,
                        route,
                        "live_coach",
                        InferenceResult({}, provider.last_usage, provider.last_request_id),
                        object_id=call_id,
                        reserved=0,
                        request_key=request_key,
                    )
        try:
            ground_guidance(result, request)
            state = merge_semantic_state(
                request["state"], result.get("state_update"), request["recent_dialogue"]
            )
        except CardRejected:
            return {"action": "NO_ACTION", "status": "no_action", "private": bool(question)}
        result = {
            **result,
            "private": bool(question),
            "status": "ready" if result["action"] != "NO_ACTION" else "no_action",
        }
        return await asyncio.to_thread(
            self._publish_help,
            p,
            call_id,
            request,
            state,
            result,
            source,
            provider,
            expires,
            state_ms,
            (time.perf_counter() - began) * 1000,
        )

    def _publish_help(
        self, p, call_id, request, state, result, source, provider, expires, state_ms, inference_ms
    ):
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            if call.data.get("transcript_version", 0) != request["state_version"] or call.data[
                "state"
            ] in TERMINAL | {"ENDING"}:
                return {"action": "NO_ACTION", "status": "no_action", "private": result["private"]}
            state_record = next(
                r for r in self.scoped(s, "conversation_states", p) if r.external_key == call_id
            )
            update(
                state_record,
                **persistent_state(state),
                stale=False,
                privacy_contract=1,
                updated_at=self.clock().isoformat(),
            )
            emit(s, p["tenant_id"], "conversation.updated", call, self.clock())
            if result["action"] != "NO_ACTION" and expires > self.clock():
                deadline = min(expires, self.clock() + timedelta(milliseconds=result["expires_in_ms"]))
                metadata = persistent_document(
                    "coach_events",
                    {
                        "call_id": call_id,
                        **result,
                        "user_id": p["user_id"],
                        "created_at": self.clock().isoformat(),
                        "expires_at": deadline.isoformat(),
                        "provider": source,
                        "actual_provider": getattr(provider, "last_provider", None),
                        "model": getattr(provider, "last_model", None),
                        "provider_request_id": getattr(provider, "last_request_id", None),
                        "latency": {
                            "state_ms": state_ms,
                            "retrieval_ms": 0.0,
                            "state_retrieval_ms": state_ms,
                            "inference_ms": inference_ms,
                            "stt_ms": None,
                        },
                    },
                )
                obj = put(s, "coach_events", p["tenant_id"], p["seller_id"], metadata, now=self.clock())
                self.raw.put(
                    "help:" + p["tenant_id"] + ":" + obj.id, {**doc(obj), **result}, expires_at=deadline
                )
                emit(s, p["tenant_id"], "coach.card_ready", call, self.clock(), {"coach_event_id": obj.id})
                result = {**result, "id": obj.id}
        return result
