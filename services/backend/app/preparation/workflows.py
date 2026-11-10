"""Per-lead preparation before calling: agent threads research each lead and write its script.

Importing or preparing a list marks its leads `preparing`; the queue never offers them until their
thread closes. A minute tick packs open leads into threads (one company per thread where possible,
`leads_per_agent` leads each) and keeps at most `agents_at_once` threads queued or running.
"""

import asyncio
import hashlib
import json
import logging
from datetime import timedelta

from sqlalchemy import func, select

from app import ops
from app.lead_lists import budget_scope, memberships
from app.platform.db import MODELS, Job, doc, emit, put, uid, update
from app.preparation.agent import INSTRUCTIONS, TOOLS, SimulatedAgent, ThreadWork, simulated_page
from app.preparation.templates import PreparationPolicy, fill_fields, render, template_version
from app.service import dt

# A failing provider costs at most this many attempts per lead and input, then the lead is
# released to calling with the base script.
MAX_ATTEMPTS = 3
RETRY_BACKOFF = (timedelta(minutes=5), timedelta(minutes=30))
STABLE_DAYS, TIME_SENSITIVE_DAYS = 30, 7
# Reserved per lead on metered routes before a thread runs; the receipt settles it.
METERED_ESTIMATE_PER_LEAD_USD = 0.25
PLAN_PAUSE = timedelta(minutes=20)
OPEN = ("missing", "queued", "stale")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def failed_attempts(state, key):
    """Consecutive failures for this exact preparation input; a new key starts over."""
    if state.get("status") != "failed" or state.get("key") != key:
        return 0
    return state.get("attempts", 1)


def retry_due(state, now):
    return failed_attempts(state, state.get("key")) < MAX_ATTEMPTS and (
        not state.get("next_attempt_at") or dt(state["next_attempt_at"]) <= now
    )


def needs_preparation(state, now):
    return state["status"] in OPEN or (state["status"] == "failed" and retry_due(state, now))


def failure_state(previous, key, now, error):
    attempts = failed_attempts(previous, key) + 1
    return {
        "status": "failed",
        "key": key,
        "script_id": previous.get("script_id"),
        "as_of": now.isoformat(),
        "expires_at": None,
        "attempts": attempts,
        "next_attempt_at": (now + RETRY_BACKOFF[attempts - 1]).isoformat()
        if attempts < MAX_ATTEMPTS
        else None,
        "error": error[:200],
    }


def company_key(lead):
    """The group of contacts at one company: imported company id, else domain, else company name."""
    for field in ("company_id", "domain", "company"):
        value = " ".join(str(lead.get(field) or "").casefold().split()).removeprefix("www.")
        if value and value != "unknown":
            return hashlib.sha256((field + ":" + value).encode()).hexdigest()[:32]
    return None


def preparation_permitted(service, s, p, lead):
    return (
        lead.data.get("status") in {"eligible", "preparing"}
        and ops.list_permitted(service, s, p, lead)
        and not ops.suppressed(service, s, p, lead.data["phone"])
    )


def preparation_settings(service, s, p):
    key = ("preparation_settings", p["tenant_id"], p["seller_id"])
    if key not in s.info:
        seller = ops.required(service, s, "sellers", p["seller_id"], p)
        stored = seller.data.get("operating_policy", {}).get("preparation", {})
        s.info[key] = {"preparation": PreparationPolicy(**stored).model_dump()}
    return s.info[key]


def script_base(service, s, p, campaign):
    """The campaign's adopted script, or none yet (version 0)."""
    stored = campaign.data.get("script") or {}
    template = stored.get("template")
    if template and template.get("format") == 2:
        return {k: stored.get(k) for k in ("version", "template", "adopted_at", "source")}
    return {"version": stored.get("version", 0), "template": None, "adopted_at": None, "source": None}


def lead_script_base(service, s, p, lead):
    return ops.script_base(service, s, p, ops.required(service, s, "campaigns", lead.data["campaign_id"], p))


def preparation_key(service, s, p, lead):
    """What a prepared script depends on: the base script, the lead's row and the script memory."""
    campaign = ops.required(service, s, "campaigns", lead.data["campaign_id"], p)
    base = ops.script_base(service, s, p, campaign)
    return digest(
        [
            template_version(base["template"]),
            lead.data.get("row") or {k: lead.data.get(k) for k in ("name", "company", "domain")},
            campaign.data.get("script_memory_key"),
            lead.data.get("rewrite_generation"),
        ]
    )


def agent_route(service, p):
    """The agent that prepares leads: the ChatGPT login, else the OpenRouter fallback, else None (D8)."""
    if service.settings.simulation:
        return SimulatedAgent()
    conf = service.configs.read(p["tenant_id"])
    provider = None
    if conf.get("research_route") != "fallback":
        provider = ops.inference(service, p["tenant_id"], "script_write", p["user_id"])
    if provider and hasattr(provider, "run"):
        return provider
    return ops.fallback_agent(service, p)


def fallback_agent(service, p, fetch=None):
    """DeepSeek V4.1 Flash on OpenRouter with local search (R9.10), when an OpenRouter key is saved."""
    if service.settings.simulation:
        return None
    key = service.configs.read(p["tenant_id"]).get("openrouter_api_key")
    if not key:
        return None
    from app.providers import ddgs
    from app.providers.chat_agent import ChatAgent

    if fetch is None:
        from app.platform.config import LIMITS
        from app.research.fetch import SafeFetcher

        async def fetch(url):
            return await SafeFetcher(max_bytes=LIMITS.research_bytes).fetch(url)

    return ChatAgent(key, search=ddgs.search, fetch_page=fetch)


