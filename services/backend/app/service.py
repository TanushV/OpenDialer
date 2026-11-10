import asyncio
import hashlib
import json
import threading
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import delete, or_, select

from app import ops
from app.calling import audio_leg
from app.calling.blockers import refuse, test_line_refusal
from app.calling.state import moved, transition
from app.compliance.phone import normalize_phone
from app.compliance.policy import CallContext, retry_seconds
from app.lead_lists import memberships, set_memberships
from app.leads import intake
from app.leads.record import csv_fields, number_view
from app.optimization.geography import plausible_zones
from app.platform.config import LIMITS, TEST_LINES
from app.platform.ctx import Ctx
from app.platform.db import MODELS, ActiveClaim, audit, doc, emit, get, put, rows, uid, update
from app.platform.errors import dt, fail  # noqa: F401 (other modules import these from here)
from app.platform.jobs import JobQueue
from app.platform.security import ConfigStore, Sessions
from app.privacy import event_metadata
from app.providers.carrier import carrier_factory
from app.providers.simulated import SimulatedCarrier

TEST_LIST = "Test line"


class AppState:
    """The backend's state: no behavior. Every operation is a function in `app.ops`."""

    def __init__(self, db, settings, clock, carrier=None):
        self.db, self.settings, self.clock = db, settings, clock
        self.injected_carrier = carrier
        self.simulated_carrier = carrier or SimulatedCarrier()
        self.configs = ConfigStore(settings.data_dir)
        self.sessions = Sessions(settings.session_seconds)
        self.carriers = carrier_factory(settings, self.configs, self.simulated_carrier)
        self.ctx = Ctx(db, settings, clock, self.configs, lambda workspace: ops.carrier(self, workspace))
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
        from app.calling.watchdog import Watchdog

        self.watchdog = Watchdog()
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


def required(service, s, kind, object_id, p, *, lock=False):
    obj = get(s, kind, object_id, p["tenant_id"], lock=lock)
    if not obj or (obj.seller_id and obj.seller_id != p["seller_id"]):
        fail(404, "Object not found")
    return obj


def scoped(service, s, kind, p):
    return rows(s, kind, p["tenant_id"], p["seller_id"])


def _by_key(service, s, p, kind, key):
    model = MODELS[kind]
    return s.scalar(
        select(model).where(
            model.tenant_id == p["tenant_id"],
            model.seller_id == p["seller_id"],
            model.external_key == key,
        )
    )


def context_rows(service, s, p, kind, value, *conditions, limit=100):
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


def carrier(service, tenant_id):
    return service.carriers(tenant_id, http=getattr(service, "http", None))


def setup(service, data):
    now = service.clock()
    with service.db.transaction() as s:
        if rows(s, "tenants"):
            fail(409, "Workspace already initialized")
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
            **service.sessions.issue(user, now),
            "campaign_id": campaign_id,
            "policy_id": policy_id,
        }


