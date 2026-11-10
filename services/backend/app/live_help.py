"""The live helper wired to the store (R10.1). The rules live in `helper/live.py`.

A turn reads the call's script, the lead, its research, earlier calls' summaries and the call so far, streams one reply
into memory and tells the Dialer to re-read the call. It never writes the call, the lead or a task.
"""

import asyncio
import logging
from datetime import timedelta

import httpx

from app import ops
from app.calling.state import TERMINAL
from app.helper import live
from app.platform.config import PROVIDER_DEFAULTS
from app.platform.db import MODELS, doc, emit, uid
from app.providers.inference import InferenceResult

log = logging.getLogger(__name__)


def provider(service, p):
    """The coach model, or None without an OpenRouter key. Simulation answers with a fixed local model."""
    conf = service.configs.read(p["tenant_id"])
    if not conf.get("openrouter_api_key") or conf.get("coach_provider") == "static":
        return None
    if service.settings.simulation:
        from app.providers.simulated import SimulatedCoach

        return SimulatedCoach()
    from app.providers.openrouter import OpenRouterCoach

    return OpenRouterCoach(
        conf["openrouter_api_key"],
        conf.get("coach_model", PROVIDER_DEFAULTS.coach_model),
        http=getattr(service, "http", None),
    )


def queue(service, s, p, call, delay_ms=live.DEBOUNCE_MS):
    """A final segment arrived on a live call: one automatic turn shortly after, coalesced with any already queued."""
    service.jobs.coalesce_live_in(
        s,
        "live_coach",
        {"call_id": call.id, "seller_id": p["seller_id"], "user_id": call.data["rep_id"]},
        f"coach:{call.id}",
        tenant_id=p["tenant_id"],
        priority=100,
        available_at=service.clock() + timedelta(milliseconds=delay_ms),
    )


def _live(service, s, p, call):
    """Whether the helper may answer on this call now."""
    if call.data["state"] in TERMINAL | {"ENDING"} or call.data.get("transcription_disabled"):
        return False
    lead = ops.required(service, s, "leads", call.data["lead_id"], p)
    return not ops.suppressed(service, s, p, lead.data["phone"])


def request_in(service, s, p, call, question):
    """Everything one turn sends: script, lead, research, earlier calls and the call so far."""
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
    dialogue = [
        {"side": r.get("side"), "text": r.get("text", "")}
        for r in ops.transcript_rows(service, s, p, call.id)
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
        "previous": shown.get("say_or_ask") if shown else None,
        "question": question,
    }


def _prepare(service, p, call_id, question, automatic):
    with service.db.transaction(False) as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        if not _live(service, s, p, call):
            return None, "ended"
        if automatic and call.data.get("coach_preferences", {}).get("muted"):
            return None, "muted"
        request = request_in(service, s, p, call, question)
        if automatic and not any(d["side"] == "remote" for d in request["dialogue"]):
            return None, "quiet"
        return request, None


def _publish(service, p, call_id, tip):
    """Show `tip` (None clears it) and tell the Dialer to re-read the call."""
    with service.db.transaction() as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        if not _live(service, s, p, call):
            return False
        if tip is None:
            service.tips.dismiss(call_id)
        else:
            service.tips.show(call_id, tip)
        emit(s, p["tenant_id"], "coach.card_ready", call, service.clock())
        return True


def _requeue(service, p, call_id):
    with service.db.transaction() as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        if call.data["state"] not in TERMINAL | {"ENDING"}:
            queue(service, s, p, call, live.BUSY_RETRY_MS)


async def turn(service, p, call_id, question=None, request_id=None, *, automatic=False):
    """One helper turn. Returns its status: shown, quiet, busy, muted, unavailable, cancelled or ended."""
    private = not automatic
    identity = request_id or uid()
    result = {"request_id": identity, "private": private}
    if automatic and service.tips.busy(call_id):
        await asyncio.to_thread(_requeue, service, p, call_id)
        return {**result, "status": "busy"}
    request, skipped = await asyncio.to_thread(_prepare, service, p, call_id, question, automatic)
    if request is None:
        return {**result, "status": skipped}
    if automatic and live.screener(request["dialogue"]):
        # R11.8: a fixed line, with no model call and no key needed.
        tip = live.card(identity, live.SCREENER_TIP, private=False, done=True, now=service.clock())
        await asyncio.to_thread(_publish, service, p, call_id, tip)
        return {**result, "status": "shown"}
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

    parts = live.script_parts(request.get("script"))

    async def progress(text):
        part, shown = live.reply(text, parts)
        if shown:
            tip = live.card(identity, shown, private=private, done=False, now=service.clock(), part=part)
            await asyncio.to_thread(_publish, service, p, call_id, tip)

    async def run():
        return await coach.converse(
            live.messages(request), on_progress=progress, quiet=live.QUIET, deadline_ms=live.DEADLINE_MS
        )

    task = asyncio.create_task(run())
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
    if status == "cancelled":
        await asyncio.to_thread(_withdraw, service, p, call_id, identity)
        return {**result, "status": status}
    if status == "unavailable":
        await asyncio.to_thread(_withdraw, service, p, call_id, identity)
        return {**result, "status": status}
    service.tips.status[call_id] = {"status": "available"}
    part, shown = live.reply(text, parts)
    if shown is None:
        await asyncio.to_thread(_withdraw, service, p, call_id, identity)
        return {**result, "status": "quiet"}
    tip = live.card(identity, shown, private=private, done=True, now=service.clock(), part=part)
    published = await asyncio.to_thread(_publish, service, p, call_id, tip)
    return {**result, "status": "shown" if published else "ended"}


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


def _withdraw(service, p, call_id, identity):
    """A turn that ended without an answer takes back anything it showed while writing."""
    current = service.tips.shown.get(call_id)
    if current and current["id"] == identity and current["status"] == "streaming":
        _publish(service, p, call_id, None)


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