def fallback_script(service, s, p, lead):
    """The base script with the seller's default options and no AI text."""
    template = ops.lead_script_base(service, s, p, lead)["template"]
    seller = ops.required(service, s, "sellers", p["seller_id"], p).data["name"]
    rep = ops.required(service, s, "users", p["user_id"], p).data["name"]
    script = {
        "lead_id": lead.id,
        "script_version": "base",
        "template_version": template_version(template),
        "template": template,
        "fact_ids_used": [],
        "claims": [],
        "fallback": True,
        "ready_for_rep": bool(template),
        "prohibited_claim_checks": [],
    }
    if template:
        script.update(render(template, lead=doc(lead), seller=seller, rep=rep))
    else:
        opener = f"Hi, this is {rep} from {seller}."
        script.update(
            sections=[
                {
                    "key": "opening",
                    "title": "Opening",
                    "options": [
                        {"id": "opening-1", "title": None, "text": opener, "picked": True, "rank": 0}
                    ],
                }
            ],
            opener=opener,
            screening_intro=f"{rep} from {seller}, calling with a business introduction.",
            gatekeeper="Who is the right person to speak with about this?",
            discovery_questions=["What would make this conversation useful for you?"],
            closing_question="Would a further conversation be useful?",
            offers=[],
            objection_cards=[],
            picks={},
            written={},
        )
    return script


def preparation_state(service, s, p, lead):
    state = dict(lead.data.get("preparation") or {"status": "missing"})
    if state.get("status") in {"ready", "skipped"} and (
        state.get("key") != ops.preparation_key(service, s, p, lead)
        or (state.get("expires_at") and dt(state["expires_at"]) <= service.clock())
    ):
        state["status"] = "stale"
    return state


def script_evidence(service, s, p, lead, script):
    model = MODELS["research_facts"]
    return (
        [
            doc(row)
            for row in ops.context_rows(
                service,
                s,
                p,
                "research_facts",
                lead.id,
                model.id.in_(script.get("fact_ids_used", [])[:32]),
                model.ctx_status == "supported",
                model.ctx_expires_at > service.clock(),
                limit=32,
            )
        ]
        if script.get("fact_ids_used")
        else []
    )


def script_row(service, s, p, lead):
    pointer = (lead.data.get("preparation") or {}).get("script_id")
    row = s.get(MODELS["script_versions"], pointer) if pointer else None
    if row and (
        row.tenant_id != p["tenant_id"] or row.seller_id != p["seller_id"] or row.data["lead_id"] != lead.id
    ):
        row = None
    return row


def selected_script(service, s, p, lead, *, assignment=None):
    """The script a rep sees for this lead: its prepared picks and text over the current base."""
    state = ops.preparation_state(service, s, p, lead)
    row = ops.script_row(service, s, p, lead)
    script = ops.fallback_script(service, s, p, lead)
    template = script["template"]
    proposal, arm = None, None
    if assignment and assignment.get("family") == "script":
        testing = ops.testing_proposal(service, s, p, lead.data["campaign_id"], assignment["comparison_id"])
        if testing:
            arm = assignment.get("arm")
            proposal = testing if arm == "candidate" else None
            template = testing["template" if proposal else "control_template"]
    prepared = dict(row.data) if row else None
    if (
        prepared
        and template
        and state["status"] == "ready"
        and prepared.get("template_version") == template_version(template)
    ):
        # New masters get fresh output. Historical scripts remain only in their call snapshots.
        facts = {fact["fact_id"]: fact for fact in ops.script_evidence(service, s, p, lead, prepared)}
        blocks = {block["id"]: block["instruction"] for block in template["blocks"]}
        written = {
            block: value
            for block, value in prepared.get("written", {}).items()
            if prepared.get("instructions", {}).get(block) == blocks.get(block)
            and all(i.startswith("row:") or i in facts for i in value.get("fact_ids", []))
        }
        options = {section["key"]: {o["id"] for o in section["options"]} for section in template["sections"]}
        picks = {
            key: ids
            for key, ids in prepared.get("picks", {}).items()
            if key in options and set(ids) <= options[key]
        }
        seller = ops.required(service, s, "sellers", p["seller_id"], p).data["name"]
        rep = ops.required(service, s, "users", p["user_id"], p).data["name"]
        used = sorted(
            {i for value in written.values() for i in value.get("fact_ids", []) if not i.startswith("row:")}
        )
        script.update(
            render(template, lead=doc(lead), seller=seller, rep=rep, picks=picks, written=written),
            script_version=prepared["script_version"],
            fact_ids_used=used,
            claims=[{"text": v["text"], "fact_ids": v["fact_ids"]} for v in written.values()],
            fallback=False,
            summary=prepared.get("summary"),
            template_version=template_version(template),
        )
    elif template is not script["template"] and template:
        seller = ops.required(service, s, "sellers", p["seller_id"], p).data["name"]
        rep = ops.required(service, s, "users", p["user_id"], p).data["name"]
        script.update(render(template, lead=doc(lead), seller=seller, rep=rep))
    follow = ops.follow_up_in(service, s, p, lead, template)
    if follow:
        script.update(sections=[follow, *script.get("sections", [])], call_purpose=follow["purpose"])
    base = ops.lead_script_base(service, s, p, lead)
    script.update(
        # Calls snapshot this script; the parsed sections are kept, the pasted Markdown is not.
        template={k: v for k, v in template.items() if k != "markdown"} if template else None,
        base_version=base["version"],
        script_arm=arm,
        proposal_id=proposal["id"] if proposal else None,
        preparation_status=state["status"],
    )
    return script, state


