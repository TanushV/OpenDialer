import asyncio
import contextlib
import logging
import time

from sqlalchemy import case, func, select

from app.accounting import RequestAccounting
from app.leads.record import number_view
from app.platform.config import LIMITS, PROVIDER_DEFAULTS, inference_model
from app.platform.db import MODELS, audit, doc, emit, put, rows, uid, update
from app.preparation.workflows import Preparation
from app.privacy import SpeechPrivacy
from app.service import Service, dt, fail

# Outcome of one claimed job in a drain pass: completed; requeued with progress
# or newer input; or handled without progress (coalesced, failed, lost lease).
DONE, ADVANCED, IDLE = "completed", "advanced", "idle"

OPTIONAL_REQUEST_ESTIMATE_USD = PROVIDER_DEFAULTS.optional_request_estimate_usd


class Application(Preparation, SpeechPrivacy, RequestAccounting, Service):
    def inference(self, tenant_id, role, user_id=None):
        conf = self.configs.read(tenant_id)
        # The ChatGPT login is the one route (R2.6); the DeepSeek fallback arrives in R9.10.
        from app.providers.codex import CodexClient, profile_home

        if not user_id:
            return None
        home = profile_home(self.settings.data_dir, user_id)
        if not (home / "auth.json").is_file():
            return None
        return CodexClient(home, inference_model(conf, role), "submit_" + role)

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
            model = MODELS["followup_tasks"]
            visible = (model.op_status != "aliased") | model.op_status.is_(None)
            scope = (
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.op_lead_id == lead.id,
                visible,
            )
            tasks = [
                doc(r)
                for r in s.scalars(
                    select(model)
                    .where(*scope)
                    .order_by(
                        case((model.op_status.in_(["completed", "cancelled", "retracted"]), 1), else_=0),
                        model.op_at.desc(),
                        model.id.desc(),
                    )
                    .limit(20)
                )
            ]
            totals = {
                kind: s.scalar(
                    select(func.count())
                    .select_from(MODELS[kind])
                    .where(
                        MODELS[kind].tenant_id == p["tenant_id"],
                        MODELS[kind].seller_id == p["seller_id"],
                        MODELS[kind].op_lead_id == lead.id,
                        (MODELS[kind].op_status != "aliased") | MODELS[kind].op_status.is_(None),
                    )
                )
                for kind in ("call_attempts", "followup_tasks")
            }
            return {
                **self.prospect_context_in(s, p, lead, calls, tasks, decision=decision),
                "lead": {
                    **doc(lead),
                    **number_view(lead.data, self.clock()),
                    "suppressed": self.suppressed(s, p, lead.data["phone"]),
                },
                "script": script,
                "preparation": preparation,
                "research": {"facts": facts, "sources": sources, "fact_limit": 64},
                "bound_number_id": binding.data["number_id"] if binding else None,
                "previous_calls": calls,
                "context_coverage": {
                    "calls_total": totals["call_attempts"],
                    "calls_omitted": max(0, totals["call_attempts"] - len(calls)),
                    "tasks_total": totals["followup_tasks"],
                    "tasks_omitted": max(0, totals["followup_tasks"] - len(tasks)),
                    "scope": "Most recent 20 calls and up to 20 tasks, with unresolved work first. Full records remain retained.",
                },
            }

    def call_review(self, p, call_id):
        """One call for Review: its outcome, the after-call summary, the rep's note and the 30-day transcript."""
        from app.aftercall.trace import behavioral_metrics

        with self.db.transaction(False) as s:
            call = self.required(s, "call_attempts", call_id, p)
            metadata = self.transcript_metadata(s, p, call_id)
            segments = self.transcript_rows(s, p, call_id, metadata=metadata)
            note = self._by_key(s, p, "notes", call_id)
            stable = [r.data for r in metadata if r.data.get("is_final") and not r.data.get("system_audio")]
            return {
                "call": doc(call),
                "lead_timezone": self.required(s, "leads", call.data["lead_id"], p).data.get("timezone"),
                "summary": self.call_summary(s, p, call_id),
                "human_notes": note.data.get("human_text") if note else None,
                "transcript": segments,
                "metrics": behavioral_metrics(stable),
                "script_trace": call.data.get("script_trace"),
                "raw_evidence_status": self.raw_status(call, segments),
            }

    def edit_notes(self, p, call_id, text):
        """The rep's own note on a call. One writer, so the last save is the note (R3.6)."""
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            lead = self.required(s, "leads", call.data["lead_id"], p)
            note = self._by_key(s, p, "notes", call_id)
            values = {"call_id": call_id, "human_text": text, "editor_id": p["user_id"]}
            if note:
                update(note, **values)
            else:
                put(s, "notes", p["tenant_id"], p["seller_id"], values, key=call_id, now=self.clock())
            self.refresh_packet_in(s, p, lead)
            audit(s, p["tenant_id"], p["seller_id"], p["user_id"], "notes.edited", call_id, self.clock())
            return {"human_notes": text}

    async def run_jobs(self, tenant_id=None, queues=None):
        from app.platform.jobs import LostJobLease, UnknownJobKind

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
                    if kind == "aftercall_summary":
                        await self.summarize_call(p, job["payload"]["call_id"])
                    # Older queued finalize and review jobs (before R10.1) have nothing left to do.
                elif lane == "live_coach":
                    from app import live_help

                    await live_help.turn(self, p, job["payload"]["call_id"], automatic=True)
                elif lane == "crm":
                    from app import crm

                    await crm.run_job(self, p, job["payload"])
                elif lane == "ml":
                    if job["payload"].get("kind") == "report_refresh":
                        pass  # A saved report view from before R8.4; the report is now computed when read.
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
        capacity.update(research=LIMITS.agent_threads, scripts=LIMITS.agent_threads, ml=2, crm=2)
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

        lanes = list(queues or ["webhook", "live_coach", "postcall", "scripts", "research", "ml", "crm"])
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
            from app.ml.training import promotion_allowed

            promote = promotion_allowed(result)
            if promote:
                for other in self.scoped(s, "model_versions", p):
                    if other.data.get("active"):
                        update(other, active=False, replaced_at=self.clock().isoformat())
            model = put(
                s,
                "model_versions",
                p["tenant_id"],
                p["seller_id"],
                {
                    **result,
                    "active": promote,
                    "promoted_at": self.clock().isoformat() if promote else None,
                    "source_dataset_id": payload.get("source_dataset_id"),
                    "goal": payload.get("goal", "meeting_booked"),
                    "window_days": payload.get("window_days"),
                },
                key="training:" + payload["source_dataset_id"],
                now=self.clock(),
            )
            # R8.5, R8.6: each promotion is recorded; a model that does not pass stays as evidence only.
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                None,
                "model.promoted" if promote else "model.kept_inactive",
                model.id,
                self.clock(),
                {"reviewable": bool(result.get("reviewable"))},
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
        await asyncio.to_thread(self.purge_transcripts)
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
                    from app import crm

                    await asyncio.to_thread(crm.schedule_syncs, self, p)
                await self.release_pool_leases(p)
                await self.watch_calling(p)
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

    def enqueue_aftercall(self, s, p, call, *, retry=False):
        """R10.2: one after-call summary per transcript version, queued when the call ends or its transcript changes.
        A retry is its own job, so a failed summary can be written again (R10.4)."""
        if call.data.get("state") not in {"ENDED", "FINALIZED", "ENDING"}:
            return None
        version = call.data.get("transcript_version", 0)
        key = f"aftercall-summary:{call.id}:{version}" + (f":retry:{uid()}" if retry else "")
        return self.jobs.enqueue_in(
            s,
            "postcall",
            {
                "kind": "aftercall_summary",
                "call_id": call.id,
                "seller_id": p["seller_id"],
                "user_id": p.get("user_id") or call.data["rep_id"],
            },
            key,
            tenant_id=p["tenant_id"],
        )

    def summary_model(self, p):
        """The after-call model: DeepSeek V4.1 Flash on OpenRouter's no-retention route; simulated in simulation."""
        key = self.configs.read(p["tenant_id"]).get("openrouter_api_key")
        if not key:
            return None
        if self.settings.simulation:
            from app.providers.simulated import SimulatedSummaryModel

            return SimulatedSummaryModel()
        from app.providers.chat_agent import BASE_URL, MODEL, ROUTING
        from app.providers.inference import InferenceClient

        return InferenceClient("litellm", key, MODEL, base_url=BASE_URL, extra_body=ROUTING, timeout=60)

    async def summarize_call(self, p, call_id):
        from app.aftercall.summary import summarize

        with self.db.transaction(False) as s:
            call = self.required(s, "call_attempts", call_id, p)
            lead = self.required(s, "leads", call.data["lead_id"], p)
            segments = self.transcript_rows(s, p, call_id)
            version = call.data.get("transcript_version", 0)
            goal = self.optimization_config(s, p).get("conversion_goal")
        model = self.summary_model(p)
        if model is None:
            result = {
                "status": "unavailable",
                "error": "Add an OpenRouter key in Settings for after-call summaries",
            }
        else:
            result = await summarize(model, segments, lead.data, goal)
        from app.optimization.script_learning import script_trace

        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            s.refresh(call, with_for_update=True)
            if call.data.get("transcript_version", 0) != version:
                return  # A newer transcript queued its own summary.
            # Which script sections were read and how the prospect reacted, for script learning.
            trace = script_trace(
                call.data.get("script_snapshot") or {},
                [r.data for r in self.transcript_metadata(s, p, call_id)],
            )
            if call.data.get("script_trace") != trace:
                update(call, script_trace=trace)
            # The summary is its own record, so a slow model never holds the call row.
            values = {
                **result,
                "call_id": call_id,
                "transcript_version": version,
                "at": self.clock().isoformat(),
            }
            record = self._by_key(s, p, "call_summaries", call_id)
            if record:
                update(record, **values)
            else:
                put(
                    s, "call_summaries", p["tenant_id"], p["seller_id"], values, key=call_id, now=self.clock()
                )
            emit(s, p["tenant_id"], "call.updated", call, self.clock())
            if result.get("summary"):
                from app import crm

                crm.enqueue_summary(self, s, p, call)
        # A mailbox or other carrier fact the hang-up could not save yet is saved now; a person's answer never is.
        await asyncio.to_thread(self.auto_disposition, p, call_id)

    def call_summary(self, s, p, call_id):
        """The after-call summary record's data, or None before it is written."""
        record = self._by_key(s, p, "call_summaries", call_id)
        return record.data if record else None

    def host_id(self):
        path = self.settings.data_dir / "host-id"
        if not path.exists():
            import os

            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write("urn:uuid:" + uid())
        return path.read_text().strip()
