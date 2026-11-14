"""The live helper wired to the store (R10.1). The rules live in `helper/live.py`.

A turn uses the call's script, lead context and dialogue to select a spoken response from the script.
Only the resolved script wording is published to the Dialer. A turn never writes the call, the lead or a task.
"""

import asyncio
import logging
from datetime import timedelta

import httpx
from sqlalchemy import select

from app import ops
from app.calling.state import TERMINAL
from app.helper import live
from app.platform.config import PROVIDER_DEFAULTS
from app.platform.db import MODELS, Job, doc, emit, uid
from app.providers.inference import InferenceResult

log = logging.getLogger(__name__)


def provider(service, p):
    """The coach model, or None without an OpenRouter key. Simulation answers with a fixed local model."""
    conf = service.configs.read(p["tenant_id"])
    if conf.get("coach_provider") == "static":
        return None
    if service.settings.simulation:
        from app.providers.simulated import SimulatedCoach

        return SimulatedCoach()
    if not conf.get("openrouter_api_key"):
        return None
    from app.providers.openrouter import OpenRouterCoach

    return OpenRouterCoach(
        conf["openrouter_api_key"],
        conf.get("coach_model", PROVIDER_DEFAULTS.coach_model),
        http=getattr(service, "http", None),
    )


def queue(service, s, p, call, transcript_version, delay_ms=live.DEBOUNCE_MS):
    """Coalesce finalized prospect speech into the next scheduled selection."""
    service.jobs.coalesce_live_in(
        s,
        "live_coach",
        {
            "call_id": call.id,
            "seller_id": p["seller_id"],
            "user_id": call.data["rep_id"],
            "transcript_version": transcript_version,
        },
        f"coach:{call.id}",
        tenant_id=p["tenant_id"],
        priority=100,
        available_at=service.clock() + timedelta(milliseconds=delay_ms),
    )


def queue_confirmed_turn(service, p, call_id, transcript_version):
    with service.db.transaction() as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        if _live(service, s, p, call) and not _muted(call):
            queue(service, s, p, call, transcript_version, delay_ms=0)


def _live(service, s, p, call):
    """Whether the helper may answer on this call now."""
    if call.data["state"] in TERMINAL | {"ENDING"} or call.data.get("transcription_disabled"):
        return False
    lead = ops.required(service, s, "leads", call.data["lead_id"], p)
    return not ops.suppressed(service, s, p, lead.data["phone"])


def request_in(service, s, p, call, question):
    """Everything one turn sends: script, lead, research, earlier calls and the call so far."""
    from app.script_follow import latest_match

    lead = ops.required(service, s, "leads", call.data["lead_id"], p)
    script = call.data.get("script_snapshot") or ops.selected_script(service, s, p, lead)[0]
    facts = [
        doc(r).get("statement")
        for r in ops.context_rows(
            service,
            s,
            p,
            "research_facts",
            lead.id,
            MODELS["research_facts"].ctx_status == "supported",
            MODELS["research_facts"].ctx_expires_at > service.clock(),
            limit=8,
        )
    ]
    earlier = []
    for prior in reversed(
        ops.lead_records(
            service,
            s,
            p,
            "call_attempts",
            lead.id,
            MODELS["call_attempts"].id != call.id,
            MODELS["call_attempts"].op_status.in_(TERMINAL),
            limit=3,
            newest=True,
        )
    ):
        summary = ops.call_summary(service, s, p, prior.id) or {}
        note = ops._by_key(service, s, p, "notes", prior.id)
        earlier.append(
            {
                "at": prior.data.get("started_at") or "",
                "outcome": prior.data.get("outcome"),
                "summary": summary.get("summary"),
                "next_step": summary.get("next_step"),
                "notes": ((note.data.get("human_text") if note else None) or "")[:1000],
            }
        )
    segments = ops.transcript_rows(service, s, p, call.id)
    delivered = latest_match(segments)
    dialogue = [
        {"side": r.get("side"), "text": r["text"], "script_section": r.get("script_section")}
        for r in segments
        if r.get("is_final") and not r.get("system_audio") and r.get("text")
    ][-live.DIALOGUE :]
    seller = ops.required(service, s, "sellers", p["seller_id"], p)
    shown = service.tips.shown.get(call.id)
    return {
        "seller": seller.data.get("name", ""),
        "lead": {k: lead.data.get(k) for k in ("name", "company", "title", "segment", "prospect_brief")},
        "script": script,
        "research": [f for f in facts if f],
        "earlier_calls": earlier,
        "dialogue": dialogue,
        "script_section": delivered["script_section"] if delivered else None,
        "previous": shown.get("say_or_ask") if shown else None,
        "question": question,
    }


def _prepare(service, p, call_id, question, *, automatic):
    with service.db.transaction(False) as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        if not _live(service, s, p, call):
            return None, "ended"
        if automatic and _muted(call):
            return None, "muted"
        return request_in(service, s, p, call, question), None


def _muted(call):
    return call.data.get("coach_preferences", {}).get("muted", False)