def follow_up_in(service, s, p, lead, template):
    """What this call is for, assembled now from the lead's real history (None on a first call).

    Built from saved call notes, callback promises and outcomes rather than a new model
    run, so it is current after every call or correction and needs no model to be up.
    It never mentions a conversation that did not happen.
    """
    from app.aftercall.tasks import ACTIVE as ACTIVE_TASKS
    from app.calling.state import TERMINAL

    attempts = lead.data.get("attempt_count", 0)
    calls = ops.lead_records(
        service,
        s,
        p,
        "call_attempts",
        lead.id,
        MODELS["call_attempts"].op_status.in_(TERMINAL),
        limit=10,
        newest=True,
    )
    callback = next(
        (
            t
            for t in ops.lead_records(service, s, p, "followup_tasks", lead.id)
            if t.data.get("kind") == "callback" and t.data.get("status") in ACTIVE_TASKS
        ),
        None,
    )
    if not attempts and not calls and not callback:
        return None
    campaign = ops.required(service, s, "campaigns", lead.data["campaign_id"], p)
    policy = ops.required(service, s, "policy_versions", campaign.data["policy_id"], p).data
    name = (lead.data.get("name") or "").strip()
    values = {
        "name": name or "there",
        "first_name": name.split()[0] if name and name.casefold() != "unknown" else "there",
        "company": lead.data.get("company") or "your company",
        "seller": ops.required(service, s, "sellers", p["seller_id"], p).data["name"],
        "rep": ops.required(service, s, "users", p["user_id"], p).data["name"],
    }
    spoken = [c for c in calls if c.data.get("human_answered_at")]
    lines = []
    if callback:
        purpose, note = "callback", (callback.data.get("note") or "").strip()
        lines.append(
            "They asked for this callback"
            + (f": {note}" if note else ".")
            + " Open by saying you are calling back as promised."
        )
    elif spoken:
        purpose = "follow_up"
    else:
        purpose = "retry"
        lines.append(
            f"No one has answered the {attempts} earlier call{'s' if attempts != 1 else ''}."
            " Introduce yourself fresh and do not mention earlier calls."
        )
    if spoken:
        latest = spoken[0]
        summary = ops.call_summary(service, s, p, latest.id) or {}
        note = ops._by_key(service, s, p, "notes", latest.id)
        when = (dt(latest.data.get("started_at")) or service.clock()).date().isoformat()
        # D4: the next script reads the after-call summary and the rep's own note.
        if summary.get("summary"):
            lines.append(f"Last conversation ({when}): {summary['summary']}")
        else:
            lines.append(f"You spoke on {when}; no summary was saved.")
        if summary.get("next_step"):
            lines.append("Agreed next step: " + summary["next_step"])
        if note and note.data.get("human_text"):
            lines.append("Your note: " + note.data["human_text"][:500])
    if template and template.get("follow_up"):
        lines.append(fill_fields(template["follow_up"], values))
    if not callback and attempts + 1 >= policy.get("max_attempts", 4):
        purpose = "final"
        voicemail = (template or {}).get("voicemail") or (
            "Hi {first_name}, this is {rep} with {seller}. I have tried you a few times about"
            " {company}. If a short conversation would help, call me back at this number. Thanks."
        )
        lines.append(
            "Final attempt. If it reaches voicemail, leave this message: " + fill_fields(voicemail, values)
        )
    return {"key": "follow_up", "title": "This call", "text": "\n".join(lines), "purpose": purpose}


def pending_preparation(service, s, p, finishing=None):
    """Lead IDs with a queued or running thread for this seller."""
    scope = (
        Job.tenant_id == p["tenant_id"],
        (Job.seller_id == p["seller_id"]) | Job.seller_id.is_(None),
        Job.queue == "research",
        Job.status.in_(["queued", "running"]),
    )
    return {
        lead_id
        for job in s.scalars(select(Job).where(*scope))
        if job.payload.get("seller_id") == p["seller_id"] and job.id != finishing
        for lead_id in job.payload.get("lead_ids", [])
    }


def running_threads(service, s, p, finishing=None):
    return sum(
        1
        for job in s.scalars(
            select(Job).where(
                Job.tenant_id == p["tenant_id"],
                Job.queue == "research",
                Job.status.in_(["queued", "running"]),
            )
        )
        if job.payload.get("seller_id") == p["seller_id"] and job.id != finishing
    )


def request_preparation_in(service, s, p, leads):
    """Mark leads to be prepared before they can be called. Returns how many were marked."""
    marked = 0
    for lead in leads:
        state = lead.data.get("preparation") or {}
        if lead.data.get("status") not in {"eligible", "preparing"}:
            continue
        if lead.data.get("prepare_requested") and state.get("status") != "failed":
            continue  # Already requested; a failed lead is asked again with fresh attempts.
        if ops.suppressed(service, s, p, lead.data["phone"]):
            continue  # An opted-out number is never researched or scripted.
        update(
            lead,
            prepare_requested=True,
            status="preparing" if state.get("status") not in {"ready", "skipped"} else lead.data["status"],
            preparation={**state, "status": "queued", "attempts": 0, "next_attempt_at": None}
            if state.get("status") not in {"ready", "skipped"}
            else state,
        )
        marked += 1
    return marked


