import asyncio
import base64
import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from sqlalchemy import delete, or_, select

from app.calling import audio_leg
from app.calling.blockers import refuse, test_line_refusal
from app.calling.carrier_errors import PAUSE_SECONDS as CHANNEL_PAUSE
from app.calling.carrier_errors import is_channel_limit
from app.calling.recovery import next_step
from app.calling.state import TERMINAL, moved, transition
from app.compliance.phone import normalize_phone
from app.compliance.policy import CallContext, authorize, retry_seconds
from app.lead_lists import LeadLists, memberships, set_memberships
from app.leads import intake
from app.leads.record import csv_fields, number_view
from app.optimization.geography import plausible_zones
from app.platform.config import LIMITS, TEST_LINES
from app.platform.ctx import Ctx
from app.platform.db import MODELS, ActiveClaim, audit, doc, emit, get, put, rows, uid, update
from app.platform.jobs import JobQueue
from app.platform.leases import owns_effect
from app.platform.security import PASSWORDS, ConfigStore, issue_session
from app.privacy import event_metadata
from app.providers.carrier import carrier_factory
from app.providers.simulated import CarrierNotSubmitted, SimulatedCarrier
from app.scheduling.queue import ReservationPlanner

TEST_LIST = "Test line"


def _body(response):
    try:
        return response.json()
    except ValueError:
        return response.text


def dt(value):
    return datetime.fromisoformat(value) if value else None


def fail(status, message):
    raise HTTPException(status, message)