def seed(service, p):
    if not service.settings.simulation:
        fail(403, "Simulation is disabled for this authority")
    now = service.clock()
    with service.db.transaction() as s:
        seller = ops.required(service, s, "sellers", p["seller_id"], p)
        update(seller, approved=True, synthetic=True)
        campaign = ops.scoped(service, s, "campaigns", p)[0]
        if not (campaign.data.get("script") or {}).get("template"):
            from app.preparation.templates import STARTER, parse_markdown

            ops.adopt_script_in(service, s, p, campaign, parse_markdown(STARTER), "simulation_starter")
        policy = ops.required(service, s, "policy_versions", campaign.data["policy_id"], p)
        update(
            policy,
            approved=True,
            synthetic=True,
            permissions=["live", "transcription"],
            review_reference="SIMULATION ONLY",
        )
        if not ops.scoped(service, s, "registry_snapshots", p):
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
            ops.required(service, s, "sellers", p["seller_id"], p),
            callback_test={"at": now.isoformat(), "simulated": True},
        )
        for phone, region in [("+14155550100", "415"), ("+12125550100", "212")]:
            if not any(n.data["phone"] == phone for n in ops.scoped(service, s, "number_inventory", p)):
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
    if not ops.leads(service, p):
        ops.import_leads(
            service,
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


def suppressed(service, s, p, phone):
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
    return any(r.data["phone"] == phone and r.data.get("active", True) and r not in s.deleted for r in cache)


def import_leads(service, p, body):
    from app.optimization.geography import AREA_STATES, normalize_state
    from app.preparation.workflows import company_key

    result = {"imported": 0, "duplicates": 0, "invalid": 0, "errors": [], "leads": [], "preparing": 0}
    created = []
    if len(body["rows"]) > service.settings.max_import_rows:
        fail(413, "Import row limit exceeded")
    headers = list(dict.fromkeys(str(k) for raw in body["rows"] for k in raw))
    column_map = body.get("column_map")
    if column_map is None:
        column_map = intake.guess_map(headers)
    elif problems := intake.check_map(column_map, headers):
        fail(422, {"code": "COLUMN_MAP_INVALID", "errors": problems})
    result["column_map"] = column_map
    now = service.clock()
    with service.db.transaction() as s:
        campaign = ops.required(service, s, "campaigns", body["campaign_id"], p)
        name = body.get("list_name", "").strip()
        update(campaign, lists={name: {}, **campaign.data.get("lists", {})})
        endpoints = {r.data["phone"]: r for r in ops.scoped(service, s, "phone_endpoints", p)}
        existing = {
            (r.data["campaign_id"], r.data["endpoint_id"]): r for r in ops.scoped(service, s, "leads", p)
        }
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
                duplicate = ops.required(
                    service, s, "leads", existing[(body["campaign_id"], endpoint.id)].id, p, lock=True
                )
                names = list(dict.fromkeys([*memberships(duplicate.data), body.get("list_name", "").strip()]))
                update(duplicate, list_names=names)
                emit(s, p["tenant_id"], "lead.updated", duplicate, service.clock())
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
                    "originally_eligible": not ops.suppressed(service, s, p, phone.e164),
                    "status": "eligible",
                    "coverage_due_at": raw.get("coverage_due_at", (now + timedelta(days=1)).isoformat()),
                    "next_attempt_at": now.isoformat(),
                    "attempt_count": 0,
                    "requested_contact": bool(raw.get("requested_contact", False)),
                    "imported_at": now.isoformat(),
                    "synthetic": service.settings.simulation,
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
            result["leads"].append({**doc(lead), "suppressed": ops.suppressed(service, s, p, phone.e164)})
        if (
            body.get("prepare")
            and created
            and ops.lead_script_base_ready(service, s, p)
            and ops.agent_route(service, p)
        ):
            result["preparing"] = ops.request_preparation_in(service, s, p, created)
            update(ops.required(service, s, "sellers", p["seller_id"], p), preparation_user_id=p["user_id"])
        ops.ensure_experiment_cohort(service, s, p, body["campaign_id"])
        emit(s, p["tenant_id"], "campaign.updated", campaign, service.clock())
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
        ops.prepare_tick(service, p)
    return result


def leads(service, p):
    with service.db.transaction(False) as s:
        return [
            {**doc(r), **number_view(r.data), "suppressed": ops.suppressed(service, s, p, r.data["phone"])}
            for r in ops.scoped(service, s, "leads", p)
        ]


def scope_spend(service, s, p, scope):
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
        month = service.clock().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        query = query.where(usage.op_at >= month)
    return int(s.scalar(query)) / 1_000_000


def budget_ok(service, s, p, estimated=0.001, scope="other"):
    """Each lead list may spend up to the per-list budget on AI; other work gets the same per month."""
    budget = ops.scoped(service, s, "budgets", p)[0].data
    limit = Decimal(str(budget.get("per_list_usd", 25.0)))
    spent = Decimal(str(ops.scope_spend(service, s, p, scope)))
    return spent + Decimal(str(estimated)) <= limit


def context(service, s, p, lead, number, *, permission="live", active=False):
    campaign = ops.required(service, s, "campaigns", lead.data["campaign_id"], p)
    policy = ops.required(service, s, "policy_versions", campaign.data["policy_id"], p)
    seller = ops.required(service, s, "sellers", p["seller_id"], p)
    endpoint = ops.required(service, s, "phone_endpoints", lead.data["endpoint_id"], p)
    user = ops.required(service, s, "users", p["user_id"], p)
    config, presence = policy.data, user.data.get("presence", {})
    internal_suppressed = ops.suppressed(service, s, p, lead.data["phone"])
    # Registry imports and consent records are gone (R2.5); neither was ever loaded in practice.
    state_listed, state_current = False, False
    granted, revoked = False, False
    healthy = number and ops.number_readiness_in(service, s, p, number)["outbound_ready"]
    authorized_number = bool(
        number
        and number.seller_id == p["seller_id"]
        and (service.settings.simulation or not number.data.get("synthetic"))
    )
    from app.compliance.operating import screening_capability, screening_snapshot

    national = screening_capability(
        screening_snapshot(service, s, p, lead.data["phone"], internal_suppressed=internal_suppressed),
        lead.data["phone"],
    )
    callback_due = bool(
        not active
        and (due := dt(lead.data.get("callback_due_at")))
        and due <= service.clock()
        and lead.data.get("callback_owner_id") in {None, p["user_id"]}
    )
    pair_open = bool(
        not active
        and (until := dt(lead.data.get("pair_until")))
        and service.clock() < until
        and lead.data.get("pair_rep_id") in {None, p["user_id"]}
    )
    return CallContext(
        now=service.clock(),
        continuing_call=active,
        authorization_reconciled=not seller.data.get("restore_hold", False),
        outreach_hold=bool(
            lead.data.get("outreach_hold")
            or lead.data.get("conversion_goal_achieved")
            or not ops.list_permitted(service, s, p, lead)
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
        route_allowed=lead.data["phone"].startswith("+1") and "1" in config.get("allowed_country_codes", []),
        local_start_hour=config.get("local_start_hour", 8),
        local_end_hour=min(17, config.get("local_end_hour", 17)),
        permission=permission,
        # Live calls carry the speech-to-text the live helper needs; nothing is recorded.
        permitted_permissions=set(config.get("permissions", []))
        | ({"transcription"} if "live" in config.get("permissions", []) else set()),
        own_test_line=ops.own_test_line(service, p, lead),
    )


def record_decision(service, s, p, lead, decision):
    return put(
        s,
        "compliance_decisions",
        p["tenant_id"],
        p["seller_id"],
        {
            "lead_id": lead.id,
            "allowed": decision.allowed,
            "reasons": decision.reasons,
            "at": service.clock().isoformat(),
            "campaign_id": lead.data["campaign_id"],
        },
        now=service.clock(),
    )


def release_unused_reservation_in(service, s, p):
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
        reservation = ops.required(service, s, "reservations", claim.reservation_id, p)
        update(reservation, status="cancelled")
        s.delete(claim)


def presence(service, p, data):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        user = ops.required(service, s, "users", p["user_id"], p)
        if not service.settings.simulation and data.get("agent_call_control_id"):
            verified = audio_leg.find(s, p, provider_id=data["agent_call_control_id"], rep_id=p["user_id"])
            if not verified or verified.data.get("state") != "answered":
                fail(403, "Agent leg must be established and verified by the server")
        if data.get("status") == "paused":
            ops.release_unused_reservation_in(service, s, p)
        starting = (
            data.get("status") == "ready" and (user.data.get("presence") or {}).get("status") != "ready"
        )
        update(user, presence={**user.data.get("presence", {}), **data, "at": service.clock().isoformat()})
        presence = user.data["presence"]
    if starting:
        from app import crm

        crm.schedule_syncs(
            service, p, starting=True
        )  # Start calling syncs every HubSpot list first (section 6).
    return presence


def reachable(service, p, registered):
    with service.db.transaction() as s:
        user = ops.required(service, s, "users", p["user_id"], p)
        update(user, reachable={"registered": bool(registered), "at": service.clock().isoformat()})
        return user.data["reachable"]


def _expirable(service):
    # Unstarted reservations, and inbound routing claims (keyed by their task) past the
    # carrier call time limit, which no live inbound leg can outlast.
    return or_(ActiveClaim.call_id.is_(None), ActiveClaim.call_id == ActiveClaim.reservation_id)


def _expired_claims(service):
    """Lapsed claims that no carrier leg can still need; one row per rep at most."""
    return select(ActiveClaim.id).where(ops._expirable(service), ActiveClaim.expires_at <= service.clock())


def _expire_claims(service, s):
    # One bounded statement instead of loading every tenant's claims. The outer
    # predicate is re-checked on PostgreSQL, so a claim that just started a call stays.
    s.execute(
        delete(ActiveClaim)
        .where(
            ActiveClaim.id.in_(ops._expired_claims(service).limit(500)),
            ops._expirable(service),
            ActiveClaim.expires_at <= service.clock(),
        )
        .execution_options(synchronize_session=False)
    )


def test_lines(service, p):
    """The owner's test line, and the test phone typed in Settings (section 10)."""
    from app.compliance.phone import normalize_phone

    typed = normalize_phone(service.configs.read(p["tenant_id"]).get("test_phone") or "").e164
    return TEST_LINES | ({typed} if typed else set())


def test_phone(service, p):
    """The test phone saved in Settings, or None."""
    return normalize_phone(service.configs.read(p["tenant_id"]).get("test_phone") or "").e164 or None


def in_test_session(service, p):
    # A test session with no saved test phone would dial no one, so it does not exist.
    return bool(service.configs.read(p["tenant_id"]).get("test_session") and ops.test_phone(service, p))


def own_test_line(service, p, lead):
    """The rep's own test phone during a test call: calling hours and attempt limits do not apply to it."""
    return ops.in_test_session(service, p) and lead.data.get("phone") in ops.test_lines(service, p)


def refuse_outside_test_line(service, p, lead):
    refuse(
        test_line_refusal(
            lead.data["phone"],
            test_session=ops.in_test_session(service, p),
            test_lines=ops.test_lines(service, p),
        )
    )


def start_test_session(service, p):
    """Test call: the saved test phone becomes the one callable lead, whatever state an older copy was in.

    An existing lead with that number (archived, paused, called before, on another list) is put back on the
    Test line list and made due now; otherwise it is created. The session ends when the test call is saved.
    """
    phone = ops.test_phone(service, p)
    if not phone:
        fail(409, "Type your phone number in Test phone first")
    with service.db.transaction(False) as s:
        campaign = next(iter(ops.scoped(service, s, "campaigns", p)), None)
        # R11.7: the owner's HubSpot test contact, when it has this number, is the one dialed, so its calls land there.
        same = sorted(
            (row for row in ops.scoped(service, s, "leads", p) if row.data.get("phone") == phone),
            key=lambda row: row.data.get("source") != "crm",
        )
        existing = same[0] if same else None
    if not campaign:
        fail(409, "Import a lead list first")
    if existing is None:
        ops.import_leads(
            service,
            p,
            {
                "campaign_id": campaign.id,
                "list_name": TEST_LIST,
                "rows": [{"name": "Your test phone", "phone": phone, "company": "Test call"}],
                "prepare": False,
            },
        )
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        lead = sorted(
            (row for row in ops.scoped(service, s, "leads", p) if row.data.get("phone") == phone),
            key=lambda row: row.data.get("source") != "crm",
        )[0]
        set_memberships(lead, [TEST_LIST, *memberships(lead.data)])
        update(
            lead,
            archived=False,
            dial_paused=False,
            # An older copy may still be held (a delivery test, a goal review): the rep's own phone is always
            # callable in a test session; only an opt-out still stops it.
            outreach_hold=None,
            status="eligible",
            next_attempt_at=None,
            pair_opened_at=None,
            due_at=None,
        )
        campaign = ops.required(service, s, "campaigns", lead.data["campaign_id"], p, lock=True)
        catalog = dict(campaign.data.get("lists", {}))
        catalog[TEST_LIST] = {**catalog.get(TEST_LIST, {}), "archived": False, "paused": False}
        update(campaign, lists=catalog)
        emit(s, p["tenant_id"], "lead.updated", lead, service.clock())
        lead_id = lead.id
    service.configs.write(p["tenant_id"], {"test_session": True})
    return {"test_session": True, "lead_id": lead_id, "phone": phone}


def end_test_session(service, p):
    service.configs.write(p["tenant_id"], {"test_session": False})


def suppress(service, p, phone, reason, *, event_id=None, session=None):
    from app.compliance.suppressions import record

    return record(service, p, phone, reason, event_id=event_id, session=session)


def event(service, p, event_id, event_type, call_id, payload=None, *, session=None):
    payload = payload or {}
    canonical = json.dumps({"event_type": event_type, "call_id": call_id, "payload": payload}, sort_keys=True)
    hashed = hashlib.sha256(canonical.encode()).hexdigest()
    with service.db.transaction(session=session) as s:
        existing = ops._by_key(service, s, p, "provider_events", event_id)
        if existing:
            if existing.data["hash"] != hashed:
                fail(409, "Provider event ID collision")
            return {"duplicate": True, "event_id": event_id}
        call = ops.required(service, s, "call_attempts", call_id, p)
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
                "received_at": service.clock().isoformat(),
            },
            key=event_id,
            now=service.clock(),
        )
        from app.measurement import call_timing

        timing = call_timing(call.data, event_type, payload, now=service.clock())
        if timing:
            update(call, **timing)
        target = transition(call.data["state"], event_type)
        if payload.get("call_control_id"):
            ops.bind_dial_receipt(service, s, p, call, payload["call_control_id"])
        if target == "ENDED":
            ops.finish_call(service, s, p, call, event_type)
        else:
            update(call, **moved(call.data, event_type, service.clock()))
            emit(s, p["tenant_id"], "call.updated", call, service.clock(), {"state": target})
        return {"duplicate": False, "event_id": event_id, "state": target}