def prior_call_context_in(service, s, p, lead_ids):
    calls, notes = MODELS["call_attempts"], MODELS["notes"]
    result = {}
    for identity in lead_ids:
        recent = list(
            s.scalars(
                select(calls)
                .where(
                    calls.tenant_id == p["tenant_id"],
                    calls.seller_id == p["seller_id"],
                    calls.op_lead_id == identity,
                )
                .order_by(calls.created_at.desc())
                .limit(3)
            )
        )
        summaries = []
        for row in recent:
            note = s.scalar(
                select(notes)
                .where(
                    notes.tenant_id == p["tenant_id"],
                    notes.seller_id == p["seller_id"],
                    notes.external_key == row.id,
                )
                .limit(1)
            )
            summaries.append(
                {
                    "outcome": row.data.get("outcome"),
                    "at": row.data.get("started_at"),
                    "summary": (
                        ((note.data.get("notes") or {}).get("summary") or "")
                        if note and not note.data.get("stale")
                        else ""
                    )[:1000],
                    "human_notes": ((note.data.get("human_text") or "") if note else "")[:1000],
                }
            )
        result[identity] = summaries
    return result


def rewrite_scripts(
    service,
    p,
    *,
    list_name=None,
    lead_ids=None,
    refresh_research=False,
    sample_size=None,
    remaining_only=False,
):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        model = MODELS["leads"]
        query = (
            select(model)
            .where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.op_status.in_(["eligible", "preparing"]),
            )
            .order_by(model.created_at, model.id)
        )
        if lead_ids is not None:
            query = query.where(model.id.in_(lead_ids))
        generation, count = uid(), 0
        for lead in s.scalars(query):
            if list_name is not None and list_name not in memberships(lead.data):
                continue
            if not ops.preparation_permitted(service, s, p, lead):
                continue
            if remaining_only and ops.preparation_state(service, s, p, lead)["status"] == "ready":
                continue
            if sample_size is not None and count >= sample_size:
                break
            update(
                lead,
                rewrite_generation=generation,
                refresh_research=refresh_research,
                prepare_requested=True,
                preparation={
                    **(lead.data.get("preparation") or {}),
                    "status": "queued",
                    "attempts": 0,
                    "next_attempt_at": None,
                },
            )
            count += 1
        update(ops.required(service, s, "sellers", p["seller_id"], p), preparation_user_id=p["user_id"])
    ops.prepare_tick(service, p)
    return {"marked": count, "sample": sample_size is not None, "generation": generation}


def prepare_list(service, p, list_name=None, lead_ids=None):
    """Prepare a whole list (or the given leads) before calling."""
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        if not ops.lead_script_base_ready(service, s, p):
            from app.service import fail

            fail(409, "Add your script before preparing leads")
        model = MODELS["leads"]
        query = select(model).where(
            model.tenant_id == p["tenant_id"],
            model.seller_id == p["seller_id"],
            model.op_status.in_(["eligible", "preparing"]),
        )
        if lead_ids is not None:
            query = query.where(model.id.in_(lead_ids))
        leads = [
            lead
            for lead in s.scalars(query)
            if list_name is None or lead_ids is not None or list_name in memberships(lead.data)
        ]
        marked = ops.request_preparation_in(service, s, p, leads)
        update(ops.required(service, s, "sellers", p["seller_id"], p), preparation_user_id=p["user_id"])
    ops.prepare_tick(service, p)
    return {"marked": marked}


def lead_script_base_ready(service, s, p):
    campaign = ops.scoped(service, s, "campaigns", p)[0]
    return bool(ops.script_base(service, s, p, campaign)["template"])


def release_list(service, p, list_name):
    """Call a list's unprepared leads now with the base script (after a budget stop, say)."""
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        model = MODELS["leads"]
        released = 0
        pending = ops.pending_preparation(service, s, p)
        for lead in s.scalars(
            select(model).where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.op_status == "preparing",
            )
        ):
            if list_name in memberships(lead.data) and lead.id not in pending:
                update(lead, status="eligible", prepare_requested=False)
                released += 1
        return {"released": released}


def prepare_tick(service, p, finishing=None):
    """Queue threads for open leads, keeping at most `agents_at_once` running.

    A thread that just finished passes its own job id, so it does not count against the limit."""
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        seller = ops.required(service, s, "sellers", p["seller_id"], p)
        paused = seller.data.get("preparation_paused_until")
        if paused and dt(paused) > service.clock():
            return {"queued": 0, "paused_until": paused}
        config = ops.preparation_settings(service, s, p)["preparation"]
        room = config["agents_at_once"] - ops.running_threads(service, s, p, finishing)
        if room <= 0 or not ops.lead_script_base_ready(service, s, p):
            return {"queued": 0}
        pending = ops.pending_preparation(service, s, p, finishing)
        model = MODELS["leads"]
        now = service.clock()
        candidates = []
        for status in ("preparing", "eligible"):
            if len(candidates) >= room * config["leads_per_agent"]:
                break
            for lead in s.scalars(
                select(model)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.op_status == status,
                )
                .order_by(model.created_at, model.id)
                .limit(2000)
            ):
                if lead.id in pending or not lead.data.get("prepare_requested"):
                    continue
                if not ops.preparation_permitted(service, s, p, lead):
                    continue
                if needs_preparation(ops.preparation_state(service, s, p, lead), now):
                    candidates.append(lead)
                    if len(candidates) >= room * config["leads_per_agent"]:
                        break
        if not candidates:
            return {"queued": 0}
        jobs = ops.queue_threads_in(service, s, p, candidates, config["leads_per_agent"], room)
        return {"queued": sum(len(job["lead_ids"]) for job in jobs), "threads": len(jobs)}