def _publish(service, p, call_id, tip, *, automatic):
    """Show a completed selection and tell the Dialer to re-read the call."""
    with service.db.transaction() as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        if not _live(service, s, p, call):
            return "ended"
        if automatic and _muted(call):
            return "muted"
        service.tips.show(call_id, tip)
        emit(s, p["tenant_id"], "coach.card_ready", call, service.clock())
        return "shown"


def _requeue(service, p, call_id, transcript_version):
    with service.db.transaction() as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        job = s.scalar(select(Job).where(Job.tenant_id == p["tenant_id"], Job.key == f"coach:{call_id}"))
        # When private help is in flight, preserve any prospect turn already queued behind it.
        newest = job is not None and job.payload.get("transcript_version") == transcript_version
        if newest and _live(service, s, p, call) and not _muted(call):
            queue(service, s, p, call, transcript_version, live.BUSY_RETRY_MS)


async def turn(service, p, call_id, question=None, request_id=None, *, transcript_version=None):
    """One helper turn. Returns its status: shown, quiet, busy, muted, unavailable, cancelled or ended."""
    private = transcript_version is None
    identity = request_id or uid()
    result = {"request_id": identity, "private": private}
    if not private and service.tips.busy(call_id):
        await asyncio.to_thread(_requeue, service, p, call_id, transcript_version)
        return {**result, "status": "busy"}
    request, skipped = await asyncio.to_thread(_prepare, service, p, call_id, question, automatic=not private)
    if request is None:
        return {**result, "status": skipped}
    if not private and live.screener(request["dialogue"]):
        # R11.8: show the rendered screening line without a model or key.
        screening = (request.get("script") or {}).get("screening_intro")
        part = {"label": "Screening", "text": screening} if screening else None
        tip = live.card(
            identity, screening or live.SCREENER_TIP, private=False, now=service.clock(), part=part
        )
        status = await asyncio.to_thread(_publish, service, p, call_id, tip, automatic=not private)
        return {**result, "status": status}
    parts = live.script_parts(request.get("script"), spoken_only=True)
    if not parts:
        return {**result, "status": "quiet"}
    coach = await asyncio.to_thread(provider, service, p)
    if coach is None:
        service.tips.status[call_id] = {"status": "paused", "reason": "not_configured"}
        return {**result, "status": "unavailable"}
    key = f"coach:{call_id}:{identity}"
    if not await asyncio.to_thread(
        ops.reserve_optional_budget, service, p, key, 0.01, object_id=call_id, work_kind="live_coach"
    ):
        service.tips.status[call_id] = {"status": "paused", "reason": "budget"}
        return {**result, "status": "unavailable"}

    task = asyncio.create_task(
        coach.converse(live.messages(request, parts=parts), quiet=live.QUIET, deadline_ms=live.DEADLINE_MS)
    )
    service.tips.start(call_id, identity, task)
    text, status, unsent = None, "shown", False
    try:
        text = await task
    except asyncio.CancelledError:
        status = "cancelled"
    except Exception as exc:
        reason = getattr(exc, "reason", None) or "transport_fault"
        log.warning("Live helper turn failed: %s", type(exc).__name__)
        service.tips.status[call_id] = {"status": "paused", "reason": reason}
        status = "unavailable"
        # The request never reached OpenRouter: nothing can be billed, so its hold is released.
        unsent = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
    finally:
        service.tips.finish(call_id, task)
        await asyncio.to_thread(_settle, service, p, coach, call_id, key, unsent)
    if status != "shown":
        return {**result, "status": status}
    service.tips.status[call_id] = {"status": "available"}
    part = live.reply(text, parts)
    if part is None:
        return {**result, "status": "quiet"}
    tip = live.card(identity, part["text"], private=private, now=service.clock(), part=part)
    status = await asyncio.to_thread(_publish, service, p, call_id, tip, automatic=not private)
    return {**result, "status": status}


def _settle(service, p, coach, call_id, key, unsent):
    """Record what the turn cost; a request that never left this computer releases its hold."""
    try:
        if unsent and not getattr(coach, "last_request_id", None):
            ops.release_unsubmitted_budget(service, p, key)
            return
        result = InferenceResult(
            {}, getattr(coach, "last_usage", {}) or {}, getattr(coach, "last_request_id", None)
        )
        ops.usage(
            service, p, "openrouter", "live_coach", result, object_id=call_id, reserved=0, request_key=key
        )
    except Exception as exc:  # The ledger never decides whether the rep sees a tip.
        log.warning("Live helper usage was not recorded: %s", type(exc).__name__)


def cancel(service, p, call_id, request_id):
    """The rep cancelled a question still being answered."""
    with service.db.transaction(False) as s:
        ops.required(service, s, "call_attempts", call_id, p)
    return {"status": "cancelled" if service.tips.cancel(call_id, request_id) else "not_running"}


def view(service, call):
    """What the Dialer shows on this call now: the tip and the helper's status."""
    if call.data["state"] in TERMINAL | {"ENDING"}:
        return None, None
    return service.tips.current(call.id, service.clock()), service.tips.status.get(call.id)


def end(service, call_id):
    service.tips.end(call_id)
