import asyncio
import contextlib
import functools
import logging
import time

from sqlalchemy import case, func, select

from app import ops
from app.leads.record import number_view
from app.platform.config import LIMITS, PROVIDER_DEFAULTS, inference_model
from app.platform.db import MODELS, audit, doc, emit, put, rows, uid, update
from app.service import dt

# Outcome of one claimed job in a drain pass: completed; requeued with progress
# or newer input; or handled without progress (coalesced, failed, lost lease).
DONE, ADVANCED, IDLE = "completed", "advanced", "idle"

OPTIONAL_REQUEST_ESTIMATE_USD = PROVIDER_DEFAULTS.optional_request_estimate_usd


def inference(service, tenant_id, role, user_id=None):
    conf = service.configs.read(tenant_id)
    # The ChatGPT login is the one route (R2.6); the DeepSeek fallback arrives in R9.10.
    from app.providers.codex import CodexClient, profile_home

    if not user_id:
        return None
    home = profile_home(service.settings.data_dir, user_id)
    if not (home / "auth.json").is_file():
        return None
    return CodexClient(home, inference_model(conf, role), "submit_" + role)


def packet(service, p, lead_id):
    with service.db.transaction(False) as s:
        lead = ops.required(service, s, "leads", lead_id, p)
        fact_model = MODELS["research_facts"]
        facts = [
            doc(r)
            for r in ops.context_rows(
                service,
                s,
                p,
                "research_facts",
                lead.id,
                fact_model.ctx_status == "supported",
                fact_model.ctx_expires_at > service.clock(),
                limit=32,
            )
        ]
        reservations = ops.lead_records(
            service,
            s,
            p,
            "reservations",
            lead.id,
            MODELS["reservations"].op_owner_id == p["user_id"],
            limit=1,
            newest=True,
        )
        assignment = reservations[0].data.get("script_comparison") if reservations else None
        script, preparation = ops.selected_script(service, s, p, lead, assignment=assignment)
        call_model = MODELS["call_attempts"]
        active_calls = ops.lead_records(
            service,
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
                and dt(candidate.data["expires_at"]) > service.clock()
            ):
                owner, origin = candidate, "frozen_reservation_decision"
        if owner and owner.data.get("feature_snapshot_id"):
            row = ops.required(service, s, "feature_snapshots", owner.data["feature_snapshot_id"], p)
            if row.data.get("lead_id") == lead.id and row.data.get("predictions"):
                decision = {**doc(row), "source": origin}
        facts = list(
            {f["id"]: f for f in [*facts, *ops.script_evidence(service, s, p, lead, script)]}.values()
        )
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
        binding = ops._by_key(service, s, p, "lead_number_bindings", lead.id)
        calls = [
            doc(r)
            for r in reversed(
                ops.lead_records(service, s, p, "call_attempts", lead.id, limit=20, newest=True)
            )
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
            **ops.prospect_context_in(service, s, p, lead, calls, tasks, decision=decision),
            "lead": {
                **doc(lead),
                **number_view(lead.data, service.clock()),
                "suppressed": ops.suppressed(service, s, p, lead.data["phone"]),
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


def call_review(service, p, call_id):
    """One call for Review: its outcome, the after-call summary, the rep's note and the 30-day transcript."""
    from app.aftercall.trace import behavioral_metrics

    with service.db.transaction(False) as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        metadata = ops.transcript_metadata(service, s, p, call_id)
        segments = ops.transcript_rows(service, s, p, call_id, metadata=metadata)
        note = ops._by_key(service, s, p, "notes", call_id)
        stable = [r.data for r in metadata if r.data.get("is_final") and not r.data.get("system_audio")]
        return {
            "call": doc(call),
            "lead_timezone": ops.required(service, s, "leads", call.data["lead_id"], p).data.get("timezone"),
            "summary": ops.call_summary(service, s, p, call_id),
            "human_notes": note.data.get("human_text") if note else None,
            "transcript": segments,
            "metrics": behavioral_metrics(stable),
            "script_trace": call.data.get("script_trace"),
            "raw_evidence_status": ops.raw_status(service, call, segments),
        }


def edit_notes(service, p, call_id, text):
    """The rep's own note on a call. One writer, so the last save is the note (R3.6)."""
    with service.db.transaction() as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        lead = ops.required(service, s, "leads", call.data["lead_id"], p)
        note = ops._by_key(service, s, p, "notes", call_id)
        values = {"call_id": call_id, "human_text": text, "editor_id": p["user_id"]}
        if note:
            update(note, **values)
        else:
            put(s, "notes", p["tenant_id"], p["seller_id"], values, key=call_id, now=service.clock())
        ops.refresh_packet_in(service, s, p, lead)
        audit(s, p["tenant_id"], p["seller_id"], p["user_id"], "notes.edited", call_id, service.clock())
        return {"human_notes": text}


async def run_jobs(service, tenant_id=None, queues=None):
    from app.platform.jobs import LostJobLease, UnknownJobKind

    if (queues is None or "ml" in queues) and service.settings.worker_enabled:
        await ops.periodic_optimization(service, tenant_id)
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
            service.jobs.coalesce_refresh, job["id"], worker_id, progressed=paged
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
                    await asyncio.to_thread(service.jobs.heartbeat, job["id"], worker_id, 60)
                except LostJobLease:
                    return
                except Exception as exc:
                    logging.getLogger(__name__).warning("Job heartbeat deferred: %s", type(exc).__name__)

        pulse = asyncio.create_task(heartbeat())
        kind = job["payload"].get("kind")
        try:
            if lane == "webhook":
                receipt = await ops.process_webhook(service, job["tenant_id"], job["payload"]["receipt_id"])
                if receipt["status"] in {"processing", "retry", "unmatched"}:
                    raise RuntimeError("Webhook processing is incomplete")
            elif lane in {"research", "scripts"}:
                if kind == "packet_refresh":
                    # Lead recomputation moved out of the rep's save into this refresh.
                    await asyncio.to_thread(ops.refresh_continuity, service, p, job["payload"]["lead_ids"])
                elif kind == "prep_thread":
                    await ops.run_prep_thread(service, p, job["payload"]["lead_ids"], job["id"])
                    await asyncio.to_thread(ops.prepare_tick, service, p, job["id"])
                else:
                    raise UnknownJobKind(f"No {lane} handler for job kind {kind!r}")
            elif lane == "postcall":
                if kind == "aftercall_summary":
                    await ops.summarize_call(service, p, job["payload"]["call_id"])
                # Older queued finalize and review jobs (before R10.1) have nothing left to do.
            elif lane == "live_coach":
                from app import live_help

                if (version := job["payload"].get("transcript_version")) is not None:
                    await live_help.turn(service, p, job["payload"]["call_id"], transcript_version=version)
            elif lane == "crm":
                from app import crm

                await crm.run_job(service, p, job["payload"])
            elif lane == "ml":
                if job["payload"].get("kind") == "report_refresh":
                    pass  # A saved report view from before R8.4; the report is now computed when read.
                elif job["payload"].get("kind") == "optimization_refresh":
                    await asyncio.to_thread(
                        ops.refresh_optimization_job, service, p, lease=(job["id"], worker_id)
                    )
                elif job["payload"].get("kind") in {"health_refresh", "learning_refresh"}:
                    method = (
                        functools.partial(ops.refresh_observed_health, service)
                        if job["payload"]["kind"] == "health_refresh"
                        else functools.partial(ops.refresh_learning, service)
                    )
                    receipt = await asyncio.to_thread(method, p, lease=(job["id"], worker_id))
                    if receipt["status"] in {"superseded", "pending"}:
                        return await requeue(job, receipt)
                    if job["payload"]["kind"] == "learning_refresh":
                        try:
                            await ops.propose_from_memory(service, p)
                        except Exception as exc:  # A proposal is optional; the refresh stands.
                            logging.getLogger(__name__).warning(
                                "Script proposal deferred: %s", type(exc).__name__
                            )
                elif kind is None:  # A training job saved before StatML was archived (D31): nothing to do.
                    pass
                else:
                    raise UnknownJobKind(f"No ml handler for job kind {kind!r}")
            else:
                raise ValueError("Unknown work lane")
            await asyncio.to_thread(service.jobs.complete, job["id"], worker_id)
            return DONE
        except LostJobLease:
            return IDLE
        except Exception as exc:
            logging.getLogger(__name__).warning("execute handled a failure: %s", type(exc).__name__)
            errors.append(
                {"kind": "job", "job_id": job["id"], "queue": lane, "error_type": type(exc).__name__}
            )
            with contextlib.suppress(LostJobLease):
                await asyncio.to_thread(service.jobs.fail, job["id"], worker_id, type(exc).__name__)
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
        pool = service.job_admission["preparation" if lane in {"research", "scripts"} else lane]
        seen = set()
        for _ in range(20):
            jobs = await asyncio.to_thread(
                service.jobs.claim_many, lane, worker_id, capacity[lane], 60, tenant_id
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


async def periodic_optimization(service, tenant_id=None):
    now = time.monotonic()
    if now < getattr(service, "_next_optimization", 0):
        return
    service._next_optimization = now + 60
    try:
        await asyncio.to_thread(ops.schedule_optimization, service, tenant_id)
    except Exception:
        service._next_optimization = 0
        raise


async def maintenance_tick(service):
    await ops.periodic_optimization(service)
    service.raw.purge()
    await asyncio.to_thread(ops.purge_transcripts, service)
    await asyncio.to_thread(ops.queue_unfinished_webhooks, service)
    prepare_due = time.monotonic() >= getattr(service, "_next_preparation", 0)
    if prepare_due:
        service._next_preparation = time.monotonic() + 60
    prepared_sellers = set()
    errors = []
    completed = 0

    def principals():
        """Seller-scoped recovery runs once per seller under its active administrator.

        A seller without one keeps the per-user passes, because a rep's own
        authority only covers that rep's calls.
        """
        with service.db.transaction(False) as session:
            users = sorted(rows(session, "users"), key=lambda u: u.created_at)
        # One user (R3.4); an older store may hold a teammate row, which acts for nobody.
        for user in users[:1]:
            yield {"tenant_id": user.tenant_id, "seller_id": user.seller_id, "user_id": user.id}

    for p in await asyncio.to_thread(lambda: list(principals())):
        try:
            if prepare_due and p["seller_id"] not in prepared_sellers:
                prepared_sellers.add(p["seller_id"])
                await asyncio.to_thread(ops.prepare_tick, service, p)
                from app import crm

                await asyncio.to_thread(crm.schedule_syncs, service, p)
            try:
                await ops.recheck_pending_numbers(service, p)
            except Exception as exc:  # A carrier or relay outage waits for the next pass.
                logging.getLogger(__name__).warning("Number recheck deferred: %s", type(exc).__name__)
            await ops.watch_calling(service, p)
            # Recovery ends with the seller's control dispatch; one pass per tick.
            await ops.recover(service, p)
            completed += 1
        except Exception as exc:
            logging.getLogger(__name__).warning("Maintenance reconciliation deferred: %s", type(exc).__name__)
            errors.append({"kind": "maintenance", "user_id": p["user_id"], "error_type": type(exc).__name__})
    return {"status": "failed" if errors else "passed", "completed": completed, "errors": errors}


def enqueue_aftercall(service, s, p, call, *, retry=False):
    """R10.2: one after-call summary per transcript version, queued when the call ends or its transcript changes.
    A retry is its own job, so a failed summary can be written again (R10.4)."""
    if call.data.get("state") not in {"ENDED", "FINALIZED", "ENDING"}:
        return None
    version = call.data.get("transcript_version", 0)
    key = f"aftercall-summary:{call.id}:{version}" + (f":retry:{uid()}" if retry else "")
    return service.jobs.enqueue_in(
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


def summary_model(service, p):
    """The after-call model: DeepSeek V4.1 Flash on OpenRouter's no-retention route; simulated in simulation."""
    key = service.configs.read(p["tenant_id"]).get("openrouter_api_key")
    if not key:
        return None
    if service.settings.simulation:
        from app.providers.simulated import SimulatedSummaryModel

        return SimulatedSummaryModel()
    from app.providers.chat_agent import BASE_URL, MODEL, ROUTING
    from app.providers.inference import InferenceClient

    return InferenceClient("litellm", key, MODEL, base_url=BASE_URL, extra_body=ROUTING, timeout=60)


async def summarize_call(service, p, call_id):
    from app.aftercall.summary import summarize
    from app.helper.live import script_parts

    with service.db.transaction(False) as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        lead = ops.required(service, s, "leads", call.data["lead_id"], p)
        segments = ops.transcript_rows(service, s, p, call_id)
        version = call.data.get("transcript_version", 0)
        goal = ops.optimization_config(service, s, p).get("conversion_goal")
        parts = script_parts(call.data.get("script_snapshot") or {})
    model = ops.summary_model(service, p)
    if model is None:
        result = {
            "status": "unavailable",
            "error": "Add an OpenRouter key in Settings for after-call summaries",
        }
    else:
        result = await summarize(model, segments, lead.data, goal, parts)
    from app.optimization.script_learning import script_trace

    with service.db.transaction() as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        s.refresh(call, with_for_update=True)
        if call.data.get("transcript_version", 0) != version:
            return  # A newer transcript queued its own summary.
        # Which script sections were read and how the prospect reacted, for script learning.
        trace = script_trace(
            call.data.get("script_snapshot") or {},
            [r.data for r in ops.transcript_metadata(service, s, p, call_id)],
        )
        if call.data.get("script_trace") != trace:
            update(call, script_trace=trace)
        # The summary is its own record, so a slow model never holds the call row.
        values = {
            **result,
            "call_id": call_id,
            "transcript_version": version,
            "at": service.clock().isoformat(),
        }
        record = ops._by_key(service, s, p, "call_summaries", call_id)
        if record:
            update(record, **values)
        else:
            put(s, "call_summaries", p["tenant_id"], p["seller_id"], values, key=call_id, now=service.clock())
        emit(s, p["tenant_id"], "call.updated", call, service.clock())
        if result.get("summary"):
            from app import crm

            crm.enqueue_summary(service, s, p, call)
    # A mailbox or other carrier fact the hang-up could not save yet is saved now; a person's answer never is.
    await asyncio.to_thread(ops.auto_disposition, service, p, call_id)


def call_summary(service, s, p, call_id):
    """The after-call summary record's data, or None before it is written."""
    record = ops._by_key(service, s, p, "call_summaries", call_id)
    return record.data if record else None


def host_id(service):
    path = service.settings.data_dir / "host-id"
    if not path.exists():
        import os

        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write("urn:uuid:" + uid())
    return path.read_text().strip()


DROP_OFF_DAYS = 30


def drop_off_report(service, p):
    """Where answered calls ended, preferring tracked speech and retaining older summary evidence."""
    from datetime import timedelta

    from app.aftercall.dropoff import drop_off
    from app.helper.live import script_parts
    from app.script_follow import latest_match

    since = service.clock() - timedelta(days=DROP_OFF_DAYS)
    with service.db.transaction(False) as s:
        calls = []
        for row in ops.scoped(service, s, "call_attempts", p):
            started = dt(row.data.get("started_at"))
            if row.data.get("is_test") or not started or started < since:
                continue
            summary = ops._by_key(service, s, p, "call_summaries", row.id)
            ended = summary.data.get("ended_at_part") if summary else None
            if row.data.get("human_answered_at"):
                transcript = [segment.data for segment in ops.transcript_metadata(service, s, p, row.id)]
                if any(segment.get("script_match_version") for segment in transcript):
                    part = latest_match(transcript)
                    ended = None
                    if part and not (
                        row.data.get("analysis_incomplete") or row.data.get("transcription_disabled")
                    ):
                        for section in (row.data.get("script_snapshot") or {}).get("sections", []):
                            if any(
                                f"{section.get('key')}:{option['id']}" == part["script_section"]
                                for option in section.get("options") or ()
                            ):
                                ended = {"stage": section["title"]}
                                break
            calls.append(
                {
                    "human": bool(row.data.get("human_answered_at")),
                    "outcome": row.data.get("outcome"),
                    "ended_at_part": ended,
                }
            )
        campaign = ops.selected_campaign(service, s, p)
        script = ops.script_base(service, s, p, campaign).get("template") or {}
    # Script order from the adopted script's spoken parts; a stage only older calls used is listed after them.
    stages = list(dict.fromkeys(label.split(" · ")[0] for label, _, _ in script_parts(script)))
    return {**drop_off(calls, stages), "window_days": DROP_OFF_DAYS}