def queue_threads_in(service, s, p, leads, size, room):
    """Pack leads into threads of `size`, keeping each company's contacts together."""
    groups = {}
    for lead in leads:
        groups.setdefault(lead.data.get("company_key") or lead.id, []).append(lead)
    threads, current = [], []
    for group in groups.values():
        if current and len(current) + len(group) > size:
            threads.append(current)
            current = []
        for lead in group:
            current.append(lead)
            if len(current) == size:
                threads.append(current)
                current = []
    if current:
        threads.append(current)
    jobs = []
    for batch in threads[:room]:
        for lead in batch:
            state = lead.data.get("preparation") or {}
            update(lead, preparation={**state, "status": "queued"} if state.get("status") in OPEN else state)
        payload = {
            "kind": "prep_thread",
            "lead_ids": [lead.id for lead in batch],
            "seller_id": p["seller_id"],
            # Threads run as the person who asked for preparation: their ChatGPT login.
            "user_id": ops.required(service, s, "sellers", p["seller_id"], p).data.get("preparation_user_id")
            or p["user_id"],
        }
        key = "prepare:" + digest(
            [[lead.id, ops.preparation_key(service, s, p, lead)] for lead in batch] + [uid()]
        )
        service.jobs.enqueue_in(s, "research", payload, key, tenant_id=p["tenant_id"])
        jobs.append(payload)
    return jobs


def preparation_status(service, p):
    """Progress per lead list, as the Leads page and the Dialer show it."""
    with service.db.transaction(False) as s:
        model = MODELS["leads"]
        lists, now = {}, service.clock()
        summary = dict.fromkeys(("total", "ready", "skipped", "failed", "working", "waiting"), 0)
        pending = ops.pending_preparation(service, s, p)
        for lead in s.scalars(
            select(model).where(model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"])
        ):
            if not lead.data.get("prepare_requested"):
                continue
            if not ops.preparation_permitted(service, s, p, lead):
                continue
            state = ops.preparation_state(service, s, p, lead)
            category = (
                "working"
                if lead.id in pending
                else state["status"]
                if state["status"] in {"ready", "skipped"}
                else "failed"
                if state["status"] == "failed" and not retry_due(state, now)
                else "waiting"
            )
            summary["total"] += 1
            summary[category] += 1
            catalog = ops.required(service, s, "campaigns", lead.data["campaign_id"], p).data.get("lists", {})
            for name in memberships(lead.data):
                if catalog.get(name, {}).get("archived"):
                    continue
                entry = lists.setdefault(
                    name,
                    {
                        "list_name": name,
                        "total": 0,
                        "ready": 0,
                        "skipped": 0,
                        "failed": 0,
                        "working": 0,
                        "waiting": 0,
                        "skipped_leads": [],
                    },
                )
                entry["total"] += 1
                if lead.id in pending:
                    entry["working"] += 1
                elif state["status"] in {"ready", "skipped"}:
                    entry[state["status"]] += 1
                    if state["status"] == "skipped" and len(entry["skipped_leads"]) < 20:
                        entry["skipped_leads"].append(
                            {"name": lead.data.get("name", ""), "reason": state.get("reason", "")}
                        )
                elif state["status"] == "failed" and not retry_due(state, now):
                    entry["failed"] += 1
                else:
                    entry["waiting"] += 1
        seller = ops.required(service, s, "sellers", p["seller_id"], p)
        paused = seller.data.get("preparation_paused_until")
        config = ops.preparation_settings(service, s, p)["preparation"]
        threads = [
            {
                "id": job.id,
                "status": job.status,
                "leads": len(job.payload.get("lead_ids", [])),
                "progress": job.payload.get("progress"),
            }
            for job in s.scalars(
                select(Job)
                .where(
                    Job.tenant_id == p["tenant_id"],
                    Job.queue == "research",
                    Job.status.in_(["queued", "running"]),
                )
                .order_by(Job.available_at, Job.id)
            )
            if job.payload.get("seller_id") == p["seller_id"]
        ]
        budget = ops.scoped(service, s, "budgets", p)[0].data
        campaign = ops.selected_campaign(service, s, p)
        for entry in lists.values():
            entry["spent_usd"] = ops.scope_spend(
                service, s, p, budget_scope(campaign.data, entry["list_name"])
            )
            entry["budget_usd"] = budget.get("per_list_usd", 25.0)
        return {
            "master_version": ops.script_base(service, s, p, campaign)["version"],
            "summary": summary,
            "lists": sorted(lists.values(), key=lambda e: e["list_name"]),
            "threads": threads,
            "paused_until": paused if paused and dt(paused) > now else None,
            "pause_reason": seller.data.get("preparation_pause_reason")
            if paused and dt(paused) > now
            else None,
            "settings": config,
            "has_script": ops.lead_script_base_ready(service, s, p),
            "ai_connected": ops.agent_route(service, p) is not None,
            "callable": s.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.op_status == "eligible",
                )
            ),
        }


def claim_thread(service, p, lead_ids):
    """Lease each lead for this thread; leads already prepared for the current input are left out."""
    owner, claimed = uid(), []
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        for lead_id in lead_ids:
            lead = ops.required(service, s, "leads", lead_id, p)
            s.refresh(lead, with_for_update=True)
            if not ops.preparation_permitted(service, s, p, lead):
                continue
            lease = lead.data.get("preparation_lease") or {}
            if lease.get("expires_at") and dt(lease["expires_at"]) > service.clock():
                continue
            state = ops.preparation_state(service, s, p, lead)
            if not needs_preparation(state, service.clock()):
                continue
            key = ops.preparation_key(service, s, p, lead)
            update(
                lead,
                preparation_lease={
                    "owner": owner,
                    "key": key,
                    "expires_at": (service.clock() + timedelta(hours=2)).isoformat(),
                },
            )
            facts = [
                doc(row)
                for row in ops.context_rows(
                    service,
                    s,
                    p,
                    "research_facts",
                    lead.id,
                    MODELS["research_facts"].ctx_status == "supported",
                    MODELS["research_facts"].ctx_expires_at > service.clock(),
                    limit=12,
                )
            ]
            claimed.append(
                {
                    "lead": doc(lead),
                    "key": key,
                    "facts": [] if lead.data.get("refresh_research") else facts,
                }
            )
        campaign = ops.scoped(service, s, "campaigns", p)[0]
        base = ops.script_base(service, s, p, campaign)
        template = base["template"]
        for item in claimed:
            item["base_version"] = base["version"]
        memory = ops.script_memory_digest_in(service, s, p, campaign) if template else None
    return owner, claimed, template, memory


