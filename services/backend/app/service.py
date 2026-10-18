import base64
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import delete, select

from app.compliance.phone import normalize_phone
from app.compliance.policy import CallContext, authorize
from app.db import MODELS, ActiveClaim, audit, doc, emit, get, put, rows, uid, update
from app.jobs.queue import JobQueue, aware
from app.optimization.numbering import select_number
from app.privacy import event_metadata
from app.providers.simulated import SimulatedCarrier, UncertainCarrierResult
from app.providers.telnyx import TelnyxCarrier
from app.scheduling.queue import ReservationPlanner
from app.security import PASSWORDS, Vault, issue_session
from app.telephony.reducer import TERMINAL, reduce_state


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
        self.jobs = JobQueue(db, clock)
        from app.privacy import EphemeralBuffer

        self.raw = EphemeralBuffer(clock=clock)
        from app.coaching.engine import CoachEngine

        self.coach = CoachEngine()

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
        return any(
            r.data["phone"] == phone and r.data.get("active", True) for r in self.scoped(s, "suppressions", p)
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
        snapshots = [r for r in self.scoped(s, "registry_snapshots", p) if r.data.get("published")]
        national = [r for r in snapshots if r.data.get("kind") == "national"]
        registry = max(national, key=lambda r: r.data["refreshed_at"], default=None)
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
            and (self.clock() - dt(r.data["refreshed_at"])).total_seconds() <= 31 * 86400
        ]
        state_current = bool(state)

        def registry_contains(snapshot):
            if snapshot.data.get("bitmap"):
                from app.compliance.registry import RegistryBitmap

                return RegistryBitmap.load(
                    base64.b64decode(snapshot.data["bitmap"]), snapshot.data["checksum"]
                ).contains(lead.data["phone"])
            return lead.data["phone"] in snapshot.data.get("listed_numbers", [])

        consents = [
            r.data for r in self.scoped(s, "consent_evidence", p) if r.data.get("endpoint_id") == endpoint.id
        ]
        revoked = any(c.get("revoked") for c in consents)
        granted = any(permission in c.get("permissions", []) and not c.get("revoked") for c in consents)
        registry_valid = bool(registry and (self.settings.simulation or not registry.data.get("synthetic")))
        listed = lead.data["phone"] in (registry.data.get("listed_numbers", []) if registry_valid else [])
        if registry_valid and registry.data.get("bitmap"):
            from app.compliance.registry import RegistryBitmap

            listed = RegistryBitmap.load(
                base64.b64decode(registry.data["bitmap"]), registry.data["checksum"]
            ).contains(lead.data["phone"])
        healthy = number and number.data["lifecycle"] in {"healthy", "limited_real_use"}
        authorized_number = bool(
            number
            and number.seller_id == p["seller_id"]
            and (self.settings.simulation or not number.data.get("synthetic"))
        )
        from app.compliance.operating import cached_screen

        prepaid = cached_screen(self, s, p, lead.data["phone"])
        national_at = dt(registry.data["refreshed_at"]) if registry_valid else None
        if prepaid:
            national_at = max(filter(None, [national_at, dt(prepaid["checked_at"])]))
            listed = listed or prepaid["listed"]
        return CallContext(
            now=self.clock(),
            seller_approved=seller.data.get("approved", False),
            authorization_reconciled=not seller.data.get("restore_hold", False),
            policy_published=config.get("approved", False)
            and (self.settings.simulation or not config.get("synthetic")),
            endpoint_valid=endpoint.data["format_status"] == "valid"
            and endpoint.data.get("reachability_status") != "invalid_endpoint"
            and not endpoint.data.get("reassignment_review_required", False),
            ownership=endpoint.data.get("ownership_status", "unknown"),
            timezone=lead.data.get("timezone"),
            timezone_confidence=lead.data.get("timezone_confidence", "unknown"),
            registry_required=True,
            registry_checked_at=national_at,
            registry_covered=bool(prepaid)
            or (registry_valid and lead.data["phone"][2:5] in registry.data["npas"]),
            registry_listed=listed,
            state_registry_required=config.get("state_registry_required", False),
            state_registry_current=state_current,
            state_registry_listed=any(registry_contains(r) for r in state),
            internal_suppressed=self.suppressed(s, p, lead.data["phone"]),
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

    def presence(self, p, data):
        with self.db.transaction() as s:
            user = self.required(s, "users", p["user_id"], p)
            if not self.settings.simulation and data.get("agent_call_control_id"):
                verified = [
                    r
                    for r in self.scoped(s, "call_legs", p)
                    if r.data.get("provider_id") == data["agent_call_control_id"]
                    and r.data.get("rep_id") == p["user_id"]
                    and r.data.get("state") == "answered"
                    and r.data.get("kind") == "agent"
                ]
                if not verified:
                    fail(403, "Agent leg must be established and verified by the server")
            update(user, presence={**user.data.get("presence", {}), **data, "at": self.clock().isoformat()})
            return user.data["presence"]

    def _expire_claims(self, s):
        for claim in list(s.scalars(select(ActiveClaim))):
            if not claim.call_id and aware(claim.expires_at) <= self.clock():
                s.delete(claim)

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

    async def start_call(self, p, data):
        now = self.clock()
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            previous = next(
                (c for c in self.scoped(s, "call_attempts", p) if c.external_key == data["idempotency_key"]),
                None,
            )
            if previous:
                if (
                    previous.data["rep_id"] != p["user_id"]
                    or previous.data["reservation_id"] != data["reservation_id"]
                ):
                    fail(409, "Idempotency key belongs to another intent")
                return doc(previous)
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
                    "treatment_variant_id": reservation.data.get("treatment_variant_id"),
                    "feature_snapshot_id": reservation.data.get("feature_snapshot_id"),
                    "decision_id": reservation.data.get("policy_decision_id"),
                    "allocation_decision_id": reservation.data.get("allocation_decision_id"),
                    "coaching_comparison": reservation.data.get("coaching_comparison"),
                    "state": "AUTHORIZED",
                    "started_at": now.isoformat(),
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
        await self.dispatch_call(p, call.id)
        with self.db.transaction(False) as s:
            return doc(self.required(s, "call_attempts", call.id, p))

    async def dispatch_call(self, p, call_id):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            call = self.required(s, "call_attempts", call_id, p)
            command = next(
                r
                for r in self.scoped(s, "command_outbox", p)
                if r.data["call_id"] == call_id and r.data["kind"] == "dial"
            )
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
            self.journal_authorization(p, call, payload)
            update(command, status="sending", attempts=1, submitted_at=self.clock().isoformat())
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
            update(
                lead,
                attempt_count=lead.data.get("attempt_count", 0) + 1,
                last_attempt_at=self.clock().isoformat(),
                next_attempt_at=(
                    self.clock() + timedelta(seconds=policy.data.get("min_retry_seconds", 3600))
                ).isoformat(),
            )
        try:
            receipt = await self.carrier(p["tenant_id"]).dial(payload)
        except UncertainCarrierResult:
            with self.db.transaction() as s:
                command = self.required(s, "command_outbox", command.id, p)
                call = self.required(s, "call_attempts", call_id, p)
                update(command, status="uncertain")
                update(call, state="RECONCILING")
                emit(s, p["tenant_id"], "call.updated", call, self.clock(), {"state": "RECONCILING"})
            return
        except Exception as e:
            with self.db.transaction() as s:
                update(
                    self.required(s, "command_outbox", command.id, p), status="failed", error=type(e).__name__
                )
                self.finish_call(s, p, self.required(s, "call_attempts", call_id, p), "provider_failure")
            return
        self.journal_authorization(p, call, payload, provider_id=receipt["call_control_id"])
        cancel = False
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            update(
                self.required(s, "command_outbox", command.id, p),
                status="completed",
                provider_id=receipt["call_control_id"],
            )
            cancel = call.data["state"] in TERMINAL or self.suppressed(s, p, lead.data["phone"])
            update(
                call,
                provider_id=receipt["call_control_id"],
                state="ENDED" if cancel else ("RINGING" if self.settings.simulation else "DIALING"),
            )
            put(
                s,
                "call_legs",
                p["tenant_id"],
                p["seller_id"],
                {
                    "call_id": call.id,
                    "provider_id": receipt["call_control_id"],
                    "kind": "prospect",
                    "state": call.data["state"],
                },
                key=receipt["call_control_id"],
                now=self.clock(),
            )
            emit(s, p["tenant_id"], "call.updated", call, self.clock(), {"state": call.data["state"]})
        if cancel:
            await self.carrier(p["tenant_id"]).hangup(receipt["call_control_id"], call_id + ":suppression")

    def finish_call(self, s, p, call, reason=None):
        if call.data["state"] != "FINALIZED":
            update(call, state="ENDED", ended_at=self.clock().isoformat(), end_reason=reason or "hangup")
        for claim in list(s.scalars(select(ActiveClaim).where(ActiveClaim.call_id == call.id))):
            s.delete(claim)
        emit(s, p["tenant_id"], "call.updated", call, self.clock(), {"state": call.data["state"]})

    async def hangup(self, p, call_id):
        with self.db.transaction() as s:
            call = self.required(s, "call_attempts", call_id, p)
            if p["role"] == "rep" and call.data["rep_id"] != p["user_id"]:
                fail(403, "Call belongs to another rep")
            if call.data["state"] in TERMINAL:
                return doc(call)
            provider_id = call.data.get("provider_id")
            command_id = call.id + ":hangup"
            existing = next(
                (c for c in self.scoped(s, "command_outbox", p) if c.external_key == command_id), None
            )
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
            update(call, state="ENDING")
            emit(s, p["tenant_id"], "call.updated", call, self.clock(), {"state": "ENDING"})
        if provider_id:
            try:
                await self.carrier(p["tenant_id"]).hangup(provider_id, command_id)
                with self.db.transaction() as s:
                    update(self.required(s, "command_outbox", existing.id, p), status="completed")
                    if self.settings.simulation:
                        self.finish_call(
                            s, p, self.required(s, "call_attempts", call_id, p), "simulated_hangup_receipt"
                        )
            except Exception as exc:
                with self.db.transaction() as s:
                    update(self.required(s, "command_outbox", existing.id, p), status="uncertain")
                logging.getLogger(__name__).warning("Durable hangup pending: %s", type(exc).__name__)
        with self.db.transaction(False) as s:
            call = self.required(s, "call_attempts", call_id, p)
            result = doc(call)
        if result["state"] in TERMINAL:
            self.jobs.enqueue(
                "postcall",
                {"call_id": call_id, "seller_id": p["seller_id"], "user_id": p["user_id"]},
                "postcall:" + call_id + ":" + str(call.version),
                tenant_id=p["tenant_id"],
            )
        return result

    def suppress(self, p, phone, reason, *, event_id=None):
        normalized = normalize_phone(phone)
        if not normalized.e164:
            fail(422, "Valid phone endpoint required")
        phone, now = normalized.e164, self.clock()
        journal = {
            "event_id": event_id or uid(),
            "tenant_id": p["tenant_id"],
            "seller_id": p["seller_id"],
            "phone": phone,
            "reason": reason,
            "at": now.isoformat(),
        }
        # Append and fsync before DB acknowledgement; replay on restore. Encrypted independently of database backup.
        path = self.settings.data_dir / "suppression-journal.enc"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as f:
            f.write(self.vault.cipher.encrypt(json.dumps(journal).encode()) + b"\n")
            f.flush()
            os.fsync(f.fileno())
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            existing = next((r for r in self.scoped(s, "suppressions", p) if r.data["phone"] == phone), None)
            if existing:
                if not existing.data.get("permanent"):
                    update(existing, reason=reason, permanent=reason != "temporary_hold", active=True)
                result = existing
            else:
                result = put(
                    s,
                    "suppressions",
                    p["tenant_id"],
                    p["seller_id"],
                    {
                        "phone": phone,
                        "reason": reason,
                        "permanent": reason != "temporary_hold",
                        "active": True,
                        "effective_at": now.isoformat(),
                        "origin_event_id": journal["event_id"],
                    },
                    key=phone,
                    now=now,
                )
            for command in self.scoped(s, "command_outbox", p):
                if (
                    command.data.get("kind") == "dial"
                    and command.data.get("status") == "pending"
                    and command.data["payload"]["to"] == phone
                ):
                    update(command, status="cancelled", reason="INTERNAL_SUPPRESSION")
                    call = self.required(s, "call_attempts", command.data["call_id"], p)
                    self.finish_call(s, p, call, "opt_out")
            for reservation in self.scoped(s, "reservations", p):
                if reservation.data["status"] == "reserved":
                    lead = self.required(s, "leads", reservation.data["lead_id"], p)
                    if lead.data["phone"] == phone:
                        update(reservation, status="cancelled")
                        s.execute(delete(ActiveClaim).where(ActiveClaim.reservation_id == reservation.id))
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "endpoint.suppressed",
                result.id,
                now,
                {"reason": reason},
            )
            emit(s, p["tenant_id"], "suppression.created", result, now)
            return doc(result)

    def event(self, p, event_id, event_type, call_id, payload=None):
        payload = payload or {}
        canonical = json.dumps(
            {"event_type": event_type, "call_id": call_id, "payload": payload}, sort_keys=True
        )
        hashed = hashlib.sha256(canonical.encode()).hexdigest()
        with self.db.transaction() as s:
            existing = next(
                (r for r in self.scoped(s, "provider_events", p) if r.external_key == event_id), None
            )
            if existing:
                if existing.data["hash"] != hashed:
                    fail(409, "Provider event ID collision")
                return {"duplicate": True, "event_id": event_id}
            call = self.required(s, "call_attempts", call_id, p)
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
            target = reduce_state(call.data["state"], event_type)
            if target == "ENDED":
                self.finish_call(s, p, call, event_type)
            else:
                update(call, state=target)
                if payload.get("call_control_id") and not call.data.get("provider_id"):
                    update(call, provider_id=payload["call_control_id"])
                    for command in self.scoped(s, "command_outbox", p):
                        if (
                            command.data.get("call_id") == call.id
                            and command.data.get("kind") == "dial"
                            and command.data.get("status") in {"sending", "uncertain"}
                        ):
                            update(command, status="completed", provider_id=payload["call_control_id"])
                emit(s, p["tenant_id"], "call.updated", call, self.clock(), {"state": target})
            return {"duplicate": False, "event_id": event_id, "state": target}

    def journal_authorization(self, p, call, payload, provider_id=None):
        record = {
            "call_id": call.id,
            "tenant_id": p["tenant_id"],
            "seller_id": p["seller_id"],
            "user_id": p["user_id"],
            "at": self.clock().isoformat(),
            "provider_id": provider_id,
            "intent": call.data,
            "command_id": payload["command_id"],
            "command_hash": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        }
        path = self.settings.data_dir / "authorization-journal.enc"
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "ab") as journal:
            journal.write(self.vault.cipher.encrypt(json.dumps(record).encode()) + b"\n")
            journal.flush()
            os.fsync(journal.fileno())
