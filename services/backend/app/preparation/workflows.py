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

from app.db import MODELS, Job, doc, emit, put, uid, update
from app.preparation.agent import INSTRUCTIONS, TOOLS, SimulatedAgent, ThreadWork, simulated_page
from app.preparation.templates import PreparationPolicy, render, template_version
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


class Preparation:
    def preparation_settings(self, s, p):
        key = ("preparation_settings", p["tenant_id"], p["seller_id"])
        if key not in s.info:
            seller = self.required(s, "sellers", p["seller_id"], p)
            stored = seller.data.get("operating_policy", {}).get("preparation", {})
            s.info[key] = {"preparation": PreparationPolicy(**stored).model_dump()}
        return s.info[key]

    def script_base(self, s, p, campaign):
        """The campaign's adopted script, or none yet (version 0)."""
        stored = campaign.data.get("script") or {}
        template = stored.get("template")
        if template and template.get("format") == 2:
            return {k: stored.get(k) for k in ("version", "template", "adopted_at", "source")}
        return {"version": stored.get("version", 0), "template": None, "adopted_at": None, "source": None}

    def lead_script_base(self, s, p, lead):
        return self.script_base(s, p, self.required(s, "campaigns", lead.data["campaign_id"], p))

    def preparation_key(self, s, p, lead):
        """What a prepared script depends on: the base script, the lead's row and the script memory."""
        campaign = self.required(s, "campaigns", lead.data["campaign_id"], p)
        base = self.script_base(s, p, campaign)
        return digest(
            [
                template_version(base["template"]),
                lead.data.get("row") or {k: lead.data.get(k) for k in ("name", "company", "domain")},
                campaign.data.get("script_memory_key"),
            ]
        )

    def agent_route(self, p):
        """The agent that prepares leads for this admin, or None when no AI is connected."""
        if self.settings.simulation:
            return SimulatedAgent()
        provider = self.inference(p["tenant_id"], "script_write", p["user_id"])
        return provider if provider and hasattr(provider, "run") else None

    def fallback_script(self, s, p, lead):
        """The base script with the seller's default options and no AI text."""
        template = self.lead_script_base(s, p, lead)["template"]
        seller = self.required(s, "sellers", p["seller_id"], p).data["name"]
        rep = self.required(s, "users", p["user_id"], p).data["name"]
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
            playbook = self.approved_playbook(s, p)
            opener = (
                playbook.data["opener"] if playbook else "Hi, this is {rep} from " + seller + "."
            ).replace("{rep}", rep)
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
                objection_cards=playbook.data.get("cards", []) if playbook else [],
                picks={},
                written={},
            )
        return script

    def preparation_state(self, s, p, lead):
        state = dict(lead.data.get("preparation") or {"status": "missing"})
        if state.get("status") in {"ready", "skipped"} and (
            state.get("key") != self.preparation_key(s, p, lead)
            or (state.get("expires_at") and dt(state["expires_at"]) <= self.clock())
        ):
            state["status"] = "stale"
        return state

    def script_evidence(self, s, p, lead, script):
        model = MODELS["research_facts"]
        return (
            [
                doc(row)
                for row in self.context_rows(
                    s,
                    p,
                    "research_facts",
                    lead.id,
                    model.id.in_(script.get("fact_ids_used", [])[:32]),
                    model.ctx_status == "supported",
                    model.ctx_expires_at > self.clock(),
                    limit=32,
                )
            ]
            if script.get("fact_ids_used")
            else []
        )

    def script_row(self, s, p, lead):
        pointer = (lead.data.get("preparation") or {}).get("script_id")
        row = s.get(MODELS["script_versions"], pointer) if pointer else None
        if row and (
            row.tenant_id != p["tenant_id"]
            or row.seller_id != p["seller_id"]
            or row.data["lead_id"] != lead.id
        ):
            row = None
        return row

    def selected_script(self, s, p, lead, *, assignment=None):
        """The script a rep sees for this lead: its prepared picks and text over the current base."""
        state = self.preparation_state(s, p, lead)
        row = self.script_row(s, p, lead)
        script = self.fallback_script(s, p, lead)
        template = script["template"]
        proposal, arm = None, None
        if assignment and assignment.get("family") == "script":
            testing = self.testing_proposal(s, p, lead.data["campaign_id"], assignment["comparison_id"])
            if testing:
                arm = assignment.get("arm")
                proposal = testing if arm == "candidate" else None
                template = testing["template" if proposal else "control_template"]
        prepared = dict(row.data) if row else None
        if prepared and template:
            # Prepared text stays usable after a script edit wherever its blocks and options still
            # exist; anything that no longer matches falls back to the seller's defaults.
            facts = {fact["fact_id"]: fact for fact in self.script_evidence(s, p, lead, prepared)}
            blocks = {block["id"]: block["instruction"] for block in template["blocks"]}
            written = {
                block: value
                for block, value in prepared.get("written", {}).items()
                if prepared.get("instructions", {}).get(block) == blocks.get(block)
                and all(i.startswith("row:") or i in facts for i in value.get("fact_ids", []))
            }
            options = {
                section["key"]: {o["id"] for o in section["options"]} for section in template["sections"]
            }
            picks = {
                key: ids
                for key, ids in prepared.get("picks", {}).items()
                if key in options and set(ids) <= options[key]
            }
            seller = self.required(s, "sellers", p["seller_id"], p).data["name"]
            rep = self.required(s, "users", p["user_id"], p).data["name"]
            used = sorted(
                {
                    i
                    for value in written.values()
                    for i in value.get("fact_ids", [])
                    if not i.startswith("row:")
                }
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
            seller = self.required(s, "sellers", p["seller_id"], p).data["name"]
            rep = self.required(s, "users", p["user_id"], p).data["name"]
            script.update(render(template, lead=doc(lead), seller=seller, rep=rep))
        base = self.lead_script_base(s, p, lead)
        script.update(
            template=template,
            base_version=base["version"],
            script_arm=arm,
            proposal_id=proposal["id"] if proposal else None,
            preparation_status=state["status"],
        )
        return script, state

    def pending_preparation(self, s, p, finishing=None):
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

    def running_threads(self, s, p, finishing=None):
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

    def request_preparation_in(self, s, p, leads):
        """Mark leads to be prepared before they can be called. Returns how many were marked."""
        marked = 0
        for lead in leads:
            state = lead.data.get("preparation") or {}
            if lead.data.get("status") not in {"eligible", "preparing"}:
                continue
            if lead.data.get("prepare_requested") and state.get("status") != "failed":
                continue  # Already requested; a failed lead is asked again with fresh attempts.
            if self.suppressed(s, p, lead.data["phone"]):
                continue  # An opted-out number is never researched or scripted.
            update(
                lead,
                prepare_requested=True,
                status="preparing"
                if state.get("status") not in {"ready", "skipped"}
                else lead.data["status"],
                preparation={**state, "status": "queued", "attempts": 0, "next_attempt_at": None}
                if state.get("status") not in {"ready", "skipped"}
                else state,
            )
            marked += 1
        return marked

    def prepare_list(self, p, list_name=None, lead_ids=None):
        """Prepare a whole list (or the given leads) before calling."""
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            if not self.lead_script_base_ready(s, p):
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
                if list_name is None or lead_ids is not None or lead.data.get("list_name", "") == list_name
            ]
            marked = self.request_preparation_in(s, p, leads)
            update(self.required(s, "sellers", p["seller_id"], p), preparation_user_id=p["user_id"])
        self.prepare_tick(p)
        return {"marked": marked}

    def lead_script_base_ready(self, s, p):
        campaign = self.scoped(s, "campaigns", p)[0]
        return bool(self.script_base(s, p, campaign)["template"])

    def release_list(self, p, list_name):
        """Call a list's unprepared leads now with the base script (after a budget stop, say)."""
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            model = MODELS["leads"]
            released = 0
            pending = self.pending_preparation(s, p)
            for lead in s.scalars(
                select(model).where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.op_status == "preparing",
                )
            ):
                if lead.data.get("list_name", "") == list_name and lead.id not in pending:
                    update(lead, status="eligible", prepare_requested=False)
                    released += 1
            return {"released": released}

    def prepare_tick(self, p, finishing=None):
        """Queue threads for open leads, keeping at most `agents_at_once` running.

        A thread that just finished passes its own job id, so it does not count against the limit."""
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            seller = self.required(s, "sellers", p["seller_id"], p)
            paused = seller.data.get("preparation_paused_until")
            if paused and dt(paused) > self.clock():
                return {"queued": 0, "paused_until": paused}
            config = self.preparation_settings(s, p)["preparation"]
            room = config["agents_at_once"] - self.running_threads(s, p, finishing)
            if room <= 0 or not self.lead_script_base_ready(s, p):
                return {"queued": 0}
            pending = self.pending_preparation(s, p, finishing)
            model = MODELS["leads"]
            now = self.clock()
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
                    if needs_preparation(self.preparation_state(s, p, lead), now):
                        candidates.append(lead)
                        if len(candidates) >= room * config["leads_per_agent"]:
                            break
            if not candidates:
                return {"queued": 0}
            jobs = self.queue_threads_in(s, p, candidates, config["leads_per_agent"], room)
            return {"queued": sum(len(job["lead_ids"]) for job in jobs), "threads": len(jobs)}

    def queue_threads_in(self, s, p, leads, size, room):
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
                update(
                    lead, preparation={**state, "status": "queued"} if state.get("status") in OPEN else state
                )
            payload = {
                "kind": "prep_thread",
                "lead_ids": [lead.id for lead in batch],
                "seller_id": p["seller_id"],
                # Threads run as the person who asked for preparation: their ChatGPT login.
                "user_id": self.required(s, "sellers", p["seller_id"], p).data.get("preparation_user_id")
                or p["user_id"],
            }
            key = "prepare:" + digest(
                [[lead.id, self.preparation_key(s, p, lead)] for lead in batch] + [uid()]
            )
            self.jobs.enqueue_in(s, "research", payload, key, tenant_id=p["tenant_id"])
            jobs.append(payload)
        return jobs

    def preparation_status(self, p):
        """Progress per lead list, as the Leads page and the Dialer show it."""
        with self.db.transaction(False) as s:
            model = MODELS["leads"]
            lists, now = {}, self.clock()
            pending = self.pending_preparation(s, p)
            for lead in s.scalars(
                select(model).where(model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"])
            ):
                if not lead.data.get("prepare_requested"):
                    continue
                entry = lists.setdefault(
                    lead.data.get("list_name", ""),
                    {
                        "list_name": lead.data.get("list_name", ""),
                        "total": 0,
                        "ready": 0,
                        "skipped": 0,
                        "failed": 0,
                        "working": 0,
                        "waiting": 0,
                    },
                )
                entry["total"] += 1
                state = self.preparation_state(s, p, lead)
                if lead.id in pending:
                    entry["working"] += 1
                elif state["status"] in {"ready", "skipped"}:
                    entry[state["status"]] += 1
                elif state["status"] == "failed" and not retry_due(state, now):
                    entry["failed"] += 1
                else:
                    entry["waiting"] += 1
            seller = self.required(s, "sellers", p["seller_id"], p)
            paused = seller.data.get("preparation_paused_until")
            config = self.preparation_settings(s, p)["preparation"]
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
            budget = self.scoped(s, "budgets", p)[0].data
            for entry in lists.values():
                entry["spent_usd"] = self.scope_spend(s, p, "list:" + entry["list_name"])
                entry["budget_usd"] = budget.get("per_list_usd", 25.0)
            return {
                "lists": sorted(lists.values(), key=lambda e: e["list_name"]),
                "threads": threads,
                "paused_until": paused if paused and dt(paused) > now else None,
                "pause_reason": seller.data.get("preparation_pause_reason")
                if paused and dt(paused) > now
                else None,
                "settings": config,
                "has_script": self.lead_script_base_ready(s, p),
                "ai_connected": self.agent_route(p) is not None,
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

    def claim_thread(self, p, lead_ids):
        """Lease each lead for this thread; leads already prepared for the current input are left out."""
        owner, claimed = uid(), []
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            for lead_id in lead_ids:
                lead = self.required(s, "leads", lead_id, p)
                s.refresh(lead, with_for_update=True)
                lease = lead.data.get("preparation_lease") or {}
                if lease.get("expires_at") and dt(lease["expires_at"]) > self.clock():
                    continue
                state = self.preparation_state(s, p, lead)
                if not needs_preparation(state, self.clock()):
                    continue
                key = self.preparation_key(s, p, lead)
                update(
                    lead,
                    preparation_lease={
                        "owner": owner,
                        "key": key,
                        "expires_at": (self.clock() + timedelta(hours=2)).isoformat(),
                    },
                )
                facts = [
                    doc(row)
                    for row in self.context_rows(
                        s,
                        p,
                        "research_facts",
                        lead.id,
                        MODELS["research_facts"].ctx_status == "supported",
                        MODELS["research_facts"].ctx_expires_at > self.clock(),
                        limit=12,
                    )
                ]
                claimed.append({"lead": doc(lead), "key": key, "facts": facts})
            campaign = self.scoped(s, "campaigns", p)[0]
            template = self.script_base(s, p, campaign)["template"]
            memory = self.script_memory_digest_in(s, p, campaign) if template else None
        return owner, claimed, template, memory

    async def run_prep_thread(self, p, lead_ids, job_id=None):
        """Research the thread's leads, write their scripts, then publish or record each outcome."""
        from app.providers.codex import PlanLimit

        owner, claimed, template, memory = await asyncio.to_thread(self.claim_thread, p, lead_ids)
        if not claimed:
            return {"prepared": 0}
        agent = await asyncio.to_thread(self.agent_route, p)
        if not template or agent is None:
            await asyncio.to_thread(
                self.finish_thread, p, owner, claimed, None, "AI or script unavailable", release=True
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
            }
            for item in claimed
        ]
        if self.settings.simulation:
            fetch = simulated_page([lead["row"] for lead in leads])
        else:
            from app.config import LIMITS
            from app.research.fetch import SafeFetcher

            async def fetch(url):
                return await SafeFetcher(max_bytes=LIMITS.research_bytes).fetch(url)

        work = ThreadWork(leads, template, fetch)
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
        scope = "list:" + (claimed[0]["lead"].get("list_name") or "")
        if metered and not await asyncio.to_thread(
            self.reserve_optional_budget,
            p,
            request_key,
            METERED_ESTIMATE_PER_LEAD_USD * len(claimed),
            object_id=claimed[0]["lead"]["id"],
            work_kind="preparation",
            scope=scope,
        ):
            await asyncio.to_thread(self.pause_preparation, p, "This list reached its AI budget.", None)
            await asyncio.to_thread(
                self.finish_thread, p, owner, claimed, None, None, release=False, requeue=True
            )
            return {"prepared": 0, "budget": "reached"}
        context = work.context(memory)
        if any(item["facts"] for item in claimed):
            context["known_facts"] = {
                alias: [{"id": i, "statement": f["statement"]} for i, f in facts.items()]
                for alias, facts in work.facts.items()
                if facts
            }
        usage, failure = {}, None
        try:
            usage = await agent.run(
                INSTRUCTIONS,
                TOOLS,
                work.handle,
                json.dumps(context),
                work.follow_up,
                web_search=True,
                max_turns=3,
            )
        except PlanLimit as exc:
            await asyncio.to_thread(self.pause_preparation, p, str(exc), PLAN_PAUSE)
            await asyncio.to_thread(
                self.finish_thread, p, owner, claimed, None, None, release=False, requeue=True
            )
            return {"prepared": 0, "paused": True}
        except Exception as exc:
            logging.getLogger(__name__).warning("Preparation thread failed: %s", type(exc).__name__)
            failure = "The AI thread stopped before finishing: " + type(exc).__name__
        finally:
            if metered:
                from app.providers.inference import InferenceResult

                await asyncio.to_thread(
                    self.usage,
                    p,
                    getattr(agent, "billing", "openai"),
                    "preparation",
                    InferenceResult({}, usage or {}, None),
                    object_id=claimed[0]["lead"]["id"],
                    request_key=request_key,
                    reserved=0,
                )
        review = await self.review_thread(p, work, owner) if work.done else {}
        published = await asyncio.to_thread(
            self.finish_thread, p, owner, claimed, work, failure, review=review
        )
        return {"prepared": published, "tool_calls": work.tool_calls, "rejections": work.rejections, **usage}

    async def review_thread(self, p, work, owner):
        """One independent meaning review of every written text in the thread. Rejected text is dropped."""
        from app.grounding import accept_review, review_request

        fields, sources = [], []
        for alias, result in work.done.items():
            for block, value in result["written"].items():
                cited = [i for i in value["fact_ids"] if not i.startswith("row:")]
                fields.append({"field": f"{alias}:{block}", "value": value["text"], "evidence": cited})
            for identity, fact in work.facts[alias].items():
                sources.append({"id": identity, "statement": fact["statement"], "quote": fact["quote"]})
            row = work.leads[alias]["row"]
            sources += [
                {"id": f"row:{k}", "statement": f"{k}: {v}"} for k, v in row.items() if str(v).strip()
            ]
        if not fields or self.settings.simulation:
            return {}
        provider = await asyncio.to_thread(self.inference, p["tenant_id"], "script_review", p["user_id"])
        if provider is None:
            return {"unreviewed": True}
        rejected = set()
        for start in range(0, len(fields), 100):
            request = review_request("script", fields[start : start + 100], sources[:400])
            try:
                from app.grounding import REVIEW_INSTRUCTIONS, ReviewOutput

                result = await provider.complete(
                    REVIEW_INSTRUCTIONS, request, ReviewOutput.model_json_schema()
                )
                receipt = accept_review(request, result.data, result.request_id, partial=True)
                rejected.update(receipt.rejected)
            except Exception as exc:
                logging.getLogger(__name__).warning("Script review unavailable: %s", type(exc).__name__)
                return {"unreviewed": True}
        return {"rejected": sorted(rejected)}

    def pause_preparation(self, p, reason, duration):
        with self.db.transaction() as s:
            seller = self.required(s, "sellers", p["seller_id"], p)
            until = self.clock() + (duration or timedelta(days=3650))
            update(seller, preparation_paused_until=until.isoformat(), preparation_pause_reason=reason)

    def resume_preparation(self, p):
        with self.db.transaction() as s:
            seller = self.required(s, "sellers", p["seller_id"], p)
            update(seller, preparation_paused_until=None, preparation_pause_reason=None)
        return self.prepare_tick(p)

    def finish_thread(self, p, owner, claimed, work, failure, *, release=False, requeue=False, review=None):
        """Publish accepted scripts and skips; record failures; release leases. Returns scripts published."""
        published, now = 0, self.clock()
        rejected = set((review or {}).get("rejected", []))
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            for item in claimed:
                lead = self.required(s, "leads", item["lead"]["id"], p)
                s.refresh(lead, with_for_update=True)
                lease = lead.data.get("preparation_lease") or {}
                if lease.get("owner") != owner:
                    continue
                state = lead.data.get("preparation") or {}
                changes = {"preparation_lease": None}
                current_key = self.preparation_key(s, p, lead)
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
                    script = self.store_prepared_in(s, p, lead, work, alias, rejected, review)
                    changes["preparation"] = {
                        "status": "ready",
                        "script_id": script.id,
                        "key": item["key"],
                        "as_of": now.isoformat(),
                        "expires_at": script.data["expires_at"],
                    }
                    if lead.data["status"] == "preparing":
                        changes["status"] = "eligible"
                    published += 1
                elif work and alias in work.skipped:
                    self.store_facts_in(s, p, lead, work, alias)
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
                        self.store_facts_in(s, p, lead, work, alias)
                    changes["preparation"] = failure_state(state, item["key"], now, error)
                    if (
                        changes["preparation"]["attempts"] >= MAX_ATTEMPTS
                        and lead.data["status"] == "preparing"
                    ):
                        # Out of attempts: call with the base script rather than never.
                        changes["status"] = "eligible"
                update(lead, **changes)
                emit(s, p["tenant_id"], "lead.packet_ready", lead, now, {"lead_id": lead.id})
        return published

    def store_facts_in(self, s, p, lead, work, alias):
        """Persist this lead's accepted facts; returns thread fact id -> stored fact row."""
        stored, now = {}, self.clock()
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
                    "synthetic": self.settings.simulation,
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
                    "synthetic": self.settings.simulation,
                },
                object_id=fact_id,
                now=now,
            )
        return stored

    def store_prepared_in(self, s, p, lead, work, alias, rejected, review):
        stored = self.store_facts_in(s, p, lead, work, alias)
        result, now = work.done[alias], self.clock()
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
                "dropped_by_review": sorted(
                    b.split(":", 1)[1] for b in rejected if b.startswith(alias + ":")
                ),
                "reviewed": not (review or {}).get("unreviewed") and not self.settings.simulation,
                "summary": work.researched.get(alias, ""),
                "expires_at": expiry.isoformat(),
                "created_at": now.isoformat(),
            },
            now=now,
        )


def get_fact(s, p, fact_id):
    from app.db import get

    row = get(s, "research_facts", fact_id, p["tenant_id"])
    return row if row and row.seller_id == p["seller_id"] else None