async def run_prep_thread(service, p, lead_ids, job_id=None):
    """Research the thread's leads, write their scripts, then publish or record each outcome."""
    from app.providers.codex import PlanLimit

    owner, claimed, template, memory = await asyncio.to_thread(ops.claim_thread, service, p, lead_ids)
    if not claimed:
        return {"prepared": 0}
    agent = await asyncio.to_thread(ops.agent_route, service, p)
    if not template or agent is None:
        await asyncio.to_thread(
            ops.finish_thread, service, p, owner, claimed, None, "AI or script unavailable", release=True
        )
        return {"prepared": 0}
    leads = [
        {
            "id": item["lead"]["id"],
            "row": item["lead"].get("row")
            or {
                k: item["lead"].get(k)
                for k in ("name", "company", "domain", "phone", "state")
                if item["lead"].get(k)
            },
            "name": item["lead"].get("name"),
            "company": item["lead"].get("company"),
            "domain": item["lead"].get("domain"),
            "signals": item["lead"].get("signals"),
        }
        for item in claimed
    ]
    if service.settings.simulation:
        fetch = simulated_page([lead["row"] for lead in leads])
    else:
        from app.platform.config import LIMITS
        from app.research.fetch import SafeFetcher

        async def fetch(url):
            return await SafeFetcher(max_bytes=LIMITS.research_bytes).fetch(url)

    work = ThreadWork(leads, template, fetch, today=service.clock().date())
    for item in claimed:
        alias = work.aliases[item["lead"]["id"]]
        for fact in item["facts"]:
            work.facts[alias][f"{alias}/f{len(work.facts[alias]) + 1}"] = {
                "statement": fact["statement"],
                "quote": fact.get("evidence_quote", ""),
                "source_url": fact.get("source_url", ""),
                "stability": fact.get("stability", "stable"),
                "stored_id": fact["fact_id"],
            }
    metered = getattr(agent, "billing", "openai") not in {"chatgpt_plan", "simulation"}
    request_key = "prepare:" + owner
    scope = await asyncio.to_thread(
        ops.preparation_budget_scope, service, p, claimed[0]["lead"].get("list_name") or ""
    )
    if metered and not await asyncio.to_thread(
        ops.reserve_optional_budget,
        service,
        p,
        request_key,
        METERED_ESTIMATE_PER_LEAD_USD * len(claimed),
        object_id=claimed[0]["lead"]["id"],
        work_kind="preparation",
        scope=scope,
    ):
        await asyncio.to_thread(ops.pause_preparation, service, p, "This list reached its AI budget.", None)
        await asyncio.to_thread(
            ops.finish_thread, service, p, owner, claimed, None, None, release=False, requeue=True
        )
        return {"prepared": 0, "budget": "reached"}
    context = work.context(memory)
    reuse = all(item["facts"] for item in claimed)
    context["research_mode"] = "reuse" if reuse else "research"
    context["prior_calls"] = {lead["id"]: [] for lead in leads}
    from app.model_tokens import capture

    with service.db.transaction(False) as session:
        base_version = claimed[0]["base_version"]
        history = ops.prior_call_context_in(service, session, p, [item["lead"]["id"] for item in claimed])
    context["prior_calls"] = {work.aliases[identity]: calls for identity, calls in history.items()}
    tracking = {
        "campaign_id": claimed[0]["lead"]["campaign_id"],
        "base_version": base_version,
        "template_version": template_version(template),
        "lead_ids": [item["lead"]["id"] for item in claimed],
        "list_names": sorted({name for item in claimed for name in memberships(item["lead"])}),
        "job_id": job_id,
        "model": getattr(agent, "model", None),
        "research": "reused" if reuse else "researched",
    }
    provider_name = getattr(agent, "billing", "openai")
    work_kind = "preparation"

    async def observe(value, status="running"):
        await asyncio.to_thread(
            capture, service, p, provider_name, work_kind, request_key, tracking, value, status
        )

    await observe({})
    if any(item["facts"] for item in claimed):
        context["known_facts"] = {
            alias: [{"id": i, "statement": f["statement"]} for i, f in facts.items()]
            for alias, facts in work.facts.items()
            if facts
        }
    usage, failure, run_status = {}, None, "completed"
    fallback = None if getattr(agent, "route", "") == "fallback" else ops.fallback_agent(service, p, fetch)
    work.route = getattr(agent, "route", "chatgpt")
    tracking["route"] = work.route
    while True:
        try:
            usage = await agent.run(
                INSTRUCTIONS,
                TOOLS,
                work.handle,
                json.dumps(context),
                work.follow_up,
                web_search=not reuse,
                max_turns=3,
                on_usage=observe,
            )
            run_status = "completed"
            break
        except asyncio.CancelledError:
            run_status = "cancelled"
            await asyncio.shield(observe(usage, run_status))
            raise
        except Exception as exc:
            run_status = "failed"
            logging.getLogger(__name__).warning(
                "Preparation on the %s route failed: %s", work.route, type(exc).__name__
            )
            await asyncio.shield(observe(usage, run_status))
            if fallback is not None and await asyncio.to_thread(
                ops.reserve_optional_budget,
                service,
                p,
                request_key + ":fallback",
                METERED_ESTIMATE_PER_LEAD_USD * len(claimed),
                object_id=claimed[0]["lead"]["id"],
                work_kind="preparation",
                scope=scope,
            ):
                # R9.12: the plan limit, a lost login or a failing ChatGPT route hands the same thread, with
                # what it already accepted, to the fallback; every record says which route wrote it.
                agent, fallback, usage = fallback, None, {}
                request_key += ":fallback"
                work.route = tracking["route"] = "fallback"
                tracking["model"] = agent.model
                provider_name = agent.billing
                continue
            if isinstance(exc, PlanLimit):
                await asyncio.to_thread(ops.pause_preparation, service, p, str(exc), PLAN_PAUSE)
                await asyncio.to_thread(
                    ops.finish_thread, service, p, owner, claimed, None, None, release=False, requeue=True
                )
                return {"prepared": 0, "paused": True}
            failure = "The AI thread stopped before finishing: " + type(exc).__name__
            break
    await asyncio.shield(observe(usage, run_status))
    review = await ops.review_thread(service, p, work, owner, tracking) if work.done else {}
    published = await asyncio.to_thread(
        ops.finish_thread, service, p, owner, claimed, work, failure, review=review
    )
    return {"prepared": published, "tool_calls": work.tool_calls, "rejections": work.rejections, **usage}