class Service(LeadLists, ReservationPlanner):
    def __init__(self, db, settings, clock, carrier=None):
        self.db, self.settings, self.clock = db, settings, clock
        self.injected_carrier = carrier
        self.simulated_carrier = carrier or SimulatedCarrier()
        self.configs = ConfigStore(settings.data_dir)
        self.carriers = carrier_factory(settings, self.configs, self.simulated_carrier)
        self.ctx = Ctx(db, settings, clock, self.configs, lambda workspace: self.carrier(workspace))
        from app.managed_webhooks import ManagedConnection

        self.managed = ManagedConnection(self)
        self.ingress_lock = threading.Lock()
        self.ingress_challenges = {}
        self.capability_cache = {}
        self.jobs = JobQueue(db, clock)
        from app.privacy import EphemeralBuffer

        self.raw = EphemeralBuffer(clock=clock)
        from app.helper.live import Tips

        self.tips = Tips()
        self.screening_capacity = asyncio.Semaphore(2)
        self.research_flights = {}
        self.job_admission = {
            name: asyncio.Semaphore(capacity)
            for name, capacity in {
                "webhook": LIMITS.live_active,
                "live_coach": LIMITS.live_active,
                "postcall": LIMITS.postcall_active,
                "preparation": LIMITS.agent_threads,
                "ml": 2,
                "crm": 2,
            }.items()
        }

    def required(self, s, kind, object_id, p, *, lock=False):
        obj = get(s, kind, object_id, p["tenant_id"], lock=lock)
        if not obj or (obj.seller_id and obj.seller_id != p["seller_id"]):
            fail(404, "Object not found")
        if p.get("role") == "rep" and kind == "call_attempts" and obj.data.get("rep_id") != p["user_id"]:
            fail(404, "Object not found")
        if p.get("role") == "rep" and kind == "notes":
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
                    model.ctx_key == context_key(kind, value),
                    *conditions,
                )
                .order_by(model.created_at.desc(), model.id.desc())
                .limit(limit)
            )
        )

    def carrier(self, tenant_id):
        return self.carriers(tenant_id, http=getattr(self, "http", None))

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
                    "dnc_screening": False,
                    "state_registry_required": False,
                    "permissions": ["live", "transcription"],
                    "local_start_hour": 8,
                    "local_end_hour": 17,
                    "max_attempts": 4,
                    "min_retry_seconds": 86400,
                    "allowed_country_codes": ["1"],
                    "voicemail_enabled": False,
                    "approved_assets": [],
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
                {"per_list_usd": 25, "number_purchase_limit_usd": 0},
                key="budget",
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
            campaign = self.scoped(s, "campaigns", p)[0]
            if not (campaign.data.get("script") or {}).get("template"):
                from app.preparation.templates import STARTER, parse_markdown

                self.adopt_script_in(s, p, campaign, parse_markdown(STARTER), "simulation_starter")
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
            update(
                self.required(s, "sellers", p["seller_id"], p),
                callback_test={"at": now.isoformat(), "simulated": True},
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
            key = phone
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
        from app.optimization.geography import AREA_STATES, normalize_state
        from app.preparation.workflows import company_key

        result = {"imported": 0, "duplicates": 0, "invalid": 0, "errors": [], "leads": [], "preparing": 0}
        created = []
        if len(body["rows"]) > self.settings.max_import_rows:
            fail(413, "Import row limit exceeded")
        headers = list(dict.fromkeys(str(k) for raw in body["rows"] for k in raw))
        column_map = body.get("column_map")
        if column_map is None:
            column_map = intake.guess_map(headers)
        elif problems := intake.check_map(column_map, headers):
            fail(422, {"code": "COLUMN_MAP_INVALID", "errors": problems})
        result["column_map"] = column_map
        now = self.clock()
        with self.db.transaction() as s:
            campaign = self.required(s, "campaigns", body["campaign_id"], p)
            name = body.get("list_name", "").strip()
            update(campaign, lists={name: {}, **campaign.data.get("lists", {})})
            endpoints = {r.data["phone"]: r for r in self.scoped(s, "phone_endpoints", p)}
            existing = {(r.data["campaign_id"], r.data["endpoint_id"]): r for r in self.scoped(s, "leads", p)}
            for index, raw in enumerate(body["rows"]):
                phone = normalize_phone(str(raw.get("phone", "")))
                if not phone.e164:
                    result["invalid"] += 1
                    result["errors"].append({"row": index + 1, "reason": "INVALID_PHONE"})
                    continue
                state = normalize_state(raw.get("state"))
                if raw.get("state") and not state:
                    result["invalid"] += 1
                    result["errors"].append({"row": index + 1, "reason": "INVALID_STATE"})
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
                    duplicate = self.required(
                        s, "leads", existing[(body["campaign_id"], endpoint.id)].id, p, lock=True
                    )
                    names = list(
                        dict.fromkeys([*memberships(duplicate.data), body.get("list_name", "").strip()])
                    )
                    update(duplicate, list_names=names)
                    emit(s, p["tenant_id"], "lead.updated", duplicate, self.clock())
                    result["leads"].append(doc(duplicate))
                    continue
                timezone = raw.get("timezone") or None
                if timezone:
                    try:
                        ZoneInfo(timezone)
                    except (ValueError, KeyError):
                        timezone = None
                fields = intake.read(raw, column_map)
                row = fields["row"]
                lead = put(
                    s,
                    "leads",
                    p["tenant_id"],
                    p["seller_id"],
                    {
                        "row": row,
                        "company_key": company_key(raw),
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
                        "state": state
                        or normalize_state(raw.get("jurisdiction"))
                        or AREA_STATES.get(phone.e164[2:5]),
                        "state_source": "provided"
                        if state or normalize_state(raw.get("jurisdiction"))
                        else "phone_area_code"
                        if AREA_STATES.get(phone.e164[2:5])
                        else "unknown",
                        "list_name": body.get("list_name", "").strip(),
                        "list_names": [body.get("list_name", "").strip()],
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
                        **csv_fields(),
                        "signals": fields["signals"],
                        "profile": fields["profile"],
                        "number_source": fields["number_source"],
                        "live_status": fields["live_status"],
                    },
                    key=body["campaign_id"] + ":" + endpoint.id,
                    now=now,
                )
                existing[(body["campaign_id"], endpoint.id)] = lead
                result["imported"] += 1
                created.append(lead)
                result["leads"].append({**doc(lead), "suppressed": self.suppressed(s, p, phone.e164)})
            if body.get("prepare") and created and self.lead_script_base_ready(s, p) and self.agent_route(p):
                result["preparing"] = self.request_preparation_in(s, p, created)
                update(self.required(s, "sellers", p["seller_id"], p), preparation_user_id=p["user_id"])
            self.ensure_experiment_cohort(s, p, body["campaign_id"])
            emit(s, p["tenant_id"], "campaign.updated", campaign, self.clock())
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
        if result["preparing"]:
            self.prepare_tick(p)
        return result

    def leads(self, p):
        with self.db.transaction(False) as s:
            return [
                {**doc(r), **number_view(r.data), "suppressed": self.suppressed(s, p, r.data["phone"])}
                for r in self.scoped(s, "leads", p)
            ]

    def scope_spend(self, s, p, scope):
        """Spent and reserved AI money for one lead list (all time) or `other` work (this month)."""
        from sqlalchemy import BigInteger, cast, func

        usage = MODELS["usage_ledger"]
        amount = cast(func.round(usage.op_amount * 1_000_000), BigInteger)
        query = select(func.coalesce(func.sum(amount), 0)).where(
            usage.tenant_id == p["tenant_id"],
            usage.seller_id == p["seller_id"],
            usage.op_campaign_id == scope,
        )
        if scope == "other":
            month = self.clock().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            query = query.where(usage.op_at >= month)
        return int(s.scalar(query)) / 1_000_000

    def budget_ok(self, s, p, estimated=0.001, scope="other"):
        """Each lead list may spend up to the per-list budget on AI; other work gets the same per month."""
        budget = self.scoped(s, "budgets", p)[0].data
        limit = Decimal(str(budget.get("per_list_usd", 25.0)))
        spent = Decimal(str(self.scope_spend(s, p, scope)))
        return spent + Decimal(str(estimated)) <= limit

    def context(self, s, p, lead, number, *, permission="live", active=False):
        campaign = self.required(s, "campaigns", lead.data["campaign_id"], p)
        policy = self.required(s, "policy_versions", campaign.data["policy_id"], p)
        seller = self.required(s, "sellers", p["seller_id"], p)
        endpoint = self.required(s, "phone_endpoints", lead.data["endpoint_id"], p)
        user = self.required(s, "users", p["user_id"], p)
        config, presence = policy.data, user.data.get("presence", {})
        internal_suppressed = self.suppressed(s, p, lead.data["phone"])
        # Registry imports and consent records are gone (R2.5); neither was ever loaded in practice.
        state_listed, state_current = False, False
        granted, revoked = False, False
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
        callback_due = bool(
            not active
            and (due := dt(lead.data.get("callback_due_at")))
            and due <= self.clock()
            and lead.data.get("callback_owner_id") in {None, p["user_id"]}
        )
        pair_open = bool(
            not active
            and (until := dt(lead.data.get("pair_until")))
            and self.clock() < until
            and lead.data.get("pair_rep_id") in {None, p["user_id"]}
        )
        return CallContext(
            now=self.clock(),
            continuing_call=active,
            authorization_reconciled=not seller.data.get("restore_hold", False),
            outreach_hold=bool(
                lead.data.get("outreach_hold")
                or lead.data.get("conversion_goal_achieved")
                or not self.list_permitted(s, p, lead)
            )
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
            timezones=plausible_zones(lead.data),
            registry_required=bool(config.get("dnc_screening")),
            registry_decision=national,
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
            min_retry_seconds=retry_seconds(config),
            callback_due=callback_due,
            pair_open=pair_open,
            # AI budgets are per lead list and never gate calling.
            budget_available=True,
            route_allowed=lead.data["phone"].startswith("+1")
            and "1" in config.get("allowed_country_codes", []),
            local_start_hour=config.get("local_start_hour", 8),
            local_end_hour=min(17, config.get("local_end_hour", 17)),
            permission=permission,
            # Live calls carry the speech-to-text the live helper needs; nothing is recorded.
            permitted_permissions=set(config.get("permissions", []))
            | ({"transcription"} if "live" in config.get("permissions", []) else set()),
            own_test_line=self.own_test_line(p, lead),
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
                verified = audio_leg.find(
                    s, p, provider_id=data["agent_call_control_id"], rep_id=p["user_id"]
                )
                if not verified or verified.data.get("state") != "answered":
                    fail(403, "Agent leg must be established and verified by the server")
            if data.get("status") == "paused":
                self.release_unused_reservation_in(s, p)
            starting = (
                data.get("status") == "ready" and (user.data.get("presence") or {}).get("status") != "ready"
            )
            update(user, presence={**user.data.get("presence", {}), **data, "at": self.clock().isoformat()})
            presence = user.data["presence"]
        if starting:
            from app import crm

            crm.schedule_syncs(
                self, p, starting=True
            )  # Start calling syncs every HubSpot list first (section 6).
        return presence

    def _expirable(self):
        # Unstarted reservations, and inbound routing claims (keyed by their task) past the
        # carrier call time limit, which no live inbound leg can outlast.
        return or_(ActiveClaim.call_id.is_(None), ActiveClaim.call_id == ActiveClaim.reservation_id)

    def _expired_claims(self):
        """Lapsed claims that no carrier leg can still need; one row per rep at most."""
        return select(ActiveClaim.id).where(self._expirable(), ActiveClaim.expires_at <= self.clock())

    def _expire_claims(self, s):
        # One bounded statement instead of loading every tenant's claims. The outer
        # predicate is re-checked on PostgreSQL, so a claim that just started a call stays.
        s.execute(
            delete(ActiveClaim)
            .where(
                ActiveClaim.id.in_(self._expired_claims().limit(500)),
                self._expirable(),
                ActiveClaim.expires_at <= self.clock(),
            )
            .execution_options(synchronize_session=False)
        )

    def test_lines(self, p):
        """The owner's test line, and the test phone typed in Settings (section 10)."""
        from app.compliance.phone import normalize_phone

        typed = normalize_phone(self.configs.read(p["tenant_id"]).get("test_phone") or "").e164
        return TEST_LINES | ({typed} if typed else set())

    def test_phone(self, p):
        """The test phone saved in Settings, or None."""
        return normalize_phone(self.configs.read(p["tenant_id"]).get("test_phone") or "").e164 or None

    def in_test_session(self, p):
        # A test session with no saved test phone would dial no one, so it does not exist.
        return bool(self.configs.read(p["tenant_id"]).get("test_session") and self.test_phone(p))

    def own_test_line(self, p, lead):
        """The rep's own test phone during a test call: calling hours and attempt limits do not apply to it."""
        return self.in_test_session(p) and lead.data.get("phone") in self.test_lines(p)

    def refuse_outside_test_line(self, p, lead):
        refuse(
            test_line_refusal(
                lead.data["phone"], test_session=self.in_test_session(p), test_lines=self.test_lines(p)
            )
        )

    def start_test_session(self, p):
        """Test call: the saved test phone becomes the one callable lead, whatever state an older copy was in.

        An existing lead with that number (archived, paused, called before, on another list) is put back on the
        Test line list and made due now; otherwise it is created. The session ends when the test call is saved.
        """
        phone = self.test_phone(p)
        if not phone:
            fail(409, "Type your phone number in Test phone first")
        with self.db.transaction(False) as s:
            campaign = next(iter(self.scoped(s, "campaigns", p)), None)
            existing = next(
                (row for row in self.scoped(s, "leads", p) if row.data.get("phone") == phone), None
            )
        if not campaign:
            fail(409, "Import a lead list first")
        if existing is None:
            self.import_leads(
                p,
                {
                    "campaign_id": campaign.id,
                    "list_name": TEST_LIST,
                    "rows": [{"name": "Your test phone", "phone": phone, "company": "Test call"}],
                    "prepare": False,
                },
            )
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            lead = next(row for row in self.scoped(s, "leads", p) if row.data.get("phone") == phone)
            set_memberships(lead, [TEST_LIST, *memberships(lead.data)])
            update(
                lead,
                archived=False,
                dial_paused=False,
                # The rep's own phone is always callable in a test session; only an opt-out still stops it.
                status="eligible",
                next_attempt_at=None,
                pair_opened_at=None,
                due_at=None,
            )
            campaign = self.required(s, "campaigns", lead.data["campaign_id"], p, lock=True)
            catalog = dict(campaign.data.get("lists", {}))
            catalog[TEST_LIST] = {**catalog.get(TEST_LIST, {}), "archived": False, "paused": False}
            update(campaign, lists=catalog)
            emit(s, p["tenant_id"], "lead.updated", lead, self.clock())
            lead_id = lead.id
        self.configs.write(p["tenant_id"], {"test_session": True})
        return {"test_session": True, "lead_id": lead_id, "phone": phone}

    def end_test_session(self, p):
        self.configs.write(p["tenant_id"], {"test_session": False})

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
            refuse(self.dial_blockers_in(s, p, rep=True))
            agent = self.required(s, "users", p["user_id"], p).data["presence"]["agent_call_control_id"]
            lead = self.required(s, "leads", reservation.data["lead_id"], p)
            self.refuse_outside_test_line(p, lead)
            number = self.required(s, "number_inventory", reservation.data["number_id"], p)
            self.managed.require_number(p["tenant_id"], number.data)
            scoped_p = {**p, **reservation.data.get("dial_scope", {})}
            decision = authorize(self.context(s, scoped_p, lead, number))
            self.record_decision(s, p, lead, decision)
            if not decision.allowed:
                fail(403, {"code": "CALL_DENIED", "reasons": decision.reasons})
            call = put(
                s,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {
                    "rep_id": p["user_id"],
                    "lead_id": lead.id,
                    "endpoint_id": lead.data["endpoint_id"],
                    "number_id": number.id,
                    "reservation_id": reservation.id,
                    "dial_scope": reservation.data.get("dial_scope", {}),
                    "treatment_variant_id": reservation.data.get("treatment_variant_id"),
                    "feature_snapshot_id": reservation.data.get("feature_snapshot_id"),
                    "decision_id": reservation.data.get("policy_decision_id"),
                    "coaching_comparison": reservation.data.get("coaching_comparison"),
                    "script_comparison": reservation.data.get("script_comparison"),
                    "script_snapshot": self.selected_script(
                        s, p, lead, assignment=reservation.data.get("script_comparison")
                    )[0],
                    "state": "AUTHORIZED",
                    "state_at": now.isoformat(),
                    "started_at": now.isoformat(),
                    "max_call_seconds": self.settings.max_call_seconds,
                    "provider_id": None,
                    "outcome": None,
                    "transcript_version": 0,
                    "synthetic": self.settings.simulation,
                    "is_test": lead.data["phone"] in TEST_LINES or self.in_test_session(p),
                    "transcription_source": "direct" if self.managed.enabled(p["tenant_id"]) else "webhook",
                },
                key=data["idempotency_key"],
                now=now,
            )
            claim.call_id = call.id
            update(reservation, status="active", call_id=call.id)
            # The persistent agent leg carries the rep's own speech stream for this call.
            update(call, agent_provider_id=agent)
            payload = {
                "to": lead.data["phone"],
                "from": number.data["phone"],
                "link_to": agent,
                "bridge_on_answer": True,
                # Telnyx detection on every attempt; a mailbox beep ends non-final attempts.
                "answering_machine_detection": "premium",
                "timeout_secs": self.configs.read(p["tenant_id"]).get("outbound_ring_seconds", 40),
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
            refuse(self.dial_blockers_in(s, p, rep=False))
            # Lock call, then lead, as other call writers do: the dial claim updates both.
            call = self.required(s, "call_attempts", call_id, p, lock=True)
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
            lead = self.required(s, "leads", call.data["lead_id"], p, lock=True)
            self.refuse_outside_test_line(p, lead)
            number = self.required(s, "number_inventory", call.data["number_id"], p)
            if not self.configs.read(p["tenant_id"]).get("number_pool"):
                # With the shared pool (D26) the caller ID is leased at dial time instead of owned by this seat.
                self.managed.require_number(p["tenant_id"], number.data)
            decision = authorize(self.context(s, {**p, **call.data.get("dial_scope", {})}, lead, number))
            submission_decision = self.record_decision(s, p, lead, decision)
            if not decision.allowed:
                update(command, status="cancelled", reasons=decision.reasons)
                self.finish_call(s, p, call, "policy_cancelled")
                return
            policy = self.required(
                s,
                "policy_versions",
                self.required(s, "campaigns", lead.data["campaign_id"], p).data["policy_id"],
                p,
            )
            ordinal = lead.data.get("attempt_count", 0) + 1
            allowance = max(lead.data.get("attempt_allowance", 0), policy.data.get("max_attempts", 4))
            payload = command.data["payload"]
            update(
                command,
                status="sending",
                attempts=1,
                submitted_at=self.clock().isoformat(),
                sent_at=self.clock().isoformat(),
                lease_owner=owner,
                lease_until=(self.clock() + timedelta(seconds=30)).isoformat(),
                # Restored only if the request provably never reached the carrier.
                prior_attempt={
                    k: lead.data.get(k) for k in ("attempt_count", "last_attempt_at", "next_attempt_at")
                },
            )
            update(
                call,
                **moved(call.data, "dial.sent", self.clock()),
                submission_decision_id=submission_decision.id,
                submitted_at=self.clock().isoformat(),
            )
            update(call, attempt_ordinal=ordinal, attempt_allowance=allowance)
            if dt(lead.data.get("pair_until")) and self.clock() < dt(lead.data["pair_until"]):
                update(call, pair_of=lead.data.get("pair_of"))
            update(lead, pair_until=None, pair_rep_id=None, pair_of=None, pair_opened_at=None)
            update(
                lead,
                attempt_count=ordinal,
                attempt_allowance=allowance,
                last_attempt_at=self.clock().isoformat(),
                next_attempt_at=(self.clock() + timedelta(seconds=retry_seconds(policy.data))).isoformat(),
                # Coverage restarts from this attempt, so tried leads cannot crowd out first calls.
                coverage_due_at=(self.clock() + timedelta(seconds=retry_seconds(policy.data))).isoformat(),
            )
            return command.id, doc(call), lead.data["phone"], payload

    def _dial_failed(self, p, call_id, command_id, owner, exc):
        with self.db.transaction() as s:
            command = self.required(s, "command_outbox", command_id, p)
            s.refresh(command, with_for_update=True)
            call = self.required(s, "call_attempts", call_id, p)
            if owns_effect(command.data, owner, self.clock()) and command.data["status"] == "sending":
                rejected = isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500
                limited = rejected and is_channel_limit(exc.response.status_code, _body(exc.response))
                # Over the account's channel limit nothing reached the prospect: not counted, and this seat waits.
                unsent = isinstance(exc, CarrierNotSubmitted) or limited
                if limited:
                    until = self.clock() + timedelta(seconds=CHANNEL_PAUSE)
                    self.configs.write(p["tenant_id"], {"channel_limited_until": until.isoformat()})
                update(
                    command,
                    status="failed" if rejected or unsent else "uncertain",
                    error=type(exc).__name__,
                    lease_owner=None,
                    lease_until=None,
                )
                if unsent:
                    # Nothing reached the carrier: no contact, so the attempt is not counted.
                    lead = self.required(s, "leads", call.data["lead_id"], p)
                    if lead.data.get("attempt_count") == call.data.get("attempt_ordinal"):
                        update(lead, **command.data.get("prior_attempt", {}))
                    self.finish_call(
                        s, p, call, "carrier_channel_limit" if limited else "carrier_not_submitted"
                    )
                elif rejected:
                    self.finish_call(s, p, call, "provider_rejected")
                elif call.data["state"] == "DIALING" and not call.data.get("provider_id"):
                    update(call, **moved(call.data, "dial.uncertain", self.clock()))
                    emit(s, p["tenant_id"], "call.updated", call, self.clock(), {"state": "RECONCILING"})

    async def _lease_pool_number(self, p, call_id, prospect):
        """D26: the relay picks this dial's caller ID from the shared pool. No answer, no dial: the call is not placed,
        is not counted against the lead, and this seat waits with a named blocker that clears by itself."""
        from app.numbers import pool
        from app.optimization.geography import AREA_STATES

        try:
            phone = await pool.lease(
                self.managed.client, prospect, pool.area_preferences(prospect, AREA_STATES), call_id
            )
        except pool.PoolUnavailable as exc:
            until = self.clock() + timedelta(seconds=30)
            self.configs.write(
                p["tenant_id"], {"pool_blocked_until": until.isoformat(), "pool_blocked": exc.reason}
            )
            raise CarrierNotSubmitted(exc.reason) from exc
        leases = self.configs.read(p["tenant_id"]).get("pool_leases") or []
        self.configs.write(
            p["tenant_id"], {"pool_leases": [*leases, call_id][-200:], "pool_blocked_until": None}
        )
        await asyncio.to_thread(self._record_pool_number, p, call_id, phone)
        return phone

    def _record_pool_number(self, p, call_id, phone):
        with self.db.transaction() as s:
            update(self.required(s, "call_attempts", call_id, p), pool_number=phone, from_number=phone)

    async def release_pool_leases(self, p):
        """End the pool lease of every call that has ended, so the team's 15 numbers are free for the next dials."""
        from app.numbers import pool

        leases = self.configs.read(p["tenant_id"]).get("pool_leases") or []
        if not leases:
            return 0
        ended = []
        for call_id in leases:
            with self.db.transaction(False) as s:
                call = self.scoped_get(s, p, "call_attempts", call_id)
            if call is None or call.data["state"] in TERMINAL:
                await pool.release(self.managed.client, call_id)
                ended.append(call_id)
        if ended:
            current = self.configs.read(p["tenant_id"]).get("pool_leases") or []
            self.configs.write(p["tenant_id"], {"pool_leases": [c for c in current if c not in ended]})
        return len(ended)

    def scoped_get(self, s, p, kind, object_id):
        from app.platform.db import get

        row = get(s, kind, object_id, p["tenant_id"])
        return row if row and row.seller_id == p["seller_id"] else None

    def _dial_completed(self, p, call_id, claimed, owner, receipt):
        command_id, call_data, phone, payload = claimed
        cancel = False
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            current = self.required(s, "command_outbox", command_id, p)
            s.refresh(current, with_for_update=True)
            if current.data["status"] != "completed":
                if current.data["status"] != "sending" or not owns_effect(current.data, owner, self.clock()):
                    return
                self.bind_dial_receipt(s, p, call, receipt["call_control_id"])
            cancel = call.data["state"] not in TERMINAL and (
                call.data["state"] == "ENDING" or self.suppressed(s, p, phone)
            )
            update(
                call,
                **moved(
                    call.data, "call.ringing" if self.settings.simulation else "call.initiated", self.clock()
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
            try:
                provider = await asyncio.to_thread(self.carrier, p["tenant_id"])
            except Exception as exc:
                # Configuration fails before any request exists; nothing can be live.
                raise CarrierNotSubmitted(type(exc).__name__) from exc
            payload = claimed[3]
            if self.configs.read(p["tenant_id"]).get("number_pool"):
                payload = {**payload, "from": await self._lease_pool_number(p, call_id, claimed[2])}
            receipt = await provider.dial(payload)
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
            and command.data.get("status")
            in {"sending", "uncertain", "reconciling", "review_required", "unconfirmed"}
        ):
            update(command, status="completed", provider_id=provider_id, lease_owner=None, lease_until=None)
        stop = self._by_key(s, p, "command_outbox", call.id + ":hangup")
        if stop and stop.data.get("status") == "unconfirmed":
            # A requested end that lacked a carrier reference can now stop the actual leg.
            update(stop, status="pending", provider_id=provider_id, recovery_reason=None)
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
        self.tips.end(call.id)
        if call.data["state"] != "FINALIZED":
            ended = dt(call.data.get("ended_at")) or self.clock()
            update(
                call,
                **moved(call.data, "call.ended", self.clock()),
                ended_at=ended.isoformat(),
                end_reason=reason or "hangup",
            )
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
        from app.billing import settle_call_estimate

        settle_call_estimate(self, s, p, call)
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
            # A recovery hang-up goes ahead only if the pass's decision still holds for the call as it is now.
            if recovery_expected is not None and (
                call.data.get("provider_id") != recovery_expected.get("provider_id")
                or next_step(call.data, self.clock()) not in {"hangup.requested", "call.ended"}
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
                    {
                        "kind": "hangup",
                        "call_id": call.id,
                        "rep_id": call.data.get("rep_id"),
                        "provider_id": provider_id,
                        "status": "pending",
                    },
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
            update(call, **moved(call.data, "hangup.requested", self.clock()))
            self.stop_agent_transcription_in(s, p, call)
            self.tips.end(call.id)
            emit(s, p["tenant_id"], "call.updated", call, self.clock(), {"state": "ENDING"})
            return doc(call), {"id": existing.id, "provider_id": provider_id, "command_id": command_id}

    def _hangup_completed(self, p, call_id, command_id, owner, error=None):
        """Record a sent stop's receipt (if any), then queue wrap-up once the call has ended."""
        with self.db.transaction() as s:
            current = self.required(s, "command_outbox", command_id, p) if command_id else None
            if current:
                s.refresh(current, with_for_update=True)
            if (
                current
                and current.data["status"] == "sending"
                and owns_effect(current.data, owner, self.clock())
            ):
                if error is not None:
                    update(current, status="uncertain", lease_owner=None, lease_until=None)
                else:
                    update(current, status="completed", lease_owner=None, lease_until=None)
                    if self.settings.simulation:
                        call = self.required(s, "call_attempts", call_id, p, lock=True)
                        self.finish_call(s, p, call, "simulated_hangup_receipt")
            call = self.required(s, "call_attempts", call_id, p)
            if call.data["state"] in TERMINAL:
                s.refresh(call, with_for_update=True)
                self.enqueue_aftercall(s, p, call)
            return doc(call)

    async def hangup(self, p, call_id, *, recovery_expected=None):
        owner = uid()
        call, command = await asyncio.to_thread(self._claim_hangup, p, call_id, owner, recovery_expected)
        if not command:
            return call
        error = None
        if command["provider_id"]:
            try:
                provider = await asyncio.to_thread(self.carrier, p["tenant_id"])
                await provider.hangup(command["provider_id"], command["command_id"])
            except Exception as exc:
                error = exc
                logging.getLogger(__name__).warning("Durable hangup pending: %s", type(exc).__name__)
        sent = command["id"] if command["provider_id"] else None
        return await asyncio.to_thread(self._hangup_completed, p, call_id, sent, owner, error)

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
            target = transition(call.data["state"], event_type)
            if payload.get("call_control_id"):
                self.bind_dial_receipt(s, p, call, payload["call_control_id"])
            if target == "ENDED":
                self.finish_call(s, p, call, event_type)
            else:
                update(call, **moved(call.data, event_type, self.clock()))
                emit(s, p["tenant_id"], "call.updated", call, self.clock(), {"state": target})
            return {"duplicate": False, "event_id": event_id, "state": target}
