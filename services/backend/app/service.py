import asyncio
import base64
import hashlib
import hmac
import json
import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.compliance.phone import normalize_phone
from app.compliance.policy import CallContext, authorize
from app.db import MODELS, ActiveClaim, audit, doc, emit, get, put, rows, uid, update
from app.jobs.queue import JobQueue
from app.optimization.numbering import select_number
from app.privacy import event_metadata
from app.providers.simulated import SimulatedCarrier
from app.providers.telnyx import TelnyxCarrier
from app.scheduling.queue import ReservationPlanner
from app.security import PASSWORDS, Vault, issue_session
from app.telephony.reducer import TERMINAL, owns_effect, reduce_state


def dt(value):
    return datetime.fromisoformat(value) if value else None


def fail(status, message):
    raise HTTPException(status, message)


class Service(ReservationPlanner):
    def __init__(self, db, settings, clock, carrier=None):
        self.db, self.settings, self.clock = db, settings, clock
        self.injected_carrier = carrier
        self.simulated_carrier = carrier or SimulatedCarrier()
        self.vault = Vault(settings.data_dir, settings.secret_key)
        self.ingress_lock = threading.Lock()
        self.ingress_challenges = {}
        self.capability_cache = {}
        self.jobs = JobQueue(db, clock)
        from app.privacy import EphemeralBuffer

        self.raw = EphemeralBuffer(clock=clock)
        from app.coaching.engine import CoachEngine

        self.coach = CoachEngine(settings.runtime_limits)
        self.preparation_capacity = asyncio.Semaphore(settings.runtime_limits.company_units)
        self.research_capacity = asyncio.Semaphore(settings.runtime_limits.company_units)
        self.script_capacity = asyncio.Semaphore(settings.runtime_limits.script_generations)
        self.screening_capacity = asyncio.Semaphore(2)
        self.research_flights = {}
        limits = settings.runtime_limits
        self.job_admission = {
            name: asyncio.Semaphore(capacity)
            for name, capacity in {
                "webhook": limits.live_active,
                "live_coach": limits.live_active,
                "postcall": limits.postcall_active,
                "preparation": limits.company_units,
                "ml": 1,
                "training": 1,
            }.items()
        }

    def required(self, s, kind, object_id, p):
        obj = get(s, kind, object_id, p["tenant_id"])
        if not obj or (obj.seller_id and obj.seller_id != p["seller_id"]):
            fail(404, "Object not found")
        if p.get("role") == "rep" and kind == "call_attempts" and obj.data.get("rep_id") != p["user_id"]:
            fail(404, "Object not found")
        if p.get("role") == "rep" and kind in {"notes", "scorecards"}:
            call = get(s, "call_attempts", obj.data.get("call_id"), p["tenant_id"])
            if not call or call.data["rep_id"] != p["user_id"]:
                fail(404, "Object not found")
        return obj

    def scoped(self, s, kind, p):
        return rows(s, kind, p["tenant_id"], p["seller_id"])

    def _by_key(self, s, p, kind, key):
        model = MODELS[kind]
        return s.scalar(
            select(model).where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.external_key == key,
            )
        )

    def context_rows(self, s, p, kind, value, *conditions, limit=100):
        from app.operational_schema import context_key

        if not value:
            return []
        model = MODELS[kind]
        return list(
            s.scalars(
                select(model)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.ctx_key == context_key(kind, value, self.db.identity_key),
                    *conditions,
                )
                .order_by(model.created_at.desc(), model.id.desc())
                .limit(limit)
            )
        )

    def carrier(self, tenant_id):
        if self.settings.simulation:
            return self.simulated_carrier
        conf = self.vault.read(tenant_id)
        if not conf.get("telnyx_api_key") or not conf.get("telnyx_connection_id"):
            fail(503, "TELNYX_NOT_CONFIGURED")
        return TelnyxCarrier(
            conf["telnyx_api_key"],
            conf["telnyx_connection_id"],
            webhook_url=conf.get("webhook_url", self.settings.webhook_public_url),
            http=getattr(self, "control_http", None),
        )

    def setup(self, data, *, first=True):
        now = self.clock()
        with self.db.transaction() as s:
            if first and rows(s, "tenants"):
                fail(409, "Workspace already initialized")
            if any(u.data["email"].casefold() == data["email"].casefold() for u in rows(s, "users")):
                fail(409, "Email already belongs to a workspace")
            tenant_id, seller_id, campaign_id, policy_id = uid(), uid(), uid(), uid()
            put(s, "tenants", tenant_id, "", {"name": data["seller_name"]}, object_id=tenant_id, now=now)
            put(
                s,
                "sellers",
                tenant_id,
                seller_id,
                {"name": data["seller_name"], "approved": False},
                object_id=seller_id,
                now=now,
            )
            user = put(
                s,
                "users",
                tenant_id,
                seller_id,
                {
                    "name": data.get("name", "Local operator"),
                    "email": data["email"].casefold(),
                    "password_hash": PASSWORDS.hash(data["password"]),
                    "role": "owner",
                    "platform_admin": first,
                    "disabled": False,
                    "presence": {"status": "paused", "audio_ready": False},
                },
                now=now,
            )
            put(
                s,
                "memberships",
                tenant_id,
                seller_id,
                {"user_id": user.id, "role": "owner"},
                key=user.id,
                now=now,
            )
            put(
                s,
                "campaigns",
                tenant_id,
                seller_id,
                {
                    "name": data.get("campaign_name", "My campaign"),
                    "policy_id": policy_id,
                    "created_at": now.isoformat(),
                },
                object_id=campaign_id,
                now=now,
            )
            put(
                s,
                "policy_versions",
                tenant_id,
                seller_id,
                {
                    "campaign_id": campaign_id,
                    "approved": False,
                    "registry_required": True,
                    "state_registry_required": False,
                    "permissions": ["live"],
                    "local_start_hour": 9,
                    "local_end_hour": 20,
                    "max_attempts": 4,
                    "min_retry_seconds": 3600,
                    "allowed_country_codes": ["1"],
                    "voicemail_enabled": False,
                    "approved_assets": [],
                    "detached_topology_verified": False,
                    "review_reference": "",
                    "synthetic": False,
                },
                object_id=policy_id,
                now=now,
            )
            put(
                s,
                "budgets",
                tenant_id,
                seller_id,
                {"daily_limit_usd": 10, "monthly_limit_usd": 100, "number_purchase_limit_usd": 0},
                key="budget",
                now=now,
            )
            put(
                s,
                "playbooks",
                tenant_id,
                seller_id,
                {
                    "name": "Truthful introduction",
                    "approved": True,
                    "version_label": "v1",
                    "claims": [],
                    "cards": [],
                    "opener": f"Hi, this is {{rep}} from {data['seller_name']}. Is this a reasonable time for a brief introduction?",
                    "screening_intro": f"{{rep}} from {data['seller_name']}, calling with a brief business introduction.",
                },
                key="approved",
                now=now,
            )
            audit(s, tenant_id, seller_id, user.id, "workspace.created", tenant_id, now)
            return {
                **issue_session(s, self.settings, user, now),
                "campaign_id": campaign_id,
                "policy_id": policy_id,
            }

    def seed(self, p):
        if not self.settings.simulation:
            fail(403, "Simulation is disabled for this authority")
        now = self.clock()
        with self.db.transaction() as s:
            seller = self.required(s, "sellers", p["seller_id"], p)
            update(seller, approved=True, synthetic=True)
            book = self.scoped(s, "playbooks", p)[0]
            if not book.data.get("cards"):
                update(
                    book,
                    synthetic=True,
                    cards=[
                        {
                            "id": "existing-provider",
                            "tags": ["provider", "objection"],
                            "text": "What would need to improve before you would consider changing providers?",
                        },
                        {
                            "id": "budget",
                            "tags": ["budget", "price", "objection"],
                            "text": "What outcome would make the investment worthwhile?",
                        },
                        {
                            "id": "timing",
                            "tags": ["timing", "callback"],
                            "text": "When would a brief follow-up be useful?",
                        },
                        {
                            "id": "next-step",
                            "tags": ["next_step", "meeting"],
                            "text": "What would you need to see in a short follow-up meeting?",
                        },
                        {
                            "id": "discovery",
                            "tags": ["discovery"],
                            "text": "What would make this conversation useful for you?",
                        },
                    ],
                )
            campaign = self.scoped(s, "campaigns", p)[0]
            policy = self.required(s, "policy_versions", campaign.data["policy_id"], p)
            update(
                policy,
                approved=True,
                synthetic=True,
                permissions=["live", "transcription"],
                review_reference="SIMULATION ONLY",
            )
            if not self.scoped(s, "registry_snapshots", p):
                put(
                    s,
                    "registry_snapshots",
                    p["tenant_id"],
                    p["seller_id"],
                    {
                        "kind": "national",
                        "npas": ["415", "212", "512"],
                        "refreshed_at": now.isoformat(),
                        "listed_numbers": [],
                        "synthetic": True,
                        "checksum": hashlib.sha256(b"synthetic-empty-registry").hexdigest(),
                        "subscription_reference": "SIMULATED",
                        "published": True,
                    },
                    now=now,
                )
            for phone, region in [("+14155550100", "415"), ("+12125550100", "212")]:
                if not any(n.data["phone"] == phone for n in self.scoped(s, "number_inventory", p)):
                    put(
                        s,
                        "number_inventory",
                        p["tenant_id"],
                        p["seller_id"],
                        {
                            "phone": phone,
                            "region": region,
                            "lifecycle": "healthy",
                            "capacity": 1,
                            "daily_limit": 100,
                            "identity_configured": True,
                            "callback_tested": True,
                            "registration_reference": "SIMULATION",
                            "synthetic": True,
                        },
                        key=phone,
                        now=now,
                    )
        if not self.leads(p):
            self.import_leads(
                p,
                {
                    "campaign_id": campaign.id,
                    "rows": [
                        {
                            "name": "Jordan Ellis",
                            "phone": "+14155550101",
                            "company": "Northstar Example",
                            "domain": "northstar.example.test",
                            "timezone": "America/Los_Angeles",
                            "timezone_confidence": "confirmed",
                            "region": "415",
                        },
                        {
                            "name": "Morgan Reed",
                            "phone": "+12125550102",
                            "company": "Harbor Example",
                            "domain": "harbor.example.test",
                            "timezone": "America/New_York",
                            "timezone_confidence": "confirmed",
                            "region": "212",
                        },
                        {
                            "name": "Casey Nguyen",
                            "phone": "+15125550103",
                            "company": "Cedar Example",
                            "domain": "cedar.example.test",
                            "timezone": "America/Chicago",
                            "timezone_confidence": "confirmed",
                            "region": "512",
                        },
                    ],
                },
            )
        return {"campaign_id": campaign.id, "policy_id": policy.id, "simulation": True}

    def suppressed(self, s, p, phone):
        cache = s.info.get("row_cache", {}).get(("suppressions", p["tenant_id"], p["seller_id"]))
        if cache is None:
            model = MODELS["suppressions"]
            key = hmac.new(self.db.identity_key, phone.encode(), hashlib.sha256).hexdigest()
            row = s.scalar(
                select(model).where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.external_key == key,
                )
            )
            cache = [row] if row else []
        return any(
            r.data["phone"] == phone and r.data.get("active", True) and r not in s.deleted for r in cache
        )

    def import_leads(self, p, body):
        result = {"imported": 0, "duplicates": 0, "invalid": 0, "errors": [], "leads": []}
        if len(body["rows"]) > self.settings.max_import_rows:
            fail(413, "Import row limit exceeded")
        now = self.clock()
        with self.db.transaction() as s:
            self.required(s, "campaigns", body["campaign_id"], p)
            endpoints = {r.data["phone"]: r for r in self.scoped(s, "phone_endpoints", p)}
            existing = {(r.data["campaign_id"], r.data["endpoint_id"]) for r in self.scoped(s, "leads", p)}
            for index, raw in enumerate(body["rows"]):
                phone = normalize_phone(str(raw.get("phone", "")))
                if not phone.e164:
                    result["invalid"] += 1
                    result["errors"].append({"row": index + 1, "reason": "INVALID_PHONE"})
                    continue
                endpoint = endpoints.get(phone.e164)
                if not endpoint:
                    endpoint = put(
                        s,
                        "phone_endpoints",
                        p["tenant_id"],
                        p["seller_id"],
                        {
                            "phone": phone.e164,
                            "extension": phone.extension,
                            "format_status": "valid",
                            "assignment_evidence": "unknown",
                            "ownership_status": "unknown",
                            "reachability_status": "unknown",
                        },
                        key=phone.e164,
                        now=now,
                    )
                    endpoints[phone.e164] = endpoint
                if (body["campaign_id"], endpoint.id) in existing:
                    result["duplicates"] += 1
                    continue
                timezone = raw.get("timezone") or None
                if timezone:
                    try:
                        ZoneInfo(timezone)
                    except (ValueError, KeyError):
                        timezone = None
                lead = put(
                    s,
                    "leads",
                    p["tenant_id"],
                    p["seller_id"],
                    {
                        "name": str(raw.get("name", "Unknown"))[:200],
                        "company": str(raw.get("company", ""))[:200],
                        "domain": str(raw.get("domain", ""))[:250].lower(),
                        "campaign_id": body["campaign_id"],
                        "endpoint_id": endpoint.id,
                        "phone": phone.e164,
                        "timezone": timezone,
                        "timezone_confidence": raw.get(
                            "timezone_confidence", "confirmed" if timezone else "unknown"
                        ),
                        "region": raw.get("region", phone.e164[2:5]),
                        "segment": raw.get("segment", "general"),
                        "jurisdiction": raw.get("jurisdiction"),
                        "originally_eligible": not self.suppressed(s, p, phone.e164),
                        "status": "eligible",
                        "coverage_due_at": raw.get("coverage_due_at", (now + timedelta(days=1)).isoformat()),
                        "next_attempt_at": now.isoformat(),
                        "attempt_count": 0,
                        "requested_contact": bool(raw.get("requested_contact", False)),
                        "imported_at": now.isoformat(),
                        "synthetic": self.settings.simulation,
                    },
                    key=body["campaign_id"] + ":" + endpoint.id,
                    now=now,
                )
                put(
                    s,
                    "lead_phone_links",
                    p["tenant_id"],
                    p["seller_id"],
                    {"lead_id": lead.id, "endpoint_id": endpoint.id},
                    key=lead.id,
                    now=now,
                )
                existing.add((body["campaign_id"], endpoint.id))
                result["imported"] += 1
                result["leads"].append({**doc(lead), "suppressed": self.suppressed(s, p, phone.e164)})
            self.ensure_experiment_cohort(s, p, body["campaign_id"])
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "leads.imported",
                body["campaign_id"],
                now,
                {k: result[k] for k in ("imported", "duplicates", "invalid")},
            )
        return result

    def leads(self, p):
        with self.db.transaction(False) as s:
            return [
                {**doc(r), "suppressed": self.suppressed(s, p, r.data["phone"])}
                for r in self.scoped(s, "leads", p)
            ]

    def budget_ok(self, s, p, estimated=0.001):
        from sqlalchemy import case, func

        budget = self.scoped(s, "budgets", p)[0].data
        now = self.clock()
        usage = MODELS["usage_ledger"]
        day, month = (
            now.replace(hour=0, minute=0, second=0, microsecond=0),
            now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        )
        key = ("budget_totals", p["tenant_id"], p["seller_id"])
        totals = s.info.get(key)
        if totals is None:
            totals = s.execute(
                select(
                    func.coalesce(func.sum(case((usage.op_at >= day, usage.op_amount), else_=0)), 0),
                    func.coalesce(func.sum(usage.op_amount), 0),
                ).where(
                    usage.tenant_id == p["tenant_id"],
                    usage.seller_id == p["seller_id"],
                    usage.op_at >= month,
                    usage.op_at <= now,
                )
            ).one()
            if "row_cache" in s.info:
                s.info[key] = totals
        daily, monthly = totals
        return daily + estimated <= budget.get("daily_limit_usd", 0) and monthly + estimated <= budget.get(
            "monthly_limit_usd", 0
        )

    def context(self, s, p, lead, number, *, permission="live", active=False):
        campaign = self.required(s, "campaigns", lead.data["campaign_id"], p)
        policy = self.required(s, "policy_versions", campaign.data["policy_id"], p)
        seller = self.required(s, "sellers", p["seller_id"], p)
        endpoint = self.required(s, "phone_endpoints", lead.data["endpoint_id"], p)
        user = self.required(s, "users", p["user_id"], p)
        config, presence = policy.data, user.data.get("presence", {})
        from app.compliance.operating import screening_max_age, source_expiry
        from app.compliance.registry import published_snapshots, registry_contains

        internal_suppressed = self.suppressed(s, p, lead.data["phone"])
        snapshots = [] if internal_suppressed else published_snapshots(s, p)
        state = [
            r
            for r in snapshots
            if r.data.get("kind") == "state" and r.data.get("jurisdiction") == lead.data.get("jurisdiction")
        ]
        state = [
            r
            for r in state
            if (self.settings.simulation or not r.data.get("synthetic"))
            and lead.data["phone"][2:5] in r.data.get("npas", [])
            and (expiry := source_expiry(r.data, self.clock(), screening_max_age(self, s, p)))
            and self.clock() < expiry
        ]
        try:
            state_listed = any(registry_contains(s, r, lead.data["phone"]) for r in state)
            state_current = bool(state)
        except ValueError:
            state_listed, state_current = False, False

        from app.compliance.consent import permission_state

        granted, revoked = permission_state(self, s, p, endpoint.id, permission)
        healthy = number and self.number_readiness_in(s, p, number)["outbound_ready"]
        authorized_number = bool(
            number
            and number.seller_id == p["seller_id"]
            and (self.settings.simulation or not number.data.get("synthetic"))
        )
        from app.compliance.operating import screening_capability, screening_snapshot

        national = screening_capability(
            screening_snapshot(self, s, p, lead.data["phone"], internal_suppressed=internal_suppressed),
            lead.data["phone"],
        )
        return CallContext(
            now=self.clock(),
            seller_approved=seller.data.get("approved", False),
            authorization_reconciled=not seller.data.get("restore_hold", False),
            journal_reconciled=not seller.data.get("journal_recovery_required", False)
            and not seller.data.get("effect_journal_pending")
            and not getattr(self, "_effect_journal_fault", False)
            and (p["tenant_id"], p["seller_id"]) not in self.__dict__.get("_journal_faults", set()),
            policy_published=config.get("approved", False)
            and (self.settings.simulation or not config.get("synthetic")),
            outreach_hold=bool(lead.data.get("outreach_hold") or lead.data.get("conversion_goal_achieved"))
            and not active,
            callback_owner_matches=active
            or not lead.data.get("callback_due_at")
            or lead.data.get("callback_owner_id") in {None, p["user_id"]},
            endpoint_valid=endpoint.data["format_status"] == "valid"
            and endpoint.data.get("reachability_status") != "invalid_endpoint"
            and not endpoint.data.get("reassignment_review_required", False),
            ownership=endpoint.data.get("ownership_status", "unknown"),
            timezone=lead.data.get("timezone"),
            timezone_confidence=lead.data.get("timezone_confidence", "unknown"),
            registry_required=True,
            registry_decision=national,
            registry_checked_at=max((dt(x["checked_at"]) for x in national["sources"]), default=None),
            registry_covered=national["covered"],
            registry_listed=national["decision"] == "listed",
            state_registry_required=config.get("state_registry_required", False),
            state_registry_current=state_current,
            state_registry_listed=state_listed,
            internal_suppressed=internal_suppressed,
            consent_required=config.get("consent_required", False),
            consent_valid=granted,
            consent_revoked=revoked,
            rep_ready=presence.get("status") == "ready" or active,
            audio_ready=bool(
                presence.get("audio_ready")
                and presence.get("telephony_ready")
                and presence.get("agent_call_control_id")
            ),
            number_healthy=bool(healthy),
            number_authorized=authorized_number,
            attempt_count=0 if active else lead.data.get("attempt_count", 0),
            max_attempts=config.get("max_attempts", 4),
            next_attempt_at=None if active else dt(lead.data.get("next_attempt_at")),
            last_attempt_at=None if active else dt(lead.data.get("last_attempt_at")),
            min_retry_seconds=config.get("min_retry_seconds", 3600),
            budget_available=self.budget_ok(s, p),
            route_allowed=lead.data["phone"].startswith("+1")
            and "1" in config.get("allowed_country_codes", []),
            local_start_hour=config.get("local_start_hour", 9),
            local_end_hour=config.get("local_end_hour", 20),
            permission=permission,
            permitted_permissions=set(config.get("permissions", [])),
        )

    def record_decision(self, s, p, lead, decision):
        return put(
            s,
            "compliance_decisions",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead.id,
                "allowed": decision.allowed,
                "reasons": decision.reasons,
                "at": self.clock().isoformat(),
                "campaign_id": lead.data["campaign_id"],
            },
            now=self.clock(),
        )

    def agent_leg_in(self, s, p, *, provider_id=None, leg_id=None, rep_id=None, active=False):
        model = MODELS["call_legs"]
        query = select(model).where(model.tenant_id == p["tenant_id"])
        if p.get("seller_id"):
            query = query.where(model.seller_id == p["seller_id"])
        if rep_id:
            query = query.where(model.op_owner_id == rep_id)
        if provider_id:
            query = query.where(model.op_provider_id == provider_id)
        elif leg_id:
            query = query.where(model.id == leg_id)
        elif active and rep_id:
            query = query.where(model.op_status.in_(["submitting", "uncertain", "ringing", "answered"]))
        else:
            raise ValueError("Agent lookup requires a provider, local identity or active rep")
        candidates = list(s.scalars(query.limit(2)))
        if len(candidates) > 1:
            fail(409, "Multiple agent identities need review before another setup")
        return next((row for row in candidates if row.data.get("kind") == "agent"), None)

    def release_unused_reservation_in(self, s, p):
        """Pause/logout releases only this rep's unsubmitted resource claim."""
        claim = s.scalar(
            select(ActiveClaim).where(
                ActiveClaim.tenant_id == p["tenant_id"],
                ActiveClaim.seller_id == p["seller_id"],
                ActiveClaim.rep_id == p["user_id"],
                ActiveClaim.call_id.is_(None),
            )
        )
        if claim:
            reservation = self.required(s, "reservations", claim.reservation_id, p)
            update(reservation, status="cancelled")
            s.delete(claim)

    def presence(self, p, data):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            user = self.required(s, "users", p["user_id"], p)
            if not self.settings.simulation and data.get("agent_call_control_id"):
                verified = self.agent_leg_in(
                    s, p, provider_id=data["agent_call_control_id"], rep_id=p["user_id"]
                )
                if not verified or verified.data.get("state") != "answered":
                    fail(403, "Agent leg must be established and verified by the server")
            if data.get("status") == "paused":
                self.release_unused_reservation_in(s, p)
                self.pause_work_in(s, p)
            else:
                self.record_work_in(s, p, True)
            update(user, presence={**user.data.get("presence", {}), **data, "at": self.clock().isoformat()})
            return user.data["presence"]

    def _expired_claims(self):
        """Unstarted claims whose reservation lapsed; one row per rep at most."""
        return select(ActiveClaim.id).where(
            ActiveClaim.call_id.is_(None), ActiveClaim.expires_at <= self.clock()
        )

    def _expire_claims(self, s):
        # One bounded statement instead of loading every tenant's claims. The outer
        # predicate is re-checked on PostgreSQL, so a claim that just started a call stays.
        s.execute(
            delete(ActiveClaim)
            .where(
                ActiveClaim.id.in_(self._expired_claims().limit(500)),
                ActiveClaim.call_id.is_(None),
                ActiveClaim.expires_at <= self.clock(),
            )
            .execution_options(synchronize_session=False)
        )

    def choose_number(self, s, p, lead):
        bindings = [r for r in self.scoped(s, "lead_number_bindings", p) if r.data["lead_id"] == lead.id]
        selection = select_number(
            doc(lead),
            self.inventory_evidence(s, p),
            bound_id=bindings[0].data["number_id"] if bindings else None,
        )
        return (
            get(s, "number_inventory", selection["number_id"], p["tenant_id"])
            if selection["number_id"]
            else None
        )

    def _prepare_call(self, p, data):
        now = self.clock()
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            previous = self._by_key(s, p, "call_attempts", data["idempotency_key"])
            if previous:
                if (
                    previous.data["rep_id"] != p["user_id"]
                    or previous.data["reservation_id"] != data["reservation_id"]
                ):
                    fail(409, "Idempotency key belongs to another intent")
                return doc(previous), False
            reservation = self.required(s, "reservations", data["reservation_id"], p)
            if (
                reservation.data["rep_id"] != p["user_id"]
                or reservation.data["status"] != "reserved"
                or dt(reservation.data["expires_at"]) <= now
            ):
                fail(409, "Reservation expired or not owned by this rep")
            claim = s.scalar(select(ActiveClaim).where(ActiveClaim.reservation_id == reservation.id))
            if not claim:
                fail(409, "Reservation claim is no longer valid")
            lead = self.required(s, "leads", reservation.data["lead_id"], p)
            number = self.required(s, "number_inventory", reservation.data["number_id"], p)
            decision = authorize(self.context(s, p, lead, number))
            self.record_decision(s, p, lead, decision)
            if not decision.allowed:
                fail(403, {"code": "CALL_DENIED", "reasons": decision.reasons})
            from app.postcall.rubrics import configuration, snapshot

            rubric = snapshot(configuration(self.required(s, "campaigns", lead.data["campaign_id"], p)))
            call = put(
                s,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {
                    "rep_id": p["user_id"],
                    "lead_id": lead.id,
                    "rubric_snapshot": rubric,
                    "rubric_revision": 1,
                    "endpoint_id": lead.data["endpoint_id"],
                    "number_id": number.id,
                    "reservation_id": reservation.id,
                    "treatment_variant_id": reservation.data.get("treatment_variant_id"),
                    "feature_snapshot_id": reservation.data.get("feature_snapshot_id"),
                    "decision_id": reservation.data.get("policy_decision_id"),
                    "allocation_decision_id": reservation.data.get("allocation_decision_id"),
                    "coaching_comparison": reservation.data.get("coaching_comparison"),
                    "script_snapshot": self.selected_script(
                        s, p, lead, variant_id=reservation.data.get("treatment_variant_id")
                    )[0],
                    "state": "AUTHORIZED",
                    "started_at": now.isoformat(),
                    "max_call_seconds": self.settings.max_call_seconds,
                    "provider_id": None,
                    "outcome": None,
                    "transcript_version": 0,
                    "detached": False,
                    "synthetic": self.settings.simulation,
                },
                key=data["idempotency_key"],
                now=now,
            )
            claim.call_id = call.id
            update(reservation, status="active", call_id=call.id)
            presence = self.required(s, "users", p["user_id"], p).data["presence"]
            payload = {
                "to": lead.data["phone"],
                "from": number.data["phone"],
                "link_to": presence["agent_call_control_id"],
                "bridge_on_answer": True,
                "park_after_unbridge": "self",
                "prevent_double_bridge": True,
                "time_limit_secs": self.settings.max_call_seconds,
                "command_id": call.id,
                "client_state": base64.b64encode(
                    json.dumps({"call_id": call.id, "tenant_id": p["tenant_id"]}).encode()
                ).decode(),
            }
            put(
                s,
                "command_outbox",
                p["tenant_id"],
                p["seller_id"],
                {
                    "kind": "dial",
                    "call_id": call.id,
                    "rep_id": p["user_id"],
                    "payload": payload,
                    "status": "pending",
                    "attempts": 0,
                },
                key=call.id,
                now=now,
            )
            put(
                s,
                "usage_ledger",
                p["tenant_id"],
                p["seller_id"],
                {
                    "provider": "telnyx",
                    "call_id": call.id,
                    "kind": "call_authorization",
                    "at": now.isoformat(),
                    "reserved_usd": 0.05,
                    "cost_usd": None,
                    "billing_verified": False,
                    "synthetic": self.settings.simulation,
                },
                key="call:" + call.id,
                now=now,
            )
            emit(s, p["tenant_id"], "call.updated", call, now, {"state": call.data["state"]})
            return doc(call), True

    def _call_snapshot(self, p, call_id):
        with self.db.transaction(False) as s:
            return doc(self.required(s, "call_attempts", call_id, p))

    async def start_call(self, p, data):
        call, created = await asyncio.to_thread(self._prepare_call, p, data)
        if not created:
            return call
        await self.dispatch_call(p, call["id"])
        return await asyncio.to_thread(self._call_snapshot, p, call["id"])

    def _claim_dial(self, p, call_id, owner):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            call = self.required(s, "call_attempts", call_id, p)
            model = MODELS["command_outbox"]
            command = s.scalar(
                select(model).where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.external_key == call_id,
                    model.op_kind == "dial",
                )
            )
            if not command or command.data.get("call_id") != call_id:
                fail(409, "Original dial intent is missing; review this call before retrying")
            if command.data["status"] != "pending":
                return
            lead = self.required(s, "leads", call.data["lead_id"], p)
            number = self.required(s, "number_inventory", call.data["number_id"], p)
            decision = authorize(self.context(s, p, lead, number))
            submission_decision = self.record_decision(s, p, lead, decision)
            if not decision.allowed:
                update(command, status="cancelled", reasons=decision.reasons)
                self.finish_call(s, p, call, "policy_cancelled")
                return
            payload = command.data["payload"]
            update(
                command,
                status="sending",
                attempts=1,
                submitted_at=self.clock().isoformat(),
                sent_at=self.clock().isoformat(),
                lease_owner=owner,
                lease_until=(self.clock() + timedelta(seconds=30)).isoformat(),
            )
            update(
                call,
                state="DIALING",
                submission_decision_id=submission_decision.id,
                submitted_at=self.clock().isoformat(),
            )
            policy = self.required(
                s,
                "policy_versions",
                self.required(s, "campaigns", lead.data["campaign_id"], p).data["policy_id"],
                p,
            )
            ordinal = lead.data.get("attempt_count", 0) + 1
            allowance = max(lead.data.get("attempt_allowance", 0), policy.data.get("max_attempts", 4))
            update(call, attempt_ordinal=ordinal, attempt_allowance=allowance)
            update(
                lead,
                attempt_count=ordinal,
                attempt_allowance=allowance,
                last_attempt_at=self.clock().isoformat(),
                next_attempt_at=(
                    self.clock() + timedelta(seconds=policy.data.get("min_retry_seconds", 3600))
                ).isoformat(),
            )
            records = [call, command, self._by_key(s, p, "usage_ledger", "call:" + call.id)]
            self.journal_effect_in(s, p, "dial", call.id, "intent", records, source=payload)
            self.journal_effect_in(s, p, "dial", call.id, "submitted", records, source=payload)
            return command.id, doc(call), lead.data["phone"], payload

    def _dial_failed(self, p, call_id, command_id, owner, exc):
        with self.db.transaction() as s:
            command = self.required(s, "command_outbox", command_id, p)
            s.refresh(command, with_for_update=True)
            call = self.required(s, "call_attempts", call_id, p)
            if owns_effect(command.data, owner, self.clock()) and command.data["status"] == "sending":
                rejected = isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500
                update(
                    command,
                    status="failed" if rejected else "uncertain",
                    error=type(exc).__name__,
                    lease_owner=None,
                    lease_until=None,
                )
                if rejected:
                    self.finish_call(s, p, call, "provider_rejected")
                elif call.data["state"] == "DIALING" and not call.data.get("provider_id"):
                    update(call, state="RECONCILING")
                    emit(s, p["tenant_id"], "call.updated", call, self.clock(), {"state": "RECONCILING"})

    def _dial_completed(self, p, call_id, claimed, owner, receipt):
        command_id, call_data, phone, payload = claimed
        cancel = False
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            current = self.required(s, "command_outbox", command_id, p)
            s.refresh(current, with_for_update=True)
            self.journal_effect_in(
                s,
                p,
                "dial",
                call_id,
                "receipt",
                [call, current],
                provider_id=receipt["call_control_id"],
                source=payload,
                emergency=True,
            )
            if current.data["status"] != "completed":
                if current.data["status"] != "sending" or not owns_effect(current.data, owner, self.clock()):
                    return
                self.bind_dial_receipt(s, p, call, receipt["call_control_id"])
            cancel = call.data["state"] not in TERMINAL and (
                call.data["state"] == "ENDING" or self.suppressed(s, p, phone)
            )
            update(
                call,
                state=reduce_state(
                    call.data["state"], "call.ringing" if self.settings.simulation else "call.initiated"
                ),
            )
            emit(s, p["tenant_id"], "call.updated", call, self.clock(), {"state": call.data["state"]})
        return cancel

    async def dispatch_call(self, p, call_id):
        owner = uid()
        claimed = await asyncio.to_thread(self._claim_dial, p, call_id, owner)
        if not claimed:
            return
        try:
            provider = await asyncio.to_thread(self.carrier, p["tenant_id"])
            receipt = await provider.dial(claimed[3])
        except Exception as exc:
            await asyncio.to_thread(self._dial_failed, p, call_id, claimed[0], owner, exc)
            return
        cancel = await asyncio.to_thread(self._dial_completed, p, call_id, claimed, owner, receipt)
        if cancel:
            await self.hangup(p, call_id)

    def bind_dial_receipt(self, s, p, call, provider_id):
        if call.data.get("provider_id") not in {None, provider_id}:
            fail(409, "Provider call reference conflicts with the established leg")
        update(call, provider_id=provider_id)
        command = self._by_key(s, p, "command_outbox", call.id)
        if (
            command
            and command.data.get("call_id") == call.id
            and command.data.get("kind") == "dial"
            and command.data.get("status") in {"sending", "uncertain", "reconciling", "review_required"}
        ):
            update(command, status="completed", provider_id=provider_id, lease_owner=None, lease_until=None)
        prior = self._by_key(s, p, "call_legs", provider_id)
        if not prior:
            put(
                s,
                "call_legs",
                p["tenant_id"],
                p["seller_id"],
                {
                    "call_id": call.id,
                    "provider_id": provider_id,
                    "kind": "prospect",
                    "state": call.data["state"],
                },
                key=provider_id,
                now=self.clock(),
            )

    def finish_call(self, s, p, call, reason=None):
        self.coach.cancel(call.id)
        if call.data["state"] != "FINALIZED":
            ended = dt(call.data.get("ended_at")) or self.clock()
            update(call, state="ENDED", ended_at=ended.isoformat(), end_reason=reason or "hangup")
            answered = dt(call.data.get("answered_at"))
            if answered and answered <= ended:
                update(call, connected_seconds=(ended - answered).total_seconds())
        legs = MODELS["call_legs"]
        if call.data.get("provider_id"):
            for leg in s.scalars(
                select(legs).where(
                    legs.tenant_id == p["tenant_id"],
                    legs.seller_id == p["seller_id"],
                    legs.op_provider_id == call.data["provider_id"],
                )
            ):
                if leg.data.get("kind") == "prospect" and leg.data.get("call_id") == call.id:
                    update(leg, state="ENDED", ended_at=call.data.get("ended_at"))
        for claim in list(s.scalars(select(ActiveClaim).where(ActiveClaim.call_id == call.id))):
            s.delete(claim)
        emit(s, p["tenant_id"], "call.updated", call, self.clock(), {"state": call.data["state"]})

    def _claim_hangup(self, p, call_id, owner, recovery_expected=None):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            call = self.required(s, "call_attempts", call_id, p)
            s.refresh(call, with_for_update=True)
            if p["role"] == "rep" and call.data["rep_id"] != p["user_id"]:
                fail(403, "Call belongs to another rep")
            if call.data["state"] in TERMINAL:
                return doc(call), None
            if recovery_expected is not None and (
                any(
                    call.data.get(key) != recovery_expected.get(key)
                    for key in ("provider_id", "started_at", "max_call_seconds")
                )
                or not call.data.get("started_at")
                or (self.clock() - dt(call.data["started_at"])).total_seconds()
                < call.data.get("max_call_seconds", self.settings.max_call_seconds)
            ):
                return doc(call), None
            provider_id = call.data.get("provider_id")
            command_id = call.id + ":hangup"
            existing = self._by_key(s, p, "command_outbox", command_id)
            if not existing:
                existing = put(
                    s,
                    "command_outbox",
                    p["tenant_id"],
                    p["seller_id"],
                    {"kind": "hangup", "call_id": call.id, "provider_id": provider_id, "status": "pending"},
                    key=command_id,
                    now=self.clock(),
                )
            if existing.data.get("lease_until") and dt(existing.data["lease_until"]) > self.clock():
                return doc(call), None
            if existing.data.get("status") == "completed":
                return doc(call), None
            update(
                existing,
                status="sending" if provider_id else "review_required",
                provider_id=provider_id,
                lease_owner=owner if provider_id else None,
                lease_until=(self.clock() + timedelta(seconds=30)).isoformat() if provider_id else None,
                sent_at=existing.data.get("sent_at") or self.clock().isoformat(),
                recovery_reason=None
                if provider_id
                else "Await the signed dial receipt or attach a verified carrier call reference",
            )
            update(call, state="ENDING")
            self.journal_effect_in(
                s,
                p,
                "hangup",
                command_id,
                "submitted",
                [existing, call],
                provider_id=provider_id,
                emergency=True,
            )
            self.coach.cancel(call.id)
            emit(s, p["tenant_id"], "call.updated", call, self.clock(), {"state": "ENDING"})
            return doc(call), {"id": existing.id, "provider_id": provider_id, "command_id": command_id}

    def _hangup_completed(self, p, call_id, command_id, owner, error=None):
        with self.db.transaction() as s:
            current = self.required(s, "command_outbox", command_id, p)
            s.refresh(current, with_for_update=True)
            if current.data["status"] != "sending" or not owns_effect(current.data, owner, self.clock()):
                return
            if error is not None:
                update(current, status="uncertain", lease_owner=None, lease_until=None)
            elif current.data["status"] == "sending":
                update(current, status="completed", lease_owner=None, lease_until=None)
                if self.settings.simulation:
                    self.finish_call(
                        s, p, self.required(s, "call_attempts", call_id, p), "simulated_hangup_receipt"
                    )
            self.journal_effect_in(s, p, "hangup", current.external_key, "receipt", [current], emergency=True)

    def _hangup_result(self, p, call_id):
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            if call.data["state"] in TERMINAL:
                self.jobs.enqueue_in(
                    s,
                    "postcall",
                    {"call_id": call_id, "seller_id": p["seller_id"], "user_id": p["user_id"]},
                    "postcall:" + call_id + ":" + str(call.version),
                    tenant_id=p["tenant_id"],
                )
            return doc(call)

    async def hangup(self, p, call_id, *, recovery_expected=None):
        owner = uid()
        call, command = await asyncio.to_thread(self._claim_hangup, p, call_id, owner, recovery_expected)
        if not command:
            return call
        if command["provider_id"]:
            try:
                provider = await asyncio.to_thread(self.carrier, p["tenant_id"])
                await provider.hangup(command["provider_id"], command["command_id"])
            except Exception as exc:
                await asyncio.to_thread(self._hangup_completed, p, call_id, command["id"], owner, exc)
                logging.getLogger(__name__).warning("Durable hangup pending: %s", type(exc).__name__)
            else:
                await asyncio.to_thread(self._hangup_completed, p, call_id, command["id"], owner)
        return await asyncio.to_thread(self._hangup_result, p, call_id)

    def suppress(self, p, phone, reason, *, event_id=None, session=None):
        from app.compliance.suppressions import record

        return record(self, p, phone, reason, event_id=event_id, session=session)

    def event(self, p, event_id, event_type, call_id, payload=None, *, session=None):
        payload = payload or {}
        canonical = json.dumps(
            {"event_type": event_type, "call_id": call_id, "payload": payload}, sort_keys=True
        )
        hashed = hashlib.sha256(canonical.encode()).hexdigest()
        with self.db.transaction(session=session) as s:
            existing = self._by_key(s, p, "provider_events", event_id)
            if existing:
                if existing.data["hash"] != hashed:
                    fail(409, "Provider event ID collision")
                return {"duplicate": True, "event_id": event_id}
            call = self.required(s, "call_attempts", call_id, p)
            # Call writers lock the source before dirtying its shared report generation.
            s.refresh(call, with_for_update=True)
            put(
                s,
                "provider_events",
                p["tenant_id"],
                p["seller_id"],
                {
                    "call_id": call_id,
                    "event_type": event_type,
                    "hash": hashed,
                    "payload": event_metadata(payload),
                    "effects_complete": False,
                    "received_at": self.clock().isoformat(),
                },
                key=event_id,
                now=self.clock(),
            )
            from app.measurement import call_timing

            timing = call_timing(call.data, event_type, payload, now=self.clock())
            if timing:
                update(call, **timing)
            target = reduce_state(call.data["state"], event_type)
            if payload.get("call_control_id"):
                self.bind_dial_receipt(s, p, call, payload["call_control_id"])
            if target == "ENDED":
                self.finish_call(s, p, call, event_type)
            else:
                update(call, state=target)
                emit(s, p["tenant_id"], "call.updated", call, self.clock(), {"state": target})
            return {"duplicate": False, "event_id": event_id, "state": target}