async def review_thread(service, p, work, owner, tracking=None):
    """One independent meaning review of every written text in the thread. Rejected text is dropped."""
    from app.grounding import accept_review, review_request

    fields, sources = [], []
    for alias, result in work.done.items():
        for block, value in result["written"].items():
            cited = [i for i in value["fact_ids"] if not i.startswith("row:")]
            fields.append({"field": f"{alias}:{block}", "value": value["text"], "evidence": cited})
        bridge = (work.triggers.get(alias) or {}).get("bridge")
        if bridge:
            fields.append(
                {"field": f"{alias}:bridge", "value": bridge["text"], "evidence": [], "inference": True}
            )
        for identity, fact in work.facts[alias].items():
            sources.append({"id": identity, "statement": fact["statement"], "quote": fact["quote"]})
        row = work.leads[alias]["row"]
        sources += [{"id": f"row:{k}", "statement": f"{k}: {v}"} for k, v in row.items() if str(v).strip()]
    if not fields or service.settings.simulation:
        return {}
    provider = await asyncio.to_thread(ops.inference, service, p["tenant_id"], "script_review", p["user_id"])
    if provider is None:
        return {"unreviewed": True}
    rejected = set()
    for start in range(0, len(fields), 100):
        request = review_request("script", fields[start : start + 100], sources[:400])
        from app.model_tokens import capture

        async def observe_review(value, status="running", start=start):
            await asyncio.to_thread(
                capture,
                service,
                p,
                getattr(provider, "billing", "openai"),
                "script_review",
                f"script-review:{owner}:{start}",
                {**(tracking or {}), "model": getattr(provider, "model", None)},
                value,
                status,
            )

        provider.usage_callback = observe_review
        await observe_review({})
        try:
            from app.grounding import REVIEW_INSTRUCTIONS, ReviewOutput

            result = await provider.complete(REVIEW_INSTRUCTIONS, request, ReviewOutput.model_json_schema())
            await observe_review(result.usage, "completed")
            receipt = accept_review(request, result.data, result.request_id, partial=True)
            rejected.update(receipt.rejected)
        except asyncio.CancelledError:
            await asyncio.shield(observe_review({}, "cancelled"))
            raise
        except Exception as exc:
            await observe_review({}, "failed")
            logging.getLogger(__name__).warning("Script review unavailable: %s", type(exc).__name__)
            return {"unreviewed": True}
    return {"rejected": sorted(rejected)}


def pause_preparation(service, p, reason, duration):
    with service.db.transaction() as s:
        seller = ops.required(service, s, "sellers", p["seller_id"], p)
        until = service.clock() + (duration or timedelta(days=3650))
        update(seller, preparation_paused_until=until.isoformat(), preparation_pause_reason=reason)


def resume_preparation(service, p):
    with service.db.transaction() as s:
        seller = ops.required(service, s, "sellers", p["seller_id"], p)
        update(seller, preparation_paused_until=None, preparation_pause_reason=None)
    return ops.prepare_tick(service, p)


