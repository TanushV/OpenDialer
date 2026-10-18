"""Advance preparation using the existing queue and bounded, versioned lead packets."""

import hashlib
import json
from datetime import timedelta

from sqlalchemy import select

from app.db import MODELS, Job, doc, uid, update
from app.preparation.templates import PreparationPolicy, in_window, policy_values, render, template_version
from app.service import dt


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Preparation:
    def preparation_context(self, s, p):
        key = ("preparation_context", p["tenant_id"], p["seller_id"])
        if key not in s.info:
            seller = self.required(s, "sellers", p["seller_id"], p)
            values = policy_values(seller.data.get("operating_policy", {}))
            playbook = self.approved_playbook(s, p)
            config = self.vault.read(p["tenant_id"])
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
                    config.get("inference_mode", "api"),
                    config.get("script_write_model", config.get("model", "gpt-6-luna")),
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

    def claim_preparation(self, p, lead_id):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            lead = self.required(s, "leads", lead_id, p)
            previous = lead.data.get("preparation_lease") or {}
            if previous.get("expires_at") and dt(previous["expires_at"]) > self.clock():
                return None
            owner = uid()
            update(
                lead,
                preparation_lease={
                    "owner": owner,
                    "expires_at": (self.clock() + timedelta(minutes=5)).isoformat(),
                },
                preparation_generation=lead.data.get("preparation_generation", 0) + 1,
            )
            return owner

    def release_preparation(self, p, lead_id, owner):
        with self.db.transaction() as s:
            lead = self.required(s, "leads", lead_id, p)
            if (lead.data.get("preparation_lease") or {}).get("owner") == owner:
                update(lead, preparation_lease=None)

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

    def selected_script(self, s, p, lead, *, variant_id=None):
        state = self.preparation_state(s, p, lead)
        pointer = state.get("script_id")
        row = s.get(MODELS["script_versions"], pointer) if pointer else None
        if row and (
            row.tenant_id != p["tenant_id"]
            or row.seller_id != p["seller_id"]
            or row.data["lead_id"] != lead.id
        ):
            row = None
        if not pointer:
            # Compatibility reader until the legacy workspace migration is verified.
            row = next(iter(self.context_rows(s, p, "script_versions", lead.id, limit=1)), None)
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
        return script, state

    def preparation_candidates(self, s, p):
        model = MODELS["leads"]
        # Only the next bounded work horizon is inspected; readiness does not
        # decrypt the entire list or historical script versions on a dashboard GET.
        leads = list(
            s.scalars(
                select(model)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.op_status.not_in(["converted", "suppressed", "wrong_party", "invalid"]),
                )
                .order_by(
                    model.op_priority.desc(),
                    model.op_due_at.asc().nullsfirst(),
                    model.op_coverage_at,
                    model.id,
                )
                .limit(500)
            )
        )
        suppressed = {
            r.data["phone"] for r in self.scoped(s, "suppressions", p) if r.data.get("active", True)
        }
        return [
            lead
            for lead in leads
            if lead.data["phone"] not in suppressed and not lead.data.get("outreach_hold")
        ]

    def preparation_status_in(self, s, p):
        leads = self.preparation_candidates(s, p)
        config = self.preparation_settings(s, p)["preparation"]
        active = list(
            s.scalars(
                select(Job).where(
                    Job.tenant_id == p["tenant_id"],
                    Job.queue == "research",
                    Job.status.in_(["queued", "running"]),
                )
            )
        )
        pending = {
            lead_id
            for job in active
            if job.payload.get("seller_id") == p["seller_id"]
            for lead_id in job.payload.get("lead_ids", [])
        }
        states = {lead.id: self.preparation_state(s, p, lead) for lead in leads}
        counts = {
            status: sum(v["status"] == status for v in states.values())
            for status in ("ready", "fallback", "stale", "failed", "missing")
        }
        prepared = counts["ready"] + counts["fallback"]
        return (
            {
                **counts,
                "prepared": prepared,
                "target": config["ahead_count"],
                "queued": len(pending & set(states)),
                "as_of": self.clock().isoformat(),
                "inspected_leads": len(leads),
                "scan_limit": 500,
                "runway_attempts": prepared,
                "runway_note": "Prepared attempts in the next 500 candidates; calling permission is checked separately.",
                "within_schedule": in_window(self.clock(), config),
                "enabled": config["enabled"],
            },
            leads,
            states,
            pending,
        )

    def preparation_status(self, p):
        with self.db.transaction(False) as s:
            return self.preparation_status_in(s, p)[0]

    def prepare_ahead(self, p):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            config = self.preparation_settings(s, p)["preparation"]
            if not config["enabled"] or not in_window(self.clock(), config) or not self.budget_ok(s, p, 0.05):
                return {"queued": 0}
            status, leads, states, pending = self.preparation_status_in(s, p)
            needed = max(0, config["ahead_count"] - status["prepared"] - status["queued"])
            selected = [
                lead
                for lead in leads
                if lead.id not in pending and states[lead.id]["status"] in {"missing", "stale"}
            ][:needed]
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
