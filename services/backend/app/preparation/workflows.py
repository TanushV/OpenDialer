"""Advance preparation using the existing queue and bounded, versioned lead packets."""

import asyncio
import contextlib
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import timedelta

from sqlalchemy import func, select

from app.config import PROVIDER_DEFAULTS, inference_model
from app.db import MODELS, Job, doc, emit, put, uid, update
from app.operational_schema import context_key
from app.preparation.templates import PreparationPolicy, in_window, policy_values, render, template_version
from app.service import dt

LEASE_HEARTBEAT_SECONDS = 20
SCAN_LIMIT = 500
# A failing provider costs at most this many paid attempts per lead and preparation key.
MAX_ATTEMPTS = 3
RETRY_BACKOFF = (timedelta(minutes=5), timedelta(minutes=30))


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
    return state["status"] in {"missing", "stale"} or (state["status"] == "failed" and retry_due(state, now))


def failure_state(previous, key, now, error, *, script_id=None):
    attempts = failed_attempts(previous, key) + 1
    return {
        "status": "failed",
        "key": key,
        # A pointer for an older input could show outdated personalization; the
        # approved generic script is rendered from current lead data instead.
        "script_id": script_id or (previous.get("script_id") if previous.get("key") == key else None),
        "as_of": now.isoformat(),
        "expires_at": None,
        "attempts": attempts,
        "next_attempt_at": (now + RETRY_BACKOFF[attempts - 1]).isoformat()
        if attempts < MAX_ATTEMPTS
        else None,
        "error": error[:200],
    }


def preparation_runway(config, now, prepared, ready_reps):
    """A declared work-rate estimate, without another attempt scheduler."""
    end = now + timedelta(hours=config["horizon_hours"])
    windows = [
        (max(now, dt(w["start_at"])), min(end, dt(w["end_at"])), w)
        for w in config["staffed_windows"]
        if dt(w["end_at"]) > now and dt(w["start_at"]) < end
    ]
    points = sorted({t for start, stop, _ in windows for t in (start, stop)})
    seconds, peak, assumptions = 0.0, 0, []
    for start, stop in zip(points, points[1:], strict=False):
        active = [w for a, b, w in windows if a <= start < b]
        named = {rep for w in active for rep in (w.get("rep_ids") or [])}
        capacity = len(named) + sum(w["reps"] for w in active if not w.get("rep_ids"))
        seconds += (stop - start).total_seconds() * capacity
        peak = max(peak, capacity)
    if not config["staffed_windows"]:
        peak = max(1, ready_reps) if config["rep_minutes"] else 0
        seconds = min(config["rep_minutes"] * 60, (end - now).total_seconds() * peak)
        assumptions.append("single_rep_default" if ready_reps == 0 else "current_ready_roster")
    if not seconds:
        assumptions.append("no_declared_rep_work")
    rate = seconds / (end - now).total_seconds() * 60
    return {
        "available_reps": peak,
        "declared_rep_work_rate": rate,
        "predicted_work_seconds": config["handling_seconds"],
        "runway_minutes": prepared * config["handling_seconds"] / rate if rate else None,
        "horizon_end": end.isoformat(),
        "assumptions": [*assumptions, "configured_attempt_work", "calling_permission_checked_separately"],
    }