def finish_thread(service, p, owner, claimed, work, failure, *, release=False, requeue=False, review=None):
    """Publish accepted scripts and skips; record failures; release leases. Returns scripts published."""
    published, now = 0, service.clock()
    rejected = set((review or {}).get("rejected", []))
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        for item in claimed:
            lead = ops.required(service, s, "leads", item["lead"]["id"], p)
            s.refresh(lead, with_for_update=True)
            lease = lead.data.get("preparation_lease") or {}
            if lease.get("owner") != owner:
                continue
            state = lead.data.get("preparation") or {}
            changes = {"preparation_lease": None}
            current_key = ops.preparation_key(service, s, p, lead)
            alias = work.aliases[lead.id] if work else None
            if requeue:
                changes["preparation"] = {**state, "status": "queued"}
            elif release:
                changes.update(
                    status="eligible" if lead.data["status"] == "preparing" else lead.data["status"]
                )
                changes["preparation"] = {**state, "status": "missing", "error": failure}
            elif current_key != item["key"]:
                changes["preparation"] = {**state, "status": "stale"}
            elif work and alias in work.done:
                changes.update(trigger_changes(work, alias, rejected))
                script = ops.store_prepared_in(service, s, p, lead, work, alias, rejected, review)
                changes["preparation"] = {
                    "status": "ready",
                    "script_id": script.id,
                    "key": item["key"],
                    "as_of": now.isoformat(),
                    "expires_at": script.data["expires_at"],
                }
                if lead.data["status"] == "preparing":
                    changes["status"] = "eligible"
                changes["refresh_research"] = False
                published += 1
            elif work and alias in work.skipped:
                changes.update(trigger_changes(work, alias, rejected))
                ops.store_facts_in(service, s, p, lead, work, alias)
                changes["preparation"] = {
                    "status": "skipped",
                    "key": item["key"],
                    "reason": work.skipped[alias],
                    "as_of": now.isoformat(),
                    "expires_at": (now + timedelta(days=STABLE_DAYS)).isoformat(),
                }
                if lead.data["status"] == "preparing":
                    changes["status"] = "eligible"
            else:
                error = failure or "The AI closed the thread without finishing this lead."
                if work and alias:
                    ops.store_facts_in(service, s, p, lead, work, alias)
                changes["preparation"] = failure_state(state, item["key"], now, error)
                if changes["preparation"]["attempts"] >= MAX_ATTEMPTS and lead.data["status"] == "preparing":
                    # Out of attempts: call with the base script rather than never.
                    changes["status"] = "eligible"
            update(lead, **changes)
            emit(s, p["tenant_id"], "lead.packet_ready", lead, now, {"lead_id": lead.id})
    return published


def store_facts_in(service, s, p, lead, work, alias):
    """Persist this lead's accepted facts; returns thread fact id -> stored fact row."""
    stored, now = {}, service.clock()
    for identity, fact in work.facts[alias].items():
        if fact.get("stored_id"):
            row = get_fact(s, p, fact["stored_id"])
            if row:
                stored[identity] = row
                continue
        source = put(
            s,
            "research_sources",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead.id,
                "url": fact["source_url"],
                "title": (work.pages.get(fact["source_url"]) or {}).get("title", "")[:300],
                "retrieved_at": now.isoformat(),
                "text": (work.pages.get(fact["source_url"]) or {}).get("text", "")[:20000],
                "synthetic": service.settings.simulation,
            },
            now=now,
        )
        fact_id = uid()
        days = STABLE_DAYS if fact.get("stability") == "stable" else TIME_SENSITIVE_DAYS
        stored[identity] = put(
            s,
            "research_facts",
            p["tenant_id"],
            p["seller_id"],
            {
                "fact_id": fact_id,
                "lead_id": lead.id,
                "statement": fact["statement"],
                "source_id": source.id,
                "source_url": fact["source_url"],
                "evidence_quote": fact["quote"],
                "status": "supported",
                "stability": fact.get("stability", "stable"),
                "expires_at": (now + timedelta(days=days)).isoformat(),
                "grounding": {"status": "quote_on_page"},
                "synthetic": service.settings.simulation,
            },
            object_id=fact_id,
            now=now,
        )
    return stored


def store_prepared_in(service, s, p, lead, work, alias, rejected, review):
    stored = ops.store_facts_in(service, s, p, lead, work, alias)
    result, now = work.done[alias], service.clock()
    template = work.template
    instructions = {block["id"]: block["instruction"] for block in template["blocks"]}
    written = {}
    for block, value in result["written"].items():
        if f"{alias}:{block}" in rejected:
            continue
        written[block] = {
            "text": value["text"],
            "fact_ids": [
                i if i.startswith("row:") else stored[i].id
                for i in value["fact_ids"]
                if i.startswith("row:") or i in stored
            ],
        }
    expiry = min(
        [
            dt(row.data["expires_at"])
            for row in stored.values()
            if row.id in {i for v in written.values() for i in v["fact_ids"]}
        ]
        or [now + timedelta(days=STABLE_DAYS)]
    )
    return put(
        s,
        "script_versions",
        p["tenant_id"],
        p["seller_id"],
        {
            "lead_id": lead.id,
            "script_version": uid(),
            "template_version": template_version(template),
            "picks": result["picks"],
            "written": written,
            "fact_ids_used": sorted(
                {i for value in written.values() for i in value["fact_ids"] if not i.startswith("row:")}
            ),
            "instructions": {block: instructions[block] for block in written},
            "dropped_by_review": sorted(b.split(":", 1)[1] for b in rejected if b.startswith(alias + ":")),
            "reviewed": not (review or {}).get("unreviewed") and not service.settings.simulation,
            "summary": work.researched.get(alias, ""),
            "route": getattr(work, "route", "chatgpt"),
            **trigger_changes(work, alias, rejected),
            "expires_at": expiry.isoformat(),
            "created_at": now.isoformat(),
        },
        now=now,
    )


def trigger_changes(work, alias, rejected):
    """The checked trigger and bridge for a lead; a bridge the review found contradicted is dropped."""
    record = work.triggers.get(alias)
    if record is None:
        return {}
    bridge = None if f"{alias}:bridge" in rejected else record["bridge"]
    return {
        "trigger": record["trigger"],
        "bridge": bridge,
        "research_route": getattr(work, "route", "chatgpt"),
    }


def get_fact(s, p, fact_id):
    from app.platform.db import get

    row = get(s, "research_facts", fact_id, p["tenant_id"])
    return row if row and row.seller_id == p["seller_id"] else None
