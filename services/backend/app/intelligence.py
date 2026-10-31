import asyncio
import contextlib
import logging
import time
from datetime import timedelta

from sqlalchemy import case, func, select

from app.accounting import RequestAccounting
from app.coaching.workflows import LiveHelper
from app.config import LIMITS, PROVIDER_DEFAULTS, inference_model
from app.db import MODELS, audit, doc, emit, get, put, rows, uid, update
from app.postcall.analysis import build_analysis
from app.preparation.workflows import Preparation
from app.privacy import SpeechPrivacy
from app.providers.inference import InferenceClient
from app.providers.openrouter import OpenRouterCoach
from app.service import Service, dt, fail

# Outcome of one claimed job in a drain pass: completed; requeued with progress
# or newer input; or handled without progress (coalesced, failed, lost lease).
DONE, ADVANCED, IDLE = "completed", "advanced", "idle"

OPTIONAL_REQUEST_ESTIMATE_USD = PROVIDER_DEFAULTS.optional_request_estimate_usd


class Application(Preparation, LiveHelper, SpeechPrivacy, RequestAccounting, Service):
    def inference(self, tenant_id, role, user_id=None):
        conf = self.configs.read(tenant_id)
        if (
            role in {"postcall_extract", "scorecard_review"}
            and not self.settings.simulation
            and not conf.get("postcall_zdr_verified")
        ):
            return None
        mode = conf.get("inference_mode", PROVIDER_DEFAULTS.inference_mode)
        if mode == "chatgpt_plan":
            mode = "codex"  # The separate plan sign-in was folded into the ChatGPT (Codex) login.
        if mode == "codex":
            from app.providers.codex import CodexClient, profile_home

            if not user_id:
                return None
            home = profile_home(self.settings.data_dir, user_id)
            if not (home / "auth.json").is_file():
                return None
            return CodexClient(home, inference_model(conf, role), "submit_" + role)
        token = conf.get("openai_api_key")
        if not token and mode != "litellm":
            return None
        if mode == "litellm" and not conf.get("inference_base_url"):
            return None
        return InferenceClient(
            mode,
            token or "",
            inference_model(conf, role),
            base_url=conf.get("inference_base_url") or None,
            http=getattr(self, "http", None),
            tool_name="submit_" + role,
        )

    def approved_playbook(self, s, p):
        model = MODELS["playbooks"]
        return s.scalar(
            select(model)
            .where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.ctx_key == "approved",
            )
            .order_by(model.version.desc(), model.created_at.desc(), model.id.desc())
            .limit(1)
        )

    def packet(self, p, lead_id):
        with self.db.transaction(False) as s:
            lead = self.required(s, "leads", lead_id, p)
            fact_model = MODELS["research_facts"]
            facts = [
                doc(r)
                for r in self.context_rows(
                    s,
                    p,
                    "research_facts",
                    lead.id,
                    fact_model.ctx_status == "supported",
                    fact_model.ctx_expires_at > self.clock(),
                    limit=32,
                )
            ]
            reservations = self.lead_records(
                s,
                p,
                "reservations",
                lead.id,
                MODELS["reservations"].op_owner_id == p["user_id"],
                limit=1,
                newest=True,
            )
            assignment = reservations[0].data.get("script_comparison") if reservations else None
            script, preparation = self.selected_script(s, p, lead, assignment=assignment)
            call_model = MODELS["call_attempts"]
            active_calls = self.lead_records(
                s,
                p,
                "call_attempts",
                lead.id,
                call_model.op_status.not_in(["ENDED", "FINALIZED", "FAILED"]),
                limit=1,
                newest=True,
            )
            if active_calls and active_calls[0].data.get("script_snapshot"):
                script = dict(active_calls[0].data["script_snapshot"])
            decision = None
            owner = next((c for c in active_calls if c.data.get("rep_id") == p["user_id"]), None)
            origin = "frozen_call_decision"
            if not owner and reservations:
                candidate = reservations[0]
                if (
                    candidate.data.get("status") == "reserved"
                    and dt(candidate.data["expires_at"]) > self.clock()
                ):
                    owner, origin = candidate, "frozen_reservation_decision"
            if owner and owner.data.get("feature_snapshot_id"):
                row = self.required(s, "feature_snapshots", owner.data["feature_snapshot_id"], p)
                if row.data.get("lead_id") == lead.id and row.data.get("predictions"):
                    decision = {**doc(row), "source": origin}
            facts = list({f["id"]: f for f in [*facts, *self.script_evidence(s, p, lead, script)]}.values())
            source_model = MODELS["research_sources"]
            sources = [
                doc(r)
                for r in s.scalars(
                    select(source_model).where(
                        source_model.tenant_id == p["tenant_id"],
                        source_model.seller_id == p["seller_id"],
                        source_model.id.in_([f.get("source_id") for f in facts]),
                    )
                )
            ]
            binding = self._by_key(s, p, "lead_number_bindings", lead.id)
            calls = [
                doc(r)
                for r in reversed(self.lead_records(s, p, "call_attempts", lead.id, limit=20, newest=True))
            ]
            history = {}
            for kind in ("commitments", "followup_tasks"):
                model = MODELS[kind]
                visible = (model.op_status != "aliased") | model.op_status.is_(None)
                history[kind] = [
                    doc(r)
                    for r in s.scalars(
                        select(model)
                        .where(
                            model.tenant_id == p["tenant_id"],
                            model.seller_id == p["seller_id"],
                            model.op_lead_id == lead.id,
                            visible,
                        )
                        .order_by(
                            case((model.op_status.in_(["completed", "cancelled", "retracted"]), 1), else_=0),
                            model.op_at.desc(),
                            model.id.desc(),
                        )
                        .limit(20)
                    )
                ]
            commitments = history["commitments"]
            totals = {}
            for kind in ("call_attempts", "commitments", "followup_tasks"):
                model = MODELS[kind]
                totals[kind] = s.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(
                        model.tenant_id == p["tenant_id"],
                        model.seller_id == p["seller_id"],
                        model.op_lead_id == lead.id,
                        (model.op_status != "aliased") | model.op_status.is_(None),
                    )
                )
            return {
                **self.prospect_context_in(
                    s, p, lead, calls, commitments, history["followup_tasks"], decision=decision
                ),
                "lead": {**doc(lead), "suppressed": self.suppressed(s, p, lead.data["phone"])},
                "script": script,
                "preparation": preparation,
                "research": {"facts": facts, "sources": sources, "fact_limit": 64},
                "bound_number_id": binding.data["number_id"] if binding else None,
                "previous_calls": calls,
                "commitments": commitments,
                "context_coverage": {
                    "calls_total": totals["call_attempts"],
                    "calls_omitted": max(0, totals["call_attempts"] - len(calls)),
                    "commitments_total": totals["commitments"],
                    "commitments_omitted": max(0, totals["commitments"] - len(commitments)),
                    "tasks_total": totals["followup_tasks"],
                    "tasks_omitted": max(0, totals["followup_tasks"] - len(history["followup_tasks"])),
                    "scope": "Most recent 20 calls and up to 20 commitments/tasks, with unresolved work first. Full records remain retained.",
                },
            }

    def configured_coach(self, p):
        if self.settings.simulation:
            return None, "approved_static"
        conf = self.configs.read(p["tenant_id"])
        provider = conf.get("coach_provider", PROVIDER_DEFAULTS.coach_provider)
        if provider == "openrouter" and conf.get("openrouter_api_key"):
            return OpenRouterCoach(
                conf["openrouter_api_key"],
                conf.get("coach_model", PROVIDER_DEFAULTS.coach_model),
                http=getattr(self, "http", None),
            ), "openrouter"
        return None, "approved_static"

    def conversation_view_in(self, s, p, call, memory=None):
        from app.coaching.context import state_view, upgrade_state
        from app.postcall.rubrics import effective

        memory = memory or self._by_key(s, p, "conversation_states", call.id)
        lineage = self._conversation_lineage(s, p, call.id)
        derived = state_view(
            upgrade_state(memory.data if memory else {}, call_id=call.id, rubric=effective(call.data)),
            lineage=lineage,
        )
        generated = build_analysis(
            call.id,
            [],
            outcome=call.data.get("outcome"),
            derived=derived,
            transcript_version=call.data["transcript_version"],
            rubric=effective(call.data),
            lineage=lineage,
        )
        return derived, generated

    def note_view_in(self, s, p, note, call=None):
        """Read compatibility view; new derived narrative is owned only by the conversation."""
        call = call or self.required(s, "call_attempts", note.data["call_id"], p)
        memory = self._by_key(s, p, "conversation_states", call.id)
        if not memory or memory.data.get("schema_version") != 2:
            return doc(note)
        derived, generated = self.conversation_view_in(s, p, call, memory)
        return {
            **doc(note),
            "call_memory": derived,
            **{k: generated[k] for k in ("notes", "observations", "semantic_trackers", "grounding_status")},
        }

    def _materialize_views_in(self, s, p, call, *, notes=True, scorecard=True, finalized=False):
        """Write-side note/scorecard metadata for the call's current derived view.

        Reads never create, repair or demote these rows. A writer that needs their
        identity or version token (finalization, a human note, a score review, a purpose
        change) materializes them inside its own transaction. The narrative itself lives
        once, in the conversation state, so stored scorecard items carry no explanation.
        """
        derived, generated = self.conversation_view_in(s, p, call)
        revision = derived["version"]
        if finalized:
            from app.optimization.script_learning import script_trace

            trace = script_trace(
                call.data.get("script_snapshot") or {},
                [r.data for r in self.transcript_metadata(s, p, call.id)],
                derived.get("observations", []),
            )
            if call.data.get("script_trace") != trace:
                update(call, script_trace=trace)
        note = self._by_key(s, p, "notes", call.id) if notes else None
        card = self._by_key(s, p, "scorecards", call.id) if scorecard else None
        if notes:
            human = note.data.get("human_text") if note else None
            values = {
                "notes": {k: generated["notes"][k] for k in ("call_id", "transcript_version", "outcome")},
                "privacy_contract": 1,
                "stale": False,
                "grounding_status": generated["grounding_status"],
                "derived_state_revision": revision,
                "status": "human_confirmed"
                if human is not None
                else "ai_finalized"
                if finalized
                else note.data.get("status", "draft")
                if note
                else "draft",
            }
            if not note:
                note = put(
                    s,
                    "notes",
                    p["tenant_id"],
                    p["seller_id"],
                    {"call_id": call.id, "human_text": None, "suggested_update": None, **values},
                    key=call.id,
                    now=self.clock(),
                )
            elif any(note.data.get(k) != v for k, v in values.items()):
                drifted = any(note.data.get(k) != values[k] for k in ("notes", "derived_state_revision"))
                update(note, **values, **({"suggested_update": human is not None} if drifted else {}))
        if scorecard:
            values = {
                **generated["scorecard"],
                "items": [
                    {k: v for k, v in item.items() if k != "explanation"}
                    for item in generated["scorecard"]["items"]
                ],
                "derived_state_revision": revision,
                "privacy_contract": 1,
                "stale": False,
            }
            if not card:
                card = put(
                    s,
                    "scorecards",
                    p["tenant_id"],
                    p["seller_id"],
                    {"call_id": call.id, **values},
                    key=call.id,
                    now=self.clock(),
                )
            elif any(card.data.get(k) != v for k, v in values.items()):
                update(card, **values)
        return note, card

    def analysis(self, p, call_id):
        """Saved views plus current TTL evidence. A read never runs a model or writes."""
        from app.optimization.script_learning import script_trace
        from app.postcall.analysis import behavioral_metrics, reviewed_tracker_matches
        from app.postcall.review import reviewed_view

        with self.db.transaction(False) as s:
            call = self.required(s, "call_attempts", call_id, p)
            metadata = self.transcript_metadata(s, p, call_id)
            segments = self.transcript_rows(s, p, call_id, metadata=metadata)
            memory = self._by_key(s, p, "conversation_states", call_id)
            derived, generated = self.conversation_view_in(s, p, call, memory)
            revision = derived["version"]
            note = self._by_key(s, p, "notes", call_id)
            card = self._by_key(s, p, "scorecards", call_id)
            human = note.data.get("human_text") if note else None
            # Drift is reported for the human to accept; the next writer records it.
            drifted = note is not None and (
                note.data.get("derived_state_revision", 0) != revision
                or note.data.get("notes", {}).get("transcript_version") != call.data["transcript_version"]
            )
            scorecard_id = card.id if card else call.id
            reviews = [doc(r) for r in self.context_rows(s, p, "reviews", scorecard_id, limit=None)]
            comments = [doc(r) for r in self.context_rows(s, p, "comments", call_id, limit=None)]
            trackers = [doc(r) for r in self.scoped(s, "trackers", p) if r.data["kind"] == "semantic"]
            revisions = {r.data["segment_id"]: r.data.get("revision", 1) for r in metadata}
            stable_metadata = [
                r.data for r in metadata if r.data.get("is_final") and not r.data.get("system_audio")
            ]
            live_ids = {item["segment_id"] for item in segments}
            interpreted = sum(
                item.get("interpretation_revision") == item.get("revision", 1) for item in stable_metadata
            )
            pending = sum(
                item["segment_id"] in live_ids
                and item.get("interpretation_revision") != item.get("revision", 1)
                for item in stable_metadata
            )
            reviewed_card, reviews = reviewed_view(generated["scorecard"], reviews, revisions, live_ids)
            return {
                "call": doc(call),
                "lead_timezone": self.required(s, "leads", call.data["lead_id"], p).data.get("timezone"),
                "notes": generated["notes"],
                "call_memory": derived,
                "derived_state_revision": revision,
                "finalization": derived["finalization"],
                "source_conflicts": derived["source_conflicts"],
                "interpretation_coverage": {
                    "total_segments": len(stable_metadata),
                    "interpreted_segments": interpreted,
                    "pending_segments": pending,
                    "unavailable_segments": len(stable_metadata) - interpreted - pending,
                    "rate_limited_opportunities": (memory.data if memory else {}).get(
                        "rate_limited_opportunities", 0
                    ),
                },
                "notes_version": note.version if note else 0,
                "notes_edit_version": note.data.get("human_edit_version", 0) if note else 0,
                "human_notes": human,
                "suggested_update": generated["notes"]
                if human is not None and (note.data.get("suggested_update") or drifted)
                else None,
                "status": "human_confirmed"
                if human is not None
                else note.data.get("status", "draft")
                if note
                else "draft",
                "grounding_status": generated["grounding_status"],
                "observations": generated["observations"],
                # Until a writer materializes it, a call's scorecard is addressed by its call.
                "scorecard_id": scorecard_id,
                "scorecard_version": card.version if card else 0,
                "scorecard": generated["scorecard"],
                "reviewed_scorecard": reviewed_card,
                "reviews": reviews,
                "transcript": segments,
                "metrics": behavioral_metrics(stable_metadata),
                "script_trace": call.data.get("script_trace")
                or script_trace(
                    call.data.get("script_snapshot") or {},
                    [r.data for r in metadata],
                    derived.get("observations", []),
                ),
                "comments": comments,
                "recording_available": False,
                "raw_evidence_status": self.raw_status(call, segments),
                "provider": note.data.get("provider", generated["provider"])
                if note
                else generated["provider"],
                "tracker_definitions": trackers[:20],
                "tracker_definitions_omitted": max(0, len(trackers) - 20),
                "trackers": self.tracker_matches(s, p, segments)
                + reviewed_tracker_matches(generated, trackers, revisions, live_ids),
            }

    def edit_notes(self, p, call_id, expected_version, text, *, expected_edit_version=None):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            call = self.required(s, "call_attempts", call_id, p)
            s.refresh(call, with_for_update=True)
            if not self._coach_authority(s, p, call):
                fail(403, "Current account or session cannot edit these notes")
            note = self._by_key(s, p, "notes", call_id)
            edits = note.data.get("human_edit_version", 0) if note else 0
            if expected_edit_version is not None:
                # Only another human save conflicts; finalization re-materializes derived fields.
                if edits != expected_edit_version:
                    fail(409, {"code": "NOTES_VERSION_CONFLICT", "current_edit_version": edits})
            elif (note.version if note else 0) != expected_version:
                fail(409, {"code": "NOTES_VERSION_CONFLICT", "current_version": note.version if note else 0})
            lead = self.required(s, "leads", call.data["lead_id"], p)
            s.refresh(lead, with_for_update=True)
            note, _ = self._materialize_views_in(s, p, call, scorecard=False)
            # The human has just read the current machine view.
            update(
                note,
                human_text=text,
                status="human_confirmed",
                editor_id=p["user_id"],
                suggested_update=False,
                human_edit_version=edits + 1,
            )
            self.refresh_packet_in(s, p, lead)
            audit(s, p["tenant_id"], p["seller_id"], p["user_id"], "notes.edited", note.id, self.clock())
            return {"notes_version": note.version, "notes_edit_version": edits + 1, "human_notes": text}

    async def run_jobs(self, tenant_id=None, queues=None):
        from app.jobs.queue import LostJobLease
        from app.postcall.finalize import KIND as FINALIZE_KIND
        from app.postcall.finalize import UnknownJobKind

        if (queues is None or "ml" in queues) and self.settings.worker_enabled:
            await self.periodic_optimization(tenant_id)
        worker_id = "worker-" + uid()
        errors = []

        async def requeue(job, receipt):
            # A page that processed rows or advanced its cursor is due again now;
            # superseded or waiting refreshes back off in the queue instead.
            receipt, cursor = receipt or {}, job["payload"].get("cursor")
            paged = receipt.get("status") == "pending" and bool(
                receipt.get("processed") or receipt.get("cursor", cursor) != cursor
            )
            renewed = await asyncio.to_thread(
                self.jobs.coalesce_refresh, job["id"], worker_id, progressed=paged
            )
            # Newer input that arrived during the run is fresh work, not a retry.
            return ADVANCED if paged or renewed else IDLE

        async def execute(lane, job):
            p = {
                "tenant_id": job["tenant_id"],
                "seller_id": job["payload"].get("seller_id"),
                "user_id": job["payload"].get("user_id"),
                "role": "admin",
                "campaign_id": job["payload"].get("campaign_id"),
            }

            async def heartbeat():
                # Lease loss or a failed renewal belongs to this job alone: its own
                # finish observes a lost lease, and siblings in the wave keep running.
                while True:
                    await asyncio.sleep(20)
                    try:
                        await asyncio.to_thread(self.jobs.heartbeat, job["id"], worker_id, 60)
                    except LostJobLease:
                        return
                    except Exception as exc:
                        logging.getLogger(__name__).warning("Job heartbeat deferred: %s", type(exc).__name__)

            pulse = asyncio.create_task(heartbeat())
            kind = job["payload"].get("kind")
            try:
                if lane == "webhook":
                    receipt = await self.process_webhook(job["tenant_id"], job["payload"]["receipt_id"])
                    if receipt["status"] in {"processing", "retry", "unmatched"}:
                        raise RuntimeError("Webhook processing is incomplete")
                elif lane in {"research", "scripts"}:
                    if kind == "packet_refresh":
                        # Lead recomputation moved out of the rep's save into this refresh.
                        await asyncio.to_thread(self.refresh_continuity, p, job["payload"]["lead_ids"])
                    elif kind == "prep_thread":
                        await self.run_prep_thread(p, job["payload"]["lead_ids"], job["id"])
                        await asyncio.to_thread(self.prepare_tick, p, job["id"])
                    else:
                        raise UnknownJobKind(f"No {lane} handler for job kind {kind!r}")
                elif lane == "postcall":
                    if kind == "conversation_review":
                        await self.review_conversation(p, job["payload"]["call_id"])
                    elif kind in {FINALIZE_KIND, None}:
                        # None: persisted legacy jobs and producers not yet on enqueue_finalize.
                        await self.analyze_postcall(p, job["payload"]["call_id"])
                    else:
                        raise UnknownJobKind(f"No postcall handler for job kind {kind!r}")
                elif lane == "live_coach":
                    await self.coach_turn(p, job["payload"]["call_id"])
                elif lane == "ml":
                    if job["payload"].get("kind") == "forecast_refresh":
                        receipt = await asyncio.to_thread(
                            self.build_forecast,
                            p,
                            job["payload"]["campaign_id"],
                            job["payload"].get("capacity"),
                            lease=(job["id"], worker_id),
                            list_name=job["payload"].get("list_name"),
                        )
                        if receipt["status"] in {"superseded", "pending"}:
                            return await requeue(job, receipt)
                    elif job["payload"].get("kind") == "home_refresh":
                        await asyncio.to_thread(
                            self.rebuild_home_metrics,
                            {**p, "role": job["payload"].get("role", "rep")},
                            lease=(job["id"], worker_id),
                        )
                    elif job["payload"].get("kind") == "report_refresh":
                        await asyncio.to_thread(
                            self.rebuild_optimization_report,
                            {**p, "role": job["payload"].get("role", "rep")},
                            job["payload"].get("epoch"),
                            lease=(job["id"], worker_id),
                        )
                    elif job["payload"].get("kind") == "optimization_refresh":
                        await asyncio.to_thread(
                            self.refresh_optimization_job, p, lease=(job["id"], worker_id)
                        )
                    elif job["payload"].get("kind") in {"health_refresh", "learning_refresh"}:
                        method = (
                            self.refresh_observed_health
                            if job["payload"]["kind"] == "health_refresh"
                            else self.refresh_learning
                        )
                        receipt = await asyncio.to_thread(method, p, lease=(job["id"], worker_id))
                        if receipt["status"] in {"superseded", "pending"}:
                            return await requeue(job, receipt)
                        if job["payload"]["kind"] == "learning_refresh":
                            try:
                                await self.propose_from_memory(p)
                            except Exception as exc:  # A proposal is optional; the refresh stands.
                                logging.getLogger(__name__).warning(
                                    "Script proposal deferred: %s", type(exc).__name__
                                )
                    elif kind is None:  # Operational training snapshots carry no kind.
                        await self.train_model_job(p, job["payload"])
                    else:
                        raise UnknownJobKind(f"No ml handler for job kind {kind!r}")
                else:
                    raise ValueError("Unknown work lane")
                await asyncio.to_thread(self.jobs.complete, job["id"], worker_id)
                return DONE
            except LostJobLease:
                return IDLE
            except Exception as exc:
                errors.append(
                    {"kind": "job", "job_id": job["id"], "queue": lane, "error_type": type(exc).__name__}
                )
                with contextlib.suppress(LostJobLease):
                    await asyncio.to_thread(self.jobs.fail, job["id"], worker_id, type(exc).__name__)
                return IDLE
            finally:
                pulse.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pulse

        # Carrier events apply in arrival order (an answer before its hangup): one webhook at a time.
        capacity = {
            "webhook": 1,
            "live_coach": LIMITS.live_active,
            "postcall": LIMITS.postcall_active,
        }
        capacity.update(research=LIMITS.company_units, scripts=LIMITS.company_units, ml=2)
        processed = 0

        async def drain(lane):
            """Claim and run waves of due jobs until the lane is idle or the pass bound is reached."""
            nonlocal processed
            pool = self.job_admission["preparation" if lane in {"research", "scripts"} else lane]
            seen = set()
            for _ in range(20):
                jobs = await asyncio.to_thread(
                    self.jobs.claim_many, lane, worker_id, capacity[lane], 60, tenant_id
                )
                if not jobs:
                    return True

                async def run(job):
                    async with pool:
                        return await execute(lane, job)

                outcomes = await asyncio.gather(*(run(job) for job in jobs))
                processed += outcomes.count(DONE)
                if all(o == IDLE and j["id"] in seen for j, o in zip(jobs, outcomes, strict=True)):
                    return False
                seen.update(j["id"] for j in jobs)
            return False

        lanes = list(queues or ["webhook", "live_coach", "postcall", "scripts", "research", "ml"])
        idle = await asyncio.gather(*(drain(lane) for lane in lanes))
        if processed and len(lanes) > 1:
            # A sibling's commits can make work due after a lane found nothing; run it in this call.
            await asyncio.gather(*(drain(lane) for lane, empty in zip(lanes, idle, strict=True) if empty))
        return {"processed": processed, "status": "failed" if errors else "passed", "errors": errors}

    def _training_source(self, s, p, payload, *, rows=True):
        """The named immutable dataset, re-verified by content; the job carries no rows.

        Publication passes rows=False: an unchanged row version is the same content,
        so the write transaction neither decrypts nor rehashes the snapshot."""
        from app.optimization.workflows import dataset_checksum

        user = self.required(s, "users", p["user_id"], p)
        if user.data.get("disabled") or user.data.get("role") not in {"owner", "admin"}:
            fail(403, "Current administrator authority is required for training")
        config = self.optimization_config(s, p)
        changed = config["conversion_goal"] != payload.get("goal") or (
            payload.get("window_days") is not None and config["window_days"] != payload["window_days"]
        )
        changed |= payload.get("baseline") is not None and payload["baseline"] != {
            k: config[k] for k in ("estimate_policy", "handling_seconds")
        }
        if rows:
            dataset = self.required(s, "learning_datasets", payload.get("source_dataset_id"), p)
            identity, version, data = dataset.id, dataset.version, dataset.data
            changed |= (
                data.get("source") != "immutable_operational_feature_snapshots"
                or data.get("checksum") != payload.get("input_fingerprint")
                or dataset_checksum(data) != payload.get("input_fingerprint")
                or data.get("goal") != payload.get("goal")
                or data.get("baseline") != payload.get("baseline")
                or (
                    payload.get("window_days") is not None
                    and data.get("window_days") != payload["window_days"]
                )
            )
        else:
            model = MODELS["learning_datasets"]
            identity, data = payload.get("source_dataset_id"), {}
            version = s.scalar(
                select(model.version).where(
                    model.id == identity, model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"]
                )
            )
            if version is None:
                fail(404, "learning_datasets not found")
        if changed:
            fail(409, "Training source or goal changed; prepare the current owned dataset")
        prior = self._by_key(s, p, "model_versions", "training:" + identity)
        return version, doc(prior) if prior else None, data.get("rows")

    def _training_snapshot(self, p, payload):
        with self.db.transaction(False) as s:
            return self._training_source(s, p, payload)

    def _training_publish(self, p, payload, version, result):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            current, prior, _ = self._training_source(s, p, payload, rows=False)
            if current != version:
                fail(409, "Training dataset changed during fitting")
            if prior:
                return False
            put(
                s,
                "model_versions",
                p["tenant_id"],
                p["seller_id"],
                {
                    **result,
                    "active": False,
                    "source_dataset_id": payload.get("source_dataset_id"),
                    "goal": payload.get("goal", "meeting_held"),
                    "window_days": payload.get("window_days"),
                },
                key="training:" + payload["source_dataset_id"],
                now=self.clock(),
            )
        return True

    async def train_model_job(self, p, payload):
        import shutil

        from app.ml.worker import train_isolated

        version, prior, rows = await asyncio.to_thread(self._training_snapshot, p, payload)
        if prior:
            return
        directory = self.settings.data_dir / "models" / uid()
        published = False
        try:
            result = await train_isolated(rows, directory, baseline=payload.get("baseline"))
            publication = asyncio.create_task(
                asyncio.to_thread(self._training_publish, p, payload, version, result)
            )
            try:
                published = await asyncio.shield(publication)
            except asyncio.CancelledError:
                # A cancelled await cannot roll back an already-running commit.
                published = await publication
                raise
        finally:
            if not published:
                await asyncio.to_thread(shutil.rmtree, directory, True)

    async def periodic_optimization(self, tenant_id=None):
        now = time.monotonic()
        if now < getattr(self, "_next_optimization", 0):
            return
        self._next_optimization = now + 60
        try:
            await asyncio.to_thread(self.schedule_optimization, tenant_id)
        except Exception:
            self._next_optimization = 0
            raise

    async def maintenance_tick(self):
        await self.periodic_optimization()
        self.raw.purge()
        await asyncio.to_thread(self.queue_unfinished_webhooks)
        prepare_due = time.monotonic() >= getattr(self, "_next_preparation", 0)
        if prepare_due:
            self._next_preparation = time.monotonic() + 60
        prepared_sellers = set()
        errors = []
        completed = 0

        def principals():
            """Seller-scoped recovery runs once per seller under its active administrator.

            A seller without one keeps the per-user passes, because a rep's own
            authority only covers that rep's calls.
            """
            sellers = {}
            with self.db.transaction(False) as session:
                for r in rows(session, "users"):
                    if not r.data.get("disabled"):
                        sellers.setdefault((r.tenant_id, r.seller_id), []).append(
                            {
                                "tenant_id": r.tenant_id,
                                "seller_id": r.seller_id,
                                "user_id": r.id,
                                "role": r.data["role"],
                            }
                        )
            for users in sellers.values():
                administrator = next((u for u in users if u["role"] in {"owner", "admin"}), None)
                yield from [administrator] if administrator else users

        for p in await asyncio.to_thread(lambda: list(principals())):
            try:
                if prepare_due and p["seller_id"] not in prepared_sellers:
                    prepared_sellers.add(p["seller_id"])
                    await asyncio.to_thread(self.prepare_tick, p)
                # Recovery ends with the seller's control dispatch; one pass per tick.
                await self.recover(p)
                completed += 1
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Maintenance reconciliation deferred: %s", type(exc).__name__
                )
                errors.append(
                    {"kind": "maintenance", "user_id": p["user_id"], "error_type": type(exc).__name__}
                )
        return {"status": "failed" if errors else "passed", "completed": completed, "errors": errors}

    def tracker_matches(self, s, p, segments):
        matches = []
        for tracker in self.scoped(s, "trackers", p):
            if tracker.data["kind"] != "keyword":
                continue
            evidence = [
                segment["segment_id"]
                for segment in segments
                if segment.get("is_final")
                and not segment.get("system_audio")
                and any(
                    phrase.casefold() in segment["text"].casefold()
                    for phrase in tracker.data.get("phrases", [])
                )
            ]
            if evidence:
                matches.append(
                    {
                        "tracker_id": tracker.id,
                        "name": tracker.data["name"],
                        "version": tracker.version,
                        "kind": "keyword",
                        "evidence": evidence,
                    }
                )
        return matches

    def _conversation_signature(self, s, p, call, state):
        from app.coaching.sources import digest

        note = self._by_key(s, p, "notes", call.id)
        book = self.approved_playbook(s, p)
        return digest(
            {
                "conversation": state["version"],
                "transcript": call.data["transcript_version"],
                "outcome": call.data.get("outcome"),
                "purpose": call.data.get("rubric_revision"),
                "human_note": note.data.get("human_text") if note else None,
                "knowledge": [book.id, book.version] if book else None,
            }
        )

    def enqueue_finalize(self, s, p, call, reason):
        """The single conversation_finalize enqueue, inside the caller's transaction.

        `call` is the locked call row; `reason` names the trigger (hangup, webhook,
        correction, wrap-up, purpose change). Identical input is one job.
        """
        from app.postcall.finalize import enqueue

        return enqueue(self, s, p, call, reason)

    def _carry_finalization(self, s, p, call, state, before):
        """A review edit of an owned or completed finalization carries its fingerprints."""
        from app.postcall.finalize import status_in

        final, after = state["finalization"], self._conversation_signature(s, p, call, state)
        if final.get("input_fingerprint") == before:
            final["input_fingerprint"] = after
        if final.get("completed_fingerprint") != before:
            return False
        status, coverage = status_in(self, s, p, call, state, True)
        state["coverage"].update(coverage)
        final.update(status=status, completed_fingerprint=after)
        return True

    def _begin_finalization(self, p, call_id):
        from app.coaching.context import upgrade_state
        from app.postcall.rubrics import effective

        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            s.refresh(call, with_for_update=True)
            if not self._coach_authority(s, p, call):
                return None
            record = self._by_key(s, p, "conversation_states", call_id)
            state = upgrade_state(record.data if record else {}, call_id=call_id, rubric=effective(call.data))
            signature = self._conversation_signature(s, p, call, state)
            final = state["finalization"]
            if final.get("completed_fingerprint") == signature:
                return None
            if final.get("owner") and dt(final["lease_expires_at"]) > self.clock():
                return None
            identity = uid()
            state["finalization"] = {
                **final,
                "status": "pending",
                "owner": identity,
                "lease_expires_at": (self.clock() + timedelta(seconds=30)).isoformat(),
                "input_fingerprint": signature,
                "last_request_id": identity,
            }
            self._store_conversation(s, p, call, state, record)
            return identity

    def _conversation_current_in(self, s, p, call, request, *, owner=None):
        """The same source fence runs before submission and inside the publication commit."""
        from app.coaching import sources

        record = self._by_key(s, p, "conversation_states", call.id)
        if not record or not self._coach_authority(s, p, call):
            return False
        if call.data.get("transcription_disabled") and request["recent_dialogue"]:
            return False
        if owner and (
            record.data["finalization"].get("owner") != owner
            or dt(record.data["finalization"]["lease_expires_at"]) <= self.clock()
        ):
            return False
        if call.data.get("rubric_revision") != request.get("rubric_revision") or call.data.get(
            "outcome"
        ) != request.get("call_outcome"):
            return False
        note = self._by_key(s, p, "notes", call.id)
        if sources.digest(note.data.get("human_text") if note else None) != request["human_note_hash"]:
            return False
        if not sources.current(self, s, p, request["context_sources"]):
            return False
        observations = {i["id"]: i for i in record.data.get("observations", [])}
        for expected in request.get("evidence_catalog", []):
            if expected["kind"] != "observation":
                continue
            current = observations.get(expected["id"])
            if (
                not current
                or current["version"] != expected["version"]
                or current["state"] != expected["status"]
                or current["epistemic_status"] != expected["epistemic_status"]
                or current.get("review_status") in {"unavailable", "rejected"}
                or current.get("invalidated_at")
            ):
                return False
        for expected in request.get("trackers", []):
            row = get(s, "trackers", expected["id"], p["tenant_id"])
            if not row or row.seller_id != p["seller_id"] or row.version != expected["version"]:
                return False
        metadata = {r.data["segment_id"]: r.data for r in self.transcript_metadata(s, p, call.id)}
        return all(
            i["segment_id"] in metadata
            and metadata[i["segment_id"]]["revision"] == i["revision"]
            and dt(i["raw_expires_at"]) > self.clock()
            and self.raw.get(self.raw_key(p, call.id, i["segment_id"])) is not None
            for i in request["recent_dialogue"]
        )

    def _conversation_request_current(self, p, call_id, request, *, owner=None):
        with self.db.transaction(False) as s:
            call = self.required(s, "call_attempts", call_id, p)
            return self._conversation_current_in(s, p, call, request, owner=owner)

    def _conversation_context(self, p, call_id, mode):
        """Read-only finalize/review input; the live helper's writer is never used here."""
        from app.postcall.finalize import request

        return request(self, p, call_id, mode)

    async def _conversation_infer(
        self, p, call_id, request, provider, route, *, method, kind, current, raw_sources=None
    ):
        from app.coaching.engine import finish_shielded

        key = f"{kind}:{call_id}:{uid()}"
        expiry = min(
            [
                dt(r["raw_expires_at"])
                for r in (raw_sources if raw_sources is not None else request.get("recent_dialogue", []))
            ]
            or [self.clock() + timedelta(seconds=30)]
        )
        deadline = time.monotonic() + min(
            LIMITS.manual_full_ms / 1000,
            max(0, (expiry - self.clock()).total_seconds()),
        )
        submitted = False
        try:
            if not provider or time.monotonic() >= deadline or not await asyncio.to_thread(current):
                raise ValueError("Conversation inference is unavailable")
            if not await self._reserve_coach(p, key, call_id, kind):
                raise ValueError("Conversation inference budget unavailable")
            if not await asyncio.to_thread(current) or expiry <= self.clock() or time.monotonic() >= deadline:
                raise ValueError("Conversation sources changed before submission")
            provider.last_usage, provider.last_request_id = {}, None
            submitted = True
            remaining = min(deadline - time.monotonic(), (expiry - self.clock()).total_seconds())
            result = await asyncio.wait_for(
                getattr(provider, method)(request, deadline_ms=max(1, int(remaining * 1000))), remaining
            )
            if (
                time.monotonic() >= deadline
                or expiry <= self.clock()
                or not await asyncio.to_thread(current)
                or str(getattr(provider, "last_provider", "")).casefold().split("/")[0] != "cerebras"
            ):
                raise ValueError("Conversation response is stale or the route is unverified")
            return result
        finally:
            await finish_shielded(self._settle_coach(p, key, call_id, kind, route, provider, submitted))

    def _merge_finalization(self, p, call_id, owner, request, result):
        from app.coaching.context import merge_semantic_state, normalized_turn
        from app.coaching.contracts import card_view

        result = normalized_turn(card_view(result), request)
        if result.get("state_version") != request["state_version"]:
            raise ValueError("Finalizer returned a different transcript version")
        if (
            result.get("context_fingerprint", request["context_fingerprint"])
            != request["context_fingerprint"]
        ):
            raise ValueError("Finalizer returned a different context fingerprint")
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            s.refresh(call, with_for_update=True)
            row = self._by_key(s, p, "conversation_states", call_id)
            if not self._conversation_current_in(s, p, call, request, owner=owner):
                return False
            if (
                call.data["transcript_version"] != request["state_version"]
                or self._conversation_signature(s, p, call, row.data)
                != row.data["finalization"]["input_fingerprint"]
            ):
                return False
            state = merge_semantic_state(
                {**row.data, "_lineage": self._conversation_lineage(s, p, call_id)},
                result.get("state_update"),
                request["recent_dialogue"],
                request_id=owner,
                at=self.clock(),
                hash_sources=True,
                catalog=request["evidence_catalog"],
                call_at=dt(request["call_at"]),
                timezone=request["prospect_timezone"],
                exposures=request.get("recent_exposures", []),
            )
            state["finalization"]["input_fingerprint"] = self._conversation_signature(s, p, call, state)
            self._store_conversation(s, p, call, state, row)
            interpreted = {i["id"]: i["version"] for i in state.get("_interpreted_segments", [])}
            for segment in self.transcript_metadata(s, p, call_id):
                if interpreted.get(segment.data["segment_id"]) == segment.data["revision"]:
                    update(
                        segment,
                        interpretation_revision=segment.data["revision"],
                        interpreted_at=self.clock().isoformat(),
                    )
            self._queue_conversation_review(s, p, call, state)
            return True

    def _finish_finalization(self, p, call_id, owner, success, offered=()):
        from app.coaching.context import upgrade_state
        from app.postcall.finalize import status_in, unprocessed

        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            s.refresh(call, with_for_update=True)
            row = self._by_key(s, p, "conversation_states", call_id)
            if not row or row.data["finalization"].get("owner") != owner:
                return
            state = upgrade_state(row.data)
            signature = self._conversation_signature(s, p, call, state)
            # Live chunks beyond this turn's page are still finalization input. Chunks the
            # turn was offered but did not interpret are not retried (bounded passes).
            remaining = success and any(
                r["segment_id"] not in offered for r in unprocessed(self.transcript_rows(s, p, call_id))
            )
            changed = (
                signature != state["finalization"].get("input_fingerprint")
                or dt(state["finalization"]["lease_expires_at"]) <= self.clock()
                or remaining
            )
            status, coverage = status_in(self, s, p, call, state, success)
            state["coverage"].update(coverage)
            state["finalization"].update(
                status="pending" if changed else status,
                owner=None,
                source_version=state["version"],
                completed_at=self.clock().isoformat(),
                # Only an accepted merge completes this input. An unavailable
                # provider or lost response must allow an explicit, budgeted retry.
                completed_fingerprint=signature if success and not changed else None,
            )
            self._store_conversation(s, p, call, state, row)
            if changed:
                self.enqueue_finalize(s, p, call, "changed_input")
            self._materialize_views_in(s, p, call, finalized=True)
            lead = self.required(s, "leads", call.data["lead_id"], p)
            self.refresh_packet_in(s, p, lead)
            emit(s, p["tenant_id"], "analysis.ready", call, self.clock())

    def _publish_unavailable_rundown(self, p, call_id):
        """No AI is configured: publish the honest partial rundown once per input.

        No request context, budget ledger or warning. `completed_fingerprint` stays
        empty so configuring a provider later can still finalize the same input.
        """
        from app.coaching.context import upgrade_state
        from app.postcall.finalize import status_in
        from app.postcall.rubrics import effective

        def due(s, call):
            if not self._coach_authority(s, p, call):
                return None
            record = self._by_key(s, p, "conversation_states", call.id)
            state = upgrade_state(record.data if record else {}, call_id=call.id, rubric=effective(call.data))
            final = state["finalization"]
            signature = self._conversation_signature(s, p, call, state)
            if final.get("unavailable_fingerprint") == signature or (
                final.get("owner") and dt(final["lease_expires_at"]) > self.clock()
            ):
                return None
            return record, state, signature

        with self.db.transaction(False) as s:
            if not due(s, self.required(s, "call_attempts", call_id, p)):
                return
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            s.refresh(call, with_for_update=True)
            found = due(s, call)
            if not found:
                return
            record, state, signature = found
            status, coverage = status_in(self, s, p, call, state, False)
            state["coverage"].update(coverage)
            state["finalization"].update(
                status=status,
                reason="ai_unavailable",
                source_version=state["version"],
                completed_at=self.clock().isoformat(),
                unavailable_fingerprint=signature,
            )
            self._store_conversation(s, p, call, state, record)
            self._materialize_views_in(s, p, call, finalized=True)
            self.refresh_packet_in(s, p, self.required(s, "leads", call.data["lead_id"], p))
            emit(s, p["tenant_id"], "analysis.ready", call, self.clock())

    async def analyze_postcall(self, p, call_id):
        """The conversation_finalize job: merge still-live unprocessed chunks into the call state.

        Selective review of the merged items is the separately queued conversation_review.
        """
        await asyncio.to_thread(self.auto_disposition, p, call_id)
        provider, route = await asyncio.to_thread(self.configured_coach, p)
        if not provider:
            await asyncio.to_thread(self._publish_unavailable_rundown, p, call_id)
            return
        owner = await asyncio.to_thread(self._begin_finalization, p, call_id)
        if not owner:
            return
        success, offered = False, ()
        try:
            request = await asyncio.to_thread(self._conversation_context, p, call_id, "finalize")
            if request and not (request["recent_dialogue"] or request["state"]["observations"]):
                success = True  # Nothing left to interpret; coverage alone decides ready/partial.
            elif request:
                offered = {r["segment_id"] for r in request["recent_dialogue"]}
                result = await self._conversation_infer(
                    p,
                    call_id,
                    request,
                    provider,
                    route,
                    method="finalize" if hasattr(provider, "finalize") else "suggest",
                    kind="conversation_finalize",
                    current=lambda: self._conversation_request_current(p, call_id, request, owner=owner),
                )
                success = await asyncio.to_thread(
                    self._merge_finalization, p, call_id, owner, request, result
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Conversation finalization unavailable: %s", type(exc).__name__
            )
        finally:
            from app.coaching.engine import finish_shielded

            await finish_shielded(
                asyncio.to_thread(self._finish_finalization, p, call_id, owner, success, offered)
            )

    def _state_review_snapshot(self, p, call_id, request):
        from app.coaching.context import upgrade_state
        from app.coaching.sources import digest
        from app.grounding import review_request

        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            s.refresh(call, with_for_update=True)
            row = self._by_key(s, p, "conversation_states", call_id)
            if not self._conversation_current_in(s, p, call, request):
                return None
            state = upgrade_state(row.data)
            active = row.data.get("review_work", {})
            if active.get("owner") and dt(active["expires_at"]) > self.clock():
                return None
            catalog = {(i["kind"], i["id"], i["version"]): i for i in request["evidence_catalog"]}
            definitions = {i["id"]: i["version"] for i in request["trackers"]}
            fields, targets, unavailable = [], [], False
            for group in ("observations", "rubric_items", "tracker_matches"):
                for item in state[group]:
                    if item.get("review_status") != "pending" or len(fields) >= LIMITS.review_fields:
                        continue
                    refs = item.get("sources", [])
                    supported = [catalog.get((r["kind"], r["id"], r["version"])) for r in refs]
                    if (
                        not refs
                        or not all(supported)
                        or not any(
                            i["kind"] != "observation"
                            or i.get("epistemic_status") in {"model_reviewed", "human_confirmed"}
                            for i in supported
                        )
                        or group == "tracker_matches"
                        and definitions.get(item["tracker_id"]) != item["definition_version"]
                    ):
                        item["review_status"] = "unavailable"
                        unavailable = True
                        continue
                    identity = item.get("id") or item.get("dimension") or item.get("tracker_id")
                    field = group + "/" + identity
                    fields.append(
                        {
                            "field": field,
                            "value": {
                                k: item[k]
                                for k in (
                                    "text",
                                    "state",
                                    "approach",
                                    "observed_guidance_id",
                                    "strategy_id",
                                    "commitment",
                                    "outcome",
                                    "score",
                                    "status",
                                    "explanation",
                                    "tracker_id",
                                )
                                if k in item
                            },
                            "evidence": [r["kind"] + ":" + r["id"] for r in refs],
                        }
                    )
                    targets.append({"group": group, "id": identity, "digest": digest(item)})
            if unavailable:
                before = self._conversation_signature(s, p, call, upgrade_state(row.data))
                state["version"] += 1
                self._carry_finalization(s, p, call, state, before)
                self._store_conversation(s, p, call, state, row)
            if not fields:
                return None
            relevant = {identity for f in fields for identity in f["evidence"]}
            # Only referenced sources constrain this selective review. Its own
            # provisional targets are not evidence for themselves.
            request["evidence_catalog"] = [
                i for i in request["evidence_catalog"] if i["kind"] + ":" + i["id"] in relevant
            ]
            sources = [
                {**i, "id": i["kind"] + ":" + i["id"]}
                for i in catalog.values()
                if i["kind"] + ":" + i["id"] in relevant
            ]
            request["recent_dialogue"] = [
                r for r in request["recent_dialogue"] if "segment:" + r["segment_id"] in relevant
            ]
            review = review_request(
                "conversation",
                fields,
                sources,
                {
                    "rubric": request["rubric"],
                    "trackers": request["trackers"],
                    "human_note": request["human_note"],
                    "prospect_timezone": request["prospect_timezone"],
                    "call_at": request["call_at"],
                    "context_fingerprint": request["context_fingerprint"],
                    "displayed_approaches": request.get("recent_exposures", []),
                },
            )
            owner = uid()
            update(
                row,
                review_work={
                    "owner": owner,
                    "expires_at": (self.clock() + timedelta(seconds=30)).isoformat(),
                    "targets": targets,
                    "fingerprint": review["fingerprint"],
                },
            )
            return owner, review

    def _publish_state_review(self, p, call_id, request, owner, receipt):
        from app.coaching.context import lineage_metadata, segment_revisions, upgrade_state
        from app.coaching.sources import digest
        from app.postcall.commitments import publish

        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            s.refresh(call, with_for_update=True)
            row = self._by_key(s, p, "conversation_states", call_id)
            active = row.data.get("review_work", {})
            if active.get("owner") != owner:
                return
            if dt(active["expires_at"]) <= self.clock() or not self._conversation_current_in(
                s, p, call, request
            ):
                update(row, review_work={"owner": None})
                return
            if receipt and receipt.fingerprint != active["fingerprint"]:
                raise ValueError("Review receipt belongs to different conversation fields")
            state = upgrade_state(row.data)
            before = self._conversation_signature(s, p, call, state)
            changed, promises, retired, retractions = [], [], set(), set()
            history = self._conversation_lineage(s, p, call_id)
            for target in active["targets"]:
                item = next(
                    (
                        i
                        for i in state[target["group"]]
                        if (i.get("id") or i.get("dimension") or i.get("tracker_id")) == target["id"]
                    ),
                    None,
                )
                if not item or digest(item) != target["digest"]:
                    continue
                item["review_status"] = "reviewed" if receipt else "rejected"
                if receipt:
                    item.update(
                        epistemic_status="model_reviewed",
                        review_receipt_id=receipt.request_id,
                        reviewed_at=self.clock().isoformat(),
                    )
                    if target["group"] == "observations":
                        retired.update(item["replaces"])
                        changed.append(lineage_metadata(item))
                        if item["kind"] == "commitment" and item["state"] == "retracted":
                            retractions.update(item["replaces"])
                        if (
                            item["kind"] == "commitment"
                            and item.get("commitment")
                            and item["state"] not in {"stale", "retracted"}
                        ):
                            promises.append(
                                {
                                    **item["commitment"],
                                    "observation_id": item["id"],
                                    "action": item["text"],
                                    "evidence": list(segment_revisions(item, history)),
                                }
                            )
                elif item.get("state") in {"resolved", "retracted"}:
                    item["state"] = "stale"
            state["observations"] = [i for i in state["observations"] if i["id"] not in retired]
            state["version"] += 1
            # Review certifies the finalized input; it is not a new input to finalize again.
            finalized = self._carry_finalization(s, p, call, state, before)
            state["_lineage_updates"] = changed
            self._store_conversation(s, p, call, state, row)
            update(row, review_work={"owner": None})
            if finalized:
                self._materialize_views_in(s, p, call, finalized=True)
            if promises or retractions:
                publish(self, s, p, call, promises, retracted_observations=retractions)
            self._queue_conversation_review(s, p, call, state)
            emit(s, p["tenant_id"], "conversation.updated", call, self.clock())

    async def review_conversation(self, p, call_id):
        from app.grounding import accept_review

        provider, route = await asyncio.to_thread(self.configured_coach, p)
        if not provider:
            return  # Without a reviewer, pending items stay visibly unreviewed.
        request = await asyncio.to_thread(self._conversation_context, p, call_id, "review")
        if not request:
            return
        work = await asyncio.to_thread(self._state_review_snapshot, p, call_id, request)
        if not work:
            return
        owner, review = work
        receipt = None
        try:
            # The review body is raw-lifetime bounded, with separate request/usage ownership.
            result = await self._conversation_infer(
                p,
                call_id,
                review,
                provider,
                route,
                method="review",
                kind="conversation_review",
                current=lambda: self._conversation_request_current(p, call_id, request),
                raw_sources=request["recent_dialogue"],
            )
            receipt = accept_review(review, result, getattr(provider, "last_request_id", None))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).warning("Conversation review unavailable: %s", type(exc).__name__)
        finally:
            from app.coaching.engine import finish_shielded

            await finish_shielded(
                asyncio.to_thread(self._publish_state_review, p, call_id, request, owner, receipt)
            )
            await asyncio.to_thread(self.auto_disposition, p, call_id)

    def host_id(self):
        path = self.settings.data_dir / "host-id"
        if not path.exists():
            import os

            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write("urn:uuid:" + uid())
        return path.read_text().strip()