class Preparation:
    @asynccontextmanager
    async def preparation_heartbeat(self, p, kind, identity, owner):
        """Renew only a running unit, never queued work or another owner's lease."""

        def renew():
            with self.db.transaction() as s:
                self.db.authorize_stream(s, p)
                row = self.required(s, kind, identity, p)
                s.refresh(row, with_for_update=True)
                if not self._coach_authority(s, p):
                    return False
                if kind == "leads":
                    lease = row.data.get("preparation_lease") or {}
                    if (
                        lease.get("owner") != owner
                        or not lease.get("expires_at")
                        or dt(lease["expires_at"]) <= self.clock()
                    ):
                        return False
                    update(
                        row,
                        preparation_lease={
                            **lease,
                            "expires_at": (self.clock() + timedelta(minutes=5)).isoformat(),
                        },
                    )
                else:
                    if (
                        row.data.get("lease_owner") != owner
                        or not row.data.get("lease_until")
                        or dt(row.data["lease_until"]) <= self.clock()
                    ):
                        return False
                    update(row, lease_until=(self.clock() + timedelta(minutes=3)).isoformat())
                return True

        parent = asyncio.current_task()
        lost = False

        async def heartbeat():
            nonlocal lost
            while True:
                await asyncio.sleep(LEASE_HEARTBEAT_SECONDS)
                try:
                    owned = await asyncio.to_thread(renew)
                except Exception:
                    owned = False
                if not owned:
                    lost = True
                    parent.cancel()
                    return

        pulse = asyncio.create_task(heartbeat())
        try:
            yield
        except asyncio.CancelledError:
            if lost:
                raise RuntimeError("Preparation lease was lost") from None
            raise
        finally:
            pulse.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pulse

    def preparation_context(self, s, p):
        key = ("preparation_context", p["tenant_id"], p["seller_id"])
        if key not in s.info:
            seller = self.required(s, "sellers", p["seller_id"], p)
            values = policy_values(seller.data.get("operating_policy", {}))
            playbook = self.approved_playbook(s, p)
            config = self.configs.read(p["tenant_id"])
            settings = {
                "script_template": values.get("script_template"),
                "preparation": PreparationPolicy(**values.get("preparation", {})).model_dump(),
            }
            s.info[key] = (
                settings,
                [
                    seller.data["name"],
                    settings["script_template"],
                    [playbook.id, playbook.version] if playbook else None,
                    config.get("inference_mode", PROVIDER_DEFAULTS.inference_mode),
                    inference_model(config, "script_write"),
                    bool(config.get("openai_api_key") or config.get("inference_base_url")),
                ],
            )
        return s.info[key]

    def preparation_settings(self, s, p):
        return self.preparation_context(s, p)[0]

    def preparation_key(self, s, p, lead):
        # Operational writes must not invalidate their own cache. Explicit
        # continuity changes do. Shared context is loaded once per transaction.
        return digest(
            [
                self.preparation_context(s, p)[1],
                {
                    key: lead.data.get(key)
                    for key in (
                        "name",
                        "company",
                        "domain",
                        "segment",
                        "timezone",
                        "region",
                        "preparation_revision",
                    )
                },
            ]
        )

    def claim_preparation(self, p, lead_id, *, retry_gate=False):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            lead = self.required(s, "leads", lead_id, p)
            s.refresh(lead, with_for_update=True)
            if not self._coach_authority(s, p):
                return None
            previous = lead.data.get("preparation_lease") or {}
            if previous.get("expires_at") and dt(previous["expires_at"]) > self.clock():
                return None
            state, key = lead.data.get("preparation") or {}, self.preparation_key(s, p, lead)
            if retry_gate and failed_attempts(state, key) and not retry_due(state, self.clock()):
                # Job reruns and repeated ticks wait for the backoff or stop at the cap.
                return None
            owner = uid()
            update(
                lead,
                preparation_lease={
                    "owner": owner,
                    "key": key,
                    "expires_at": (self.clock() + timedelta(minutes=5)).isoformat(),
                },
                preparation_generation=lead.data.get("preparation_generation", 0) + 1,
            )
            return owner

    def release_preparation(self, p, lead_id, owner, failure=None):
        """Release the lease; a failed unit records its attempt in the same write."""
        with self.db.transaction() as s:
            lead = self.required(s, "leads", lead_id, p)
            s.refresh(lead, with_for_update=True)
            lease = lead.data.get("preparation_lease") or {}
            if lease.get("owner") != owner:
                return
            changes = {"preparation_lease": None}
            key = self.preparation_key(s, p, lead)
            # A failure for a superseded input is not charged to the newer key's attempts.
            if failure and lease.get("key", key) == key and self._coach_authority(s, p):
                changes["preparation"] = failure_state(
                    lead.data.get("preparation") or {}, key, self.clock(), failure
                )
            update(lead, **changes)

    def publish_script_in(self, s, p, lead, script, *, key, source_key, facts, failure=None):
        """The one publication step for prepared scripts.

        Callers validate first. An existing version for the same key is reused, the
        lead points at it, and a failure keeps the approved script with its attempt.
        """
        now = self.clock()
        obj = self._by_key(s, p, "script_versions", key)
        if not obj:
            script = {**script, "created_at": now.isoformat()}
            obj = put(
                s,
                "script_versions",
                p["tenant_id"],
                p["seller_id"],
                script,
                object_id=script["script_version"],
                key=key,
                now=now,
            )
        update(
            lead,
            preparation=failure_state(
                lead.data.get("preparation") or {}, source_key, now, failure, script_id=obj.id
            )
            if failure
            else {
                "status": "fallback" if obj.data.get("fallback") else "ready",
                "script_id": obj.id,
                "key": source_key,
                "as_of": now.isoformat(),
                "expires_at": min(
                    (fact["expires_at"] for fact in facts), default=(now + timedelta(days=1)).isoformat()
                ),
            },
        )
        emit(s, p["tenant_id"], "lead.packet_ready", obj, now, {"lead_id": lead.id})
        return obj

    def preparation_state(self, s, p, lead):
        state = dict(lead.data.get("preparation") or {"status": "missing"})
        if state.get("key") != self.preparation_key(s, p, lead) or (
            state.get("expires_at") and dt(state["expires_at"]) <= self.clock()
        ):
            state["status"] = "stale" if state.get("script_id") else "missing"
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
                    lead.data.get("domain"),
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

    def selected_script(self, s, p, lead, *, variant_id=None):
        state = self.preparation_state(s, p, lead)
        row = self.script_row(s, p, lead)
        script = dict(row.data) if row else self.fallback_script(s, p, lead)
        # Validate the script's actual evidence, independently of the latest-facts page.
        facts = self.script_evidence(s, p, lead, script)
        current = self.preparation_settings(s, p)["script_template"]
        stale = script.get("template_version", "legacy-playbook") != template_version(current)
        if row and not script.get("fallback"):
            stale = stale or script.get("grounding", {}).get("status") not in {
                "model_reviewed",
                "approved_template",
            }
        versions = {fact["fact_id"]: fact["version"] for fact in facts}
        stale = stale or any(
            versions.get(key) != version for key, version in script.get("evidence_versions", {}).items()
        )
        if (
            state["status"] == "stale"
            or stale
            or (not set(script.get("fact_ids_used", [])).issubset({f["fact_id"] for f in facts}))
        ):
            script = self.fallback_script(s, p, lead)
            script["prohibited_claim_checks"] = [
                "Prepared context changed or expired; generic script retained"
            ]
            state["status"] = "stale"
        if script.get("template"):
            script.update(
                render(
                    script["template"],
                    lead=doc(lead),
                    seller=self.required(s, "sellers", p["seller_id"], p).data["name"],
                    rep=self.required(s, "users", p["user_id"], p).data["name"],
                    slots=script.get("filled_slots"),
                )
            )
        if variant_id and not current:
            variant = s.get(MODELS["treatment_variants"], variant_id)
            if (
                variant
                and variant.tenant_id == p["tenant_id"]
                and variant.seller_id == p["seller_id"]
                and variant.data.get("approved")
            ):
                rep = self.required(s, "users", p["user_id"], p)
                script.update(
                    opener=variant.data["opener"].replace("{rep}", rep.data["name"]),
                    treatment_variant_id=variant.id,
                )
        if script.get("final_voicemail_asset"):
            campaign = self.required(s, "campaigns", lead.data["campaign_id"], p)
            policy = self.required(s, "policy_versions", campaign.data["policy_id"], p).data
            if not policy.get("voicemail_enabled") or script["final_voicemail_asset"] not in policy.get(
                "approved_assets", []
            ):
                script["final_voicemail_asset"] = None
        return script, state

    def preparation_candidates(self, s, p, horizon_end, wanted):
        """The next `wanted` eligible leads in calling order, read in small pages.

        Readiness inspects the prepared-ahead window, not the whole horizon, so a
        dashboard read or idle tick decrypts tens of rows rather than hundreds.
        """
        model = MODELS["leads"]
        query = (
            select(model)
            .where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.op_status.not_in(["converted", "suppressed", "wrong_party", "invalid"]),
                (model.op_due_at.is_(None) | (model.op_due_at <= horizon_end)),
            )
            .order_by(
                model.op_priority.desc(),
                model.op_due_at.asc().nullsfirst(),
                model.op_coverage_at,
                model.id,
            )
        )
        suppression, eligible, inspected = MODELS["suppressions"], [], 0
        page = max(2 * wanted, 20)
        while wanted and len(eligible) < wanted and inspected < SCAN_LIMIT:
            leads = list(s.scalars(query.offset(inspected).limit(min(page, SCAN_LIMIT - inspected))))
            inspected += len(leads)
            keys = {lead.data["phone"] for lead in leads}
            suppressed = {
                row.data["phone"]
                for row in s.scalars(
                    select(suppression).where(
                        suppression.tenant_id == p["tenant_id"],
                        suppression.seller_id == p["seller_id"],
                        suppression.external_key.in_(keys),
                    )
                )
                if row.data.get("active", True)
            }
            eligible.extend(
                lead
                for lead in leads
                if lead.data["phone"] not in suppressed and not lead.data.get("outreach_hold")
            )
            if len(leads) < page:
                break
        s.info["preparation_inspected"] = inspected
        return eligible[:wanted]

    def pending_preparation(self, s, p):
        """Lead IDs with queued or running preparation for this seller."""
        scope = (
            Job.tenant_id == p["tenant_id"],
            (Job.seller_id == p["seller_id"]) | Job.seller_id.is_(None),
            Job.queue.in_(["research", "scripts"]),
            Job.status.in_(["queued", "running"]),
        )
        return {
            lead_id
            for job in s.scalars(select(Job).where(*scope))
            if job.payload.get("seller_id") == p["seller_id"]
            and (job.queue == "research" or job.payload.get("kind") == "packet_refresh")
            for lead_id in job.payload.get("lead_ids", [])
        }

    def preparation_status_in(self, s, p, *, count=True):
        optimization = self.optimization_config(s, p)
        now = self.clock()
        horizon_end = now + timedelta(hours=optimization["horizon_hours"])
        config = self.preparation_settings(s, p)["preparation"]
        ready_reps = sum(
            not row.data.get("disabled")
            and row.data.get("role") in {"owner", "admin", "rep"}
            and row.data.get("presence", {}).get("status") == "ready"
            and row.data["presence"].get("at") is not None
            and dt(row.data["presence"]["at"]) > now - timedelta(seconds=120)
            for row in self.scoped(s, "users", p)
        )
        staffed = preparation_runway(optimization, now, 0, ready_reps)["available_reps"]
        target = min(config["workspace_ahead_cap"], config["ahead_count"] * staffed)
        leads = self.preparation_candidates(s, p, horizon_end, target)
        pending = self.pending_preparation(s, p)
        states = {lead.id: self.preparation_state(s, p, lead) for lead in leads}
        # Compare the actual used facts in two bounded bulk reads. Reading readiness
        # must not decrypt historical versions or make one query per lead.
        script_model, fact_model = MODELS["script_versions"], MODELS["research_facts"]
        script_ids = {state.get("script_id") for state in states.values()} - {None}
        scripts = {
            row.id: row.data
            for row in (
                s.scalars(
                    select(script_model).where(
                        script_model.tenant_id == p["tenant_id"],
                        script_model.seller_id == p["seller_id"],
                        script_model.id.in_(script_ids),
                    )
                )
                if script_ids
                else []
            )
        }
        fact_ids = {
            identity for script in scripts.values() for identity in script.get("fact_ids_used", [])[:32]
        }
        facts = {
            row.id: row
            for row in (
                s.execute(
                    select(
                        fact_model.id,
                        fact_model.version,
                        fact_model.ctx_key,
                        fact_model.ctx_status,
                        fact_model.ctx_expires_at,
                    ).where(
                        fact_model.tenant_id == p["tenant_id"],
                        fact_model.seller_id == p["seller_id"],
                        fact_model.id.in_(fact_ids),
                        fact_model.ctx_status == "supported",
                        fact_model.ctx_expires_at > now,
                    )
                )
                if fact_ids
                else []
            )
        }
        for lead in leads:
            state = states[lead.id]
            if state["status"] not in {"ready", "fallback"}:
                continue
            script = scripts.get(state.get("script_id"))
            stale = not script or script.get("lead_id") != lead.id
            if script and not script.get("fallback"):
                stale |= script.get("grounding", {}).get("status") not in {
                    "approved_template",
                    "model_reviewed",
                }
            for identity in script.get("fact_ids_used", []) if script else []:
                fact = facts.get(identity)
                stale |= bool(
                    not fact
                    or fact.ctx_key != context_key("research_facts", lead.data.get("domain"))
                    or fact.ctx_status != "supported"
                    or fact.version != script.get("evidence_versions", {}).get(identity)
                )
            if stale:
                state["status"] = "stale"
        counts = {
            status: sum(v["status"] == status for v in states.values())
            for status in ("ready", "fallback", "stale", "failed", "missing")
        }
        prepared = counts["ready"] + counts["fallback"]
        runway = preparation_runway(optimization, now, prepared, ready_reps)
        inspected = s.info["preparation_inspected"]
        omitted = None
        if count:
            # Dashboard reads only; the minute tick never counts the whole horizon.
            model = MODELS["leads"]
            total = s.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.op_status.not_in(["converted", "suppressed", "wrong_party", "invalid"]),
                    (model.op_due_at.is_(None) | (model.op_due_at <= horizon_end)),
                )
            )
            omitted = max(0, total - inspected)
        failed = [v for v in states.values() if v["status"] == "failed"]
        return (
            {
                **counts,
                "prepared": prepared,
                "target": target,
                **runway,
                "queued": len(pending & set(states)),
                "retry_exhausted": sum(failed_attempts(v, v.get("key")) >= MAX_ATTEMPTS for v in failed),
                "retry_due": sum(retry_due(v, now) for v in failed),
                "as_of": now.isoformat(),
                "inspected_leads": len(leads),
                "inspected_rows": inspected,
                "omitted_rows": omitted,
                "scan_limit": SCAN_LIMIT,
                "runway_attempts": prepared,
                "runway_note": f"Prepared attempts among the next {target} callable leads; calling permission is checked separately.",
                "within_schedule": in_window(now, config),
                "enabled": config["enabled"],
            },
            leads,
            states,
            pending,
        )

    def preparation_status(self, p):
        with self.db.transaction(False) as s:
            return self.preparation_status_in(s, p)[0]

    def preparation_plan(self, p):
        """Read-only selection for one tick. Satisfied, disabled or closed ticks write nothing."""
        with self.db.transaction(False) as s:
            config = self.preparation_settings(s, p)["preparation"]
            if not config["enabled"] or not in_window(self.clock(), config):
                return []
            _, leads, states, pending = self.preparation_status_in(s, p, count=False)
            return [
                {"id": lead.id, "version": lead.version}
                for lead in leads
                if lead.id not in pending and needs_preparation(states[lead.id], self.clock())
            ]

    def prepare_ahead(self, p):
        plan = self.preparation_plan(p)
        if not plan:
            return {"queued": 0}
        # The short write claims only the selected candidates, rechecked by version.
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            config = self.preparation_settings(s, p)["preparation"]
            if not config["enabled"] or not in_window(self.clock(), config):
                return {"queued": 0}
            pending = self.pending_preparation(s, p)
            model = MODELS["leads"]
            current = {
                lead.id: lead
                for lead in s.scalars(
                    select(model).where(
                        model.tenant_id == p["tenant_id"],
                        model.seller_id == p["seller_id"],
                        model.id.in_([item["id"] for item in plan]),
                    )
                )
            }
            selected = [
                current[item["id"]]
                for item in plan
                if item["id"] in current
                and current[item["id"]].version == item["version"]
                and item["id"] not in pending
            ]
            jobs = self.queue_preparation_in(s, p, selected, config["batch_size"])
            return {"queued": len(selected), "jobs": jobs}

    def queue_preparation_in(self, s, p, leads, batch_size):
        jobs = []
        # Group nearby domains to reuse one company research result in each work unit.
        leads = sorted(
            {lead.id: lead for lead in leads}.values(),
            key=lambda lead: (lead.data.get("domain", ""), lead.id),
        )
        for offset in range(0, len(leads), batch_size):
            batch = leads[offset : offset + batch_size]
            key = "prepare:" + digest(
                [
                    [
                        lead.id,
                        self.preparation_key(s, p, lead),
                        lead.data.get("preparation", {}).get("expires_at"),
                        lead.data.get("preparation_generation", 0),
                    ]
                    for lead in batch
                ]
            )
            jobs.append(
                self.jobs.enqueue_in(
                    s,
                    "research",
                    {
                        "lead_ids": [lead.id for lead in batch],
                        "seller_id": p["seller_id"],
                        "user_id": p["user_id"],
                    },
                    key,
                    tenant_id=p["tenant_id"],
                )
            )
        return jobs
