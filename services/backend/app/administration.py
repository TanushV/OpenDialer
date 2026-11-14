import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, or_, select, text

from app import ops, test_list
from app.calling import audio_leg
from app.calling.blockers import Facts, mark_stuck
from app.calling.blockers import blockers as dial_gate
from app.calling.state import TERMINAL
from app.platform.config import PROVIDER_DEFAULTS, CapabilityStatus
from app.platform.db import MODELS, RETIRED_TABLES, audit, doc, emit, put, rows, uid, update
from app.service import dt, fail
from app.telephony.recovery import call_receipt

NUMBER_BLOCKERS = {
    "NO_CALLER_NUMBER",
    "CALLER_NUMBER_ROUTE_REQUIRED",
    "CALLER_NUMBER_CALLBACK_TEST_REQUIRED",
    "CALLER_NUMBER_PAUSED",
}
NUMBER_REASONS = {"NUMBER_UNHEALTHY", "NUMBER_NOT_AUTHORIZED"}
NO_LEADS = (
    "No one to call right now. Leads still being researched show on the Leads page; "
    "others may be outside calling hours or already called today."
)
SETUP_REASONS = {"REP_NOT_READY", "AUDIO_NOT_READY", "LIVE_OBLIGATION", "RESERVATION_EXISTS"}
TIMING_REASONS = {"OUTSIDE_CALLING_WINDOW", "NOT_DUE", "CADENCE_LIMIT", "ENDPOINT_CLAIMED"}
REGISTRY_REASONS = {"REGISTRY_NOT_CLEARED_FOR_ENDPOINT", "SCREENING_PENDING"}
# Why nobody can be called, in the order a rep can act on it. The first reason present is shown, then the rest briefly.
WHY_NOBODY = [
    (
        {"CRM_SYNC_STALE"},
        "Some HubSpot leads need a sync before calling.",
        "leads",
    ),
    (
        {"DNC_NOT_CHECKED"},
        "HubSpot leads are waiting for do-not-call screening. Check RealValidito in Settings.",
        "settings",
    ),
    (
        {"REGISTRY_NOT_CLEARED_FOR_ENDPOINT", "SCREENING_PENDING", "STATE_REGISTRY_MISSING"},
        "Some leads wait for do-not-call screening. Check screening setup in Settings.",
        "settings",
    ),
    (
        {"TIMEZONE_UNCERTAIN"},
        "Some leads need a state or time zone to determine their calling hours.",
        "leads",
    ),
    (
        {"NUMBER_CONFLICT", "NUMBER_BAD", "NUMBER_DEAD", "NO_VALID_NUMBER", "NEEDS_NUMBER"},
        "Some leads have no usable number. Review shows which leads need attention.",
        "attention",
    ),
    (
        {"OUTSIDE_CALLING_WINDOW"},
        "Some leads are outside calling hours in their time zone.",
        None,
    ),
    (
        {"NOT_DUE", "CADENCE_LIMIT"},
        "Some leads are waiting for their next scheduled attempt.",
        None,
    ),
    (
        {"COMPANY_CONTACTED_TODAY"},
        "Some companies were already called today; their other contacts wait until tomorrow.",
        None,
    ),
    ({"ATTEMPT_LIMIT"}, "Some leads reached their attempt limit.", None),
    ({"OUTREACH_HOLD"}, "Some leads are paused, removed, on a paused list, or already booked.", "leads"),
    (
        {"INTERNAL_SUPPRESSION", "WRONG_PARTY", "INVALID_ENDPOINT"},
        "Some leads opted out or have a wrong number.",
        None,
    ),
]


NO_LEAD_DUE_NOW = "No lead is inside calling hours and due right now; the queue offers them when they are."


def capability_statuses(
    conf, policy, screening, *, simulation, source_version, ingress=None, inference=False
):
    """Evidence states; a configured credential never certifies a funded/live integration."""
    values = []

    def add(identity, state, blocks, reasons, remedy, evidence_at=None):
        values.append(
            CapabilityStatus(
                id=identity,
                state=state,
                blocks=blocks,
                reason_codes=reasons,
                remedy=remedy,
                evidence_at=evidence_at,
                source_version=source_version,
            ).model_dump()
        )

    carrier = all(conf.get(k) for k in ("telnyx_api_key", "telnyx_connection_id", "telnyx_public_key"))
    add(
        "telephony",
        "software_verified" if simulation else "configured" if carrier else "unconfigured",
        "none" if simulation or carrier else "voice",
        ["SIMULATION_ONLY"]
        if simulation
        else ["LIVE_CARRIER_ACCEPTANCE_REQUIRED"]
        if carrier
        else ["TELNYX_NOT_CONFIGURED"],
        "Configure Telnyx credentials, approved traffic and ingress, then verify owned test destinations.",
    )
    privacy = policy.get("transcription_privacy_verified") and policy.get("transcription_privacy_reference")
    add(
        "transcription",
        "software_verified" if simulation else "configured" if privacy and carrier else "unavailable",
        "none" if simulation or privacy and carrier else "transcription",
        ["SIMULATION_ONLY"]
        if simulation
        else ["LIVE_STT_ACCEPTANCE_REQUIRED"]
        if privacy and carrier
        else ["STT_PRIVACY_OR_CARRIER_MISSING"],
        "Record selected STT engine/account retention evidence and configure Telnyx. Voice and manual notes remain separate.",
    )
    helper = conf.get("coach_provider", PROVIDER_DEFAULTS.coach_provider) == "openrouter" and bool(
        conf.get("openrouter_api_key")
    )
    add(
        "live_helper",
        "software_verified" if simulation else "configured" if helper else "unconfigured",
        "none" if simulation or helper else "help",
        ["SIMULATION_ONLY"]
        if simulation
        else ["FUNDED_CEREBRAS_ACCEPTANCE_REQUIRED"]
        if helper
        else ["CEREBRAS_NOT_CONFIGURED"],
        "Add an OpenRouter key for the pinned Cerebras ZDR route; use the prepared script when help is unavailable.",
    )
    screening_ready = bool(screening.get("cleared_count"))
    add(
        "national_dnc",
        "software_verified"
        if simulation
        else "configured"
        if screening_ready
        else "degraded"
        if screening.get("configured")
        else "unconfigured",
        "none" if screening_ready or not screening.get("enabled") else "voice",
        ["SIMULATION_ONLY"]
        if simulation
        else ["SCREENING_OFF"]
        if not screening.get("enabled")
        else list(
            screening.get("reason_codes")
            or (["CURRENT_AUTHORIZED_COVERAGE"] if screening_ready else ["SCREENING_PENDING"])
        ),
        "Import fresh seller-authorized coverage or authorize a prepaid screening batch. Internal opt-outs always block.",
    )
    url = conf.get("webhook_url", "")
    proof = ingress or {}
    route_current = bool(
        proof.get("endpoint_digest") == hashlib.sha256(url.encode()).hexdigest()
        and proof.get("routing") == "software_verified"
    )
    add(
        "local_ingress",
        "software_verified" if route_current else "configured" if url else "unconfigured",
        "none" if url else "voice",
        ["ROUTE_PROOF_ONLY"]
        if route_current
        else ["INGRESS_CHECK_REQUIRED"]
        if url
        else ["INGRESS_NOT_CONFIGURED"],
        "Configure the webhook-only HTTPS route to the current local port, then run the route check. Keep the authority awake.",
        proof.get("at") if route_current else None,
    )
    add(
        "offline_callback",
        "unavailable",
        "none",
        ["OFFLINE_FALLBACK_UNAVAILABLE"],
        "Keep the selected authority online or use an existing always-on connected host. An operator note is not carrier failover proof.",
    )
    # The approved generic script always remains available, so preparation never blocks calling.
    add(
        "preparation",
        "software_verified" if simulation else "configured" if inference else "degraded",
        "none",
        ["SIMULATION_ONLY"]
        if simulation
        else ["LIVE_INFERENCE_ACCEPTANCE_REQUIRED"]
        if inference
        else ["APPROVED_FALLBACK_ONLY"],
        "Publish the seller template; personalized research and scripts additionally require configured inference.",
    )
    return values


def dial_blockers_in(service, s, p, *, rep, integrity_ok=True):
    """Everything that stops calling now (`calling/blockers.py`). `rep` adds the rep's own audio."""
    conf = service.configs.read(p["tenant_id"])
    seller = ops.required(service, s, "sellers", p["seller_id"], p)
    presence = ops.required(service, s, "users", p["user_id"], p).data.get("presence", {}) if rep else {}
    numbers = ops.scoped(service, s, "number_inventory", p)
    audio = None
    if rep:
        agent = presence.get("agent_call_control_id")
        audio = bool(agent and presence.get("audio_ready") and presence.get("telephony_ready"))
        if audio and not service.settings.simulation:
            leg = audio_leg.find(s, p, provider_id=agent, rep_id=p["user_id"])
            audio = bool(leg and leg.data.get("state") == "answered")
    return ops._mark_stuck(
        service,
        dial_gate(
            Facts(
                carrier_setup_required=not service.settings.simulation,
                integrity_ok=integrity_ok,
                restore_hold=bool(seller.data.get("restore_hold")),
                telnyx_configured=bool(conf.get("telnyx_api_key") and conf.get("telnyx_connection_id")),
                signature_key=bool(conf.get("telnyx_public_key")),
                audio_setup=bool(conf.get("sip_username") and conf.get("telephony_credential_id")),
                connection=service.managed.connection_code(p["tenant_id"]),
                campaign=bool(next(iter(ops.scoped(service, s, "campaigns", p)), None)),
                number=ops.number_setup_blocker(service, s, p, numbers),
                test_number=next(
                    (
                        n.data["phone"]
                        for n in numbers
                        if n.data.get("identity_configured") or n.data.get("shared_pool")
                    ),
                    None,
                ),
                shared_pool=service.managed.pool is not None,
                pending_optouts=presence.get("pending_optouts", 0),
                rep_audio_ready=audio,
                channel_limited=(conf.get("channel_limited_until") or "") > service.clock().isoformat(),
            )
        ),
    )


def _mark_stuck(service, found):
    return mark_stuck(found, service.watchdog.stuck)


async def watch_calling(service, p):
    """R9E.6: repair a self-clearing blocker that outlived its promise, and log it."""
    import logging

    with service.db.transaction(False) as s:
        codes = [b.code for b in ops.dial_blockers_in(service, s, p, rep=False)]
    for code in service.watchdog.observe(codes, time.monotonic()):
        logging.getLogger(__name__).warning("Watchdog: %s outlived its promise; repairing it", code)
        await ops.repair_blocker(service, p, code)


async def repair_blocker(service, p, code):
    managed = service.managed
    if code == "CONNECTION_SETUP_CHANGING":
        if not managed.edit_lock.locked():
            managed.configuring = False
    elif code.startswith("CONNECTION_") and managed.enabled(p["tenant_id"]):
        await managed.close()
        await managed.start()
    elif code == "TELNYX_CALL_LIMIT_REACHED":
        service.configs.write(p["tenant_id"], {"channel_limited_until": None})


def setup_rows(service, p):
    """Settings: one true status and one row per thing to set (section 7, R7.2)."""
    from app.calling.blockers import CATALOG
    from app.calling.setup import rows
    from app.compliance.phone import normalize_phone
    from app.providers.codex import profile_home

    conf = service.configs.read(p["tenant_id"])
    live = {}
    if service.settings.simulation:
        live["connection"] = (True, "Simulated workspace: call events are simulated")
    elif not service.managed.enabled(p["tenant_id"]):
        live["connection"] = (False, "Import your calling setup file to connect call events", False)
    else:
        code = service.managed.connection_code(p["tenant_id"])
        live["connection"] = (code is None, "Connected" if code is None else CATALOG[code][0])
    home = profile_home(service.settings.data_dir, p["user_id"])
    signed_in = (home / "auth.json").is_file() and (home / "account.json").is_file()
    live["chatgpt"] = (signed_in, "Signed in on this Mac" if signed_in else "Not signed in", signed_in)
    phone = normalize_phone(conf.get("test_phone") or "")
    live["test_phone"] = (
        bool(phone.e164),
        phone.e164 or "Add your own number for test calls",
        bool(conf.get("test_phone")),
    )
    with service.db.transaction(False) as s:
        numbers = ops.scoped(service, s, "number_inventory", p)
        code = ops.number_setup_blocker(service, s, p, numbers)
        ready = sum(1 for n in numbers if n.data.get("lifecycle") in {"healthy", "limited_real_use"})
        live["numbers"] = (
            code is None,
            f"{ready} ready" if code is None else CATALOG[code][0],
            bool(numbers),
        )
        campaign = next(iter(ops.scoped(service, s, "campaigns", p)), None)
        script = (campaign.data.get("script") or {}) if campaign else {}
        live["script"] = (
            bool(script.get("template")),
            f"Version {script.get('version')} saved" if script.get("template") else "No script saved yet",
            bool(script.get("template")),
        )
        policy = (
            ops.required(service, s, "policy_versions", campaign.data["policy_id"], p).data
            if campaign
            else {}
        )
        start, end = policy.get("local_start_hour", 9), policy.get("local_end_hour", 17)
        valid = isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= 24
        live["hours"] = (
            valid,
            f"{start}:00 to {end}:00 in each lead's time" if valid else "Fix the hours",
        )
        first = next(iter(ops.dial_blockers_in(service, s, p, rep=False)), None)
    return {
        "status": first.detail() if first else None,
        "rows": rows(conf, conf.get("_setup_checks") or {}, live),
    }


async def check_setup_row(service, p, row_id):
    """Run the real check behind one row and store its result with the value it checked (R7.3)."""
    from fastapi import HTTPException

    from app.calling import setup
    from app.providers import openrouter, telnyx

    spec = setup.BY_ID.get(row_id)
    if not spec:
        raise HTTPException(404, "Unknown setting")
    conf = service.configs.read(p["tenant_id"])
    if row_id in setup.LIVE:
        return ops.setup_rows(service, p)
    if not all(conf.get(field) for field in spec.fields):
        raise HTTPException(409, f"Add the {spec.label} first")
    http = getattr(service, "http", None)
    if service.settings.simulation:
        ok, detail = True, "Simulated workspace: not checked with the provider"
    elif row_id == "telnyx":
        ok, detail = await telnyx.check_key(conf["telnyx_api_key"], http)
    elif row_id == "openrouter":
        ok, detail = await openrouter.check_key(conf["openrouter_api_key"], http)
    elif row_id == "realvalidito":
        from app.providers.realvalidito import DNCUnavailable, RealValidito

        try:
            credits = await RealValidito(
                conf["realvalidito_api_key"], conf["realvalidito_api_secret"], http=http
            ).credits()
            ok, detail = True, f"{credits} do-not-call credits left"
        except DNCUnavailable as exc:
            ok, detail = (
                False,
                {"DNC_NO_CREDITS": "No credits left", "DNC_KEY_INVALID": "Key or secret refused"}.get(
                    exc.reason, str(exc)
                ),
            )
    elif row_id == "hubspot":
        from app.providers.hubspot import HubSpot

        client = HubSpot(conf["hubspot_service_key"], http=http)
        ok, detail = await client.check()
        if ok:
            from app.providers.hubspot import HubSpotError

            try:
                made = await client.ensure_properties()  # R9B.10: once, at setup.
                detail = "Connected" + (f"; created {len(made)} fields" if made else "")
            except HubSpotError as exc:
                ok, detail = False, f"Connected, but the app's fields could not be created ({exc})"
    elif row_id == "treg":
        from app.providers.treg import Treg

        ok, detail = await Treg(conf["treg_token"]).check()
    else:
        raise HTTPException(409, f"The {spec.label} check arrives with its integration")
    checks = {
        **(conf.get("_setup_checks") or {}),
        row_id: setup.check_record(spec, conf, ok, detail, service.clock().isoformat()),
    }
    service.configs.write(p["tenant_id"], {"_setup_checks": checks})
    return ops.setup_rows(service, p)


def preflight(service, p, *, full=True, deep=False):
    if not full:
        cached = service.capability_cache.get((p["tenant_id"], p["seller_id"]), {})
        return {
            "integrity_ok": None,
            "ready_to_call": None,
            "checks_scope": "operational",
            "blockers": [],
            "warnings": [
                "Run preflight for database and provider readiness; each call is authorized at submission."
            ],
            "mode": "local",
            "simulation": service.settings.simulation,
            "version": service.settings.version,
            **cached,
            "capabilities_stale": not cached
            or (service.clock() - dt(cached["capabilities_as_of"])).total_seconds() > 60,
        }
    conf = service.configs.read(p["tenant_id"])
    blockers, warnings = [], []
    integrity = ops.database_integrity(service, deep=deep)
    with service.db.transaction(False) as s:
        # A missing campaign or budget is a named blocker, not an IndexError.
        campaign = next(iter(ops.scoped(service, s, "campaigns", p)), None)
        policy = (
            ops.required(service, s, "policy_versions", campaign.data["policy_id"], p) if campaign else None
        )
        seller = ops.required(service, s, "sellers", p["seller_id"], p)
        operating = dict(seller.data.get("operating_policy", {}))
        source_version = f"{conf.get('version', 0)}:{seller.version}:{policy.version if policy else 0}"
        blockers.extend(
            b.code for b in ops.dial_blockers_in(service, s, p, rep=False, integrity_ok=integrity)
        )
        numbers = ops.scoped(service, s, "number_inventory", p)
        from app.compliance.operating import screening_capability, screening_snapshot

        screening = screening_capability(screening_snapshot(service, s, p))
        screening["enabled"] = bool(policy and policy.data.get("dnc_screening"))
        state_required = bool(policy and policy.data.get("state_registry_required", False))
        screening["state_registry_required"] = state_required
        if campaign:
            ops._preflight_queue(service, s, p, campaign, numbers, screening, blockers, warnings)
        if not ops.scoped(service, s, "budgets", p):
            blockers.append("BUDGET_NOT_CONFIGURED")
        pending = sum(
            c.data.get("status") in {"sending", "uncertain"}
            for c in ops.scoped(service, s, "command_outbox", p)
        )
        if pending:
            warnings.append("Uncertain commands require reconciliation")
    if service.settings.simulation:
        warnings.append("Simulated providers: no real calls, purchases, inference, or carrier billing")
    inference = bool(ops.inference(service, p["tenant_id"], "research_extract", p["user_id"]))
    if not inference:
        warnings.append("Research inference is unconfigured; approved fallback scripts remain available")
    provider = conf.get("coach_provider", PROVIDER_DEFAULTS.coach_provider)
    if provider == "openrouter" and not conf.get("openrouter_api_key"):
        warnings.append("OpenRouter/Cerebras is unconfigured; approved static coaching remains available")
    capabilities = capability_statuses(
        {**conf, "webhook_url": conf.get("webhook_url", service.settings.webhook_public_url)},
        operating,
        screening,
        simulation=service.settings.simulation,
        source_version=source_version,
        ingress=conf.get("ingress_evidence"),
        inference=inference,
    )
    if service.managed.enabled(p["tenant_id"]):
        connected = service.managed.status()["connected"]
        for capability in capabilities:
            if capability["id"] == "offline_callback" and (
                service.managed.pool and service.managed.pool.offline_fallback
            ):
                capability.update(
                    state="configured",
                    reason_codes=["HOSTED_CALLBACK_GREETING_CONFIGURED"],
                    remedy="Missed calls appear in Review. Test the greeting with the app closed.",
                )
            if capability["id"] == "local_ingress":
                capability.update(
                    state="software_verified" if connected else "degraded",
                    blocks="none" if connected else "voice",
                    reason_codes=[
                        "MANAGED_CONNECTION_READY" if connected else "MANAGED_CONNECTION_UNAVAILABLE"
                    ],
                    remedy="Keep OpenDialer open. Reconnect in Settings if disconnected.",
                )
    cache = {"capabilities": capabilities, "capabilities_as_of": service.clock().isoformat()}
    with service.ingress_lock:
        service.capability_cache[(p["tenant_id"], p["seller_id"])] = cache
        while len(service.capability_cache) > 128:
            service.capability_cache.pop(next(iter(service.capability_cache)))
    return {
        **cache,
        "capabilities_stale": False,
        "integrity_ok": integrity,
        "integrity_check": "full" if deep else "quick",
        "ready_to_call": not blockers,
        "blockers": sorted(set(blockers)),
        "warnings": warnings,
        "mode": "local",
        "simulation": service.settings.simulation,
        "version": service.settings.version,
        "screening": screening,
        "provider_validation": "simulated"
        if service.settings.simulation
        else "requires live account validation",
    }


def calling_readiness(service, p):
    """R9E.5: what pressing Start would do now. Run while idle, so a problem shows before Start, not after."""
    check = ops.preflight(service, p, full=True)
    screening = check.get("screening") or {}
    callable_now = screening.get("callable_when_ready_count", 0)
    reasons = set(screening.get("queue_reason_codes") or []) - SETUP_REASONS
    with service.db.transaction(False) as s:
        found = [
            b.detail()
            for b in ops.dial_blockers_in(service, s, p, rep=False, integrity_ok=check["integrity_ok"])
        ]
        detail = no_lead_detail(service, s, p, reasons) if not callable_now else {}
    return {
        "blockers": found,
        "callable_now": callable_now,
        "inspected": screening.get("inspected_count", 0),
        "why_none": detail.get("message"),
        "fix": detail.get("fix"),
        "next_action": "Start calling to wait for the queue, or return later."
        if detail.get("next_action")
        else None,
        "checked_at": service.clock().isoformat(),
    }


def database_integrity(service, *, deep=False):
    """Routine readiness runs SQLite's quick_check; the full page check is an explicit deep run."""
    if deep:
        return service.db.integrity()
    with service.db.transaction(False) as s:
        return s.execute(text("PRAGMA quick_check")).scalar() == "ok"


def _preflight_queue(service, s, p, campaign, numbers, screening, blockers, warnings):
    from app.lead_lists import TEST_LIST
    from app.optimization.numbering import select_number
    from app.scheduling.policy import queue_decision

    lead_model = MODELS["leads"]
    if run := test_list.test_list_run(service, s, p):
        p = {**p, "dial_lists": [test_list.NAME], "test_only": True, "test_run_id": run["id"]}
        leads = [lead for lead in test_list.contacts(s, p, campaign.id) if test_list.permits(run, p, lead)]
        total = len(leads)
    elif ops.in_test_session(service, p):
        p = {**p, "dial_lists": [TEST_LIST], "test_only": True}
        lead = ops.test_candidate(service, s, p, ops.test_phone(service, p), campaign_id=campaign.id)
        leads = [lead] if lead else []
        total = len(leads)
    else:
        scope = (
            lead_model.tenant_id == p["tenant_id"],
            lead_model.seller_id == p["seller_id"],
            lead_model.data["test_list_contact"].as_boolean().is_not(True),
        )
        total = s.scalar(select(func.count()).select_from(lead_model).where(*scope))
        leads = list(s.scalars(select(lead_model).where(*scope).order_by(lead_model.id).limit(500)))
    ops._queue_context(service, s, p, campaign.id, leads)
    inventory = ops.inventory_evidence(service, s, p, fast=True)
    claims = s.info["active_claims"]
    own = next((c for c in claims if c.rep_id == p["user_id"]), None)
    claimed = {c.endpoint_id for c in claims}
    bindings = {
        r.data["lead_id"]: r.data["number_id"] for r in ops.scoped(service, s, "lead_number_bindings", p)
    }
    by_number = {n.id: n for n in numbers}
    counts = {
        "cleared_count": 0,
        "pending_count": 0,
        "blocked_count": 0,
        "dialable_queue_count": 0,
        "callable_when_ready_count": 0,
    }
    reasons, queue_reasons = set(), set()
    for lead in leads:
        allocation = select_number(doc(lead), inventory, bound_id=bindings.get(lead.id))
        context = ops.context(service, s, p, lead, by_number.get(allocation["number_id"]))
        national = context.registry_decision
        key = (
            "cleared_count"
            if national["decision"] == "clear"
            else "blocked_count"
            if national["decision"] in {"blocked", "listed"}
            else "pending_count"
        )
        counts[key] += 1
        reasons.update(national["reason_codes"])
        decision = queue_decision(
            context,
            lead.data,
            own_claim=own,
            endpoint_claimed=lead.data["endpoint_id"] in claimed,
            now=service.clock(),
        )
        counts["dialable_queue_count"] += int(decision.allowed)
        # Would be dialable once the rep goes ready: their own presence is not a lead problem.
        counts["callable_when_ready_count"] += int(set(decision.reasons) <= SETUP_REASONS)
        queue_reasons.update(decision.reasons)
        if context.state_registry_required and not context.state_registry_current:
            reasons.add("STATE_REGISTRY_MISSING")
    screening.update(
        **counts,
        inspected_count=len(leads),
        total_count=total,
        omitted_count=max(0, total - len(leads)),
        counts_complete=total <= len(leads),
        reason_codes=sorted(reasons or screening["reason_codes"]),
        queue_reason_codes=sorted(queue_reasons),
        queue_scope="inspected candidates for the current rep; not reserved capacity",
    )
    if screening["enabled"] and not counts["cleared_count"]:
        blockers.extend(screening["reason_codes"] or ["SCREENING_PENDING"])
    if "STATE_REGISTRY_MISSING" in reasons:
        warnings.append("Some leads require a current authorized state registry source")
    if not total:
        blockers.append("NO_LEADS")
    elif not counts["callable_when_ready_count"]:
        # Rep presence, number setup and timing are reported once, not per lead.
        setup = SETUP_REASONS | (NUMBER_REASONS if any(b in NUMBER_BLOCKERS for b in blockers) else set())
        lead_reasons = queue_reasons - setup - TIMING_REASONS
        if not screening["enabled"]:
            lead_reasons -= REGISTRY_REASONS
        blockers.extend(sorted(lead_reasons))
        if queue_reasons & TIMING_REASONS and not lead_reasons:
            warnings.append(NO_LEAD_DUE_NOW)
        if total > len(leads):
            warnings.append("Queue inspection is bounded; further candidates may be eligible")


def number_setup_blocker(service, s, p, numbers):
    """The one next step for caller numbers, or None when any number can call now."""
    if (pool := service.managed.pool) is not None:
        if not pool.numbers:
            return "NO_CALLER_NUMBER"
        if not any(n.outbound_enabled for n in pool.numbers):
            return "CALLER_NUMBER_PAUSED"
        return (
            None
            if ops.workspace_test_passed(service, s, p)
            or ops.in_test_session(service, p)
            or test_list.test_list_run(service, s, p)
            else ("CALLER_NUMBER_CALLBACK_TEST_REQUIRED")
        )
    active = [n for n in numbers if n.data.get("lifecycle") != "retired_callback_only"]
    readiness = [(n, ops.number_readiness_in(service, s, p, n)) for n in active]
    if any(r["outbound_ready"] for _, r in readiness):
        return None
    if not active:
        return "NO_CALLER_NUMBER"
    if all(r["configuration_errors"] or not n.data.get("identity_configured") for n, r in readiness):
        return "CALLER_NUMBER_ROUTE_REQUIRED"
    if not ops.workspace_test_passed(service, s, p):
        return "CALLER_NUMBER_CALLBACK_TEST_REQUIRED"
    return "CALLER_NUMBER_PAUSED"


def no_lead_detail(service, s, p, reasons):
    """The refusal for Next call: the plain reason nobody can be called, most useful first."""
    reasons = set(reasons)
    found = [(text, fix) for codes, text, fix in WHY_NOBODY if codes & reasons]
    if reasons & NUMBER_REASONS:
        found.insert(
            0,
            (
                "No eligible caller number is available now. Check assigned numbers, active calls, and daily limits.",
                "numbers",
            ),
        )
    if reasons == {"OUTSIDE_CALLING_WINDOW"}:
        found = [("Everyone left is outside calling hours in their time zone.", None)]
    if not found:
        found = [("No lead is ready to call. Check lead preparation or sync a list in Leads.", "leads")]
    primary = next((reason for reason in found if reason[1]), found[0])
    messages = [primary[0], *(text for text, fix in found if (text, fix) != primary)]
    detail = {"code": "NO_ELIGIBLE_LEADS", "reasons": sorted(reasons), "message": " ".join(messages[:3])}
    if primary[1]:
        detail["fix"] = primary[1]
    elif reasons & TIMING_REASONS:
        detail["next_action"] = "Keep calling on to retry, or pause and return later."
    return detail


def _ingress_start(service, p, data):
    conf = service.configs.read(p["tenant_id"])
    endpoint = conf.get("webhook_url", service.settings.webhook_public_url)
    if not endpoint:
        fail(409, "INGRESS_NOT_CONFIGURED: save the public webhook endpoint first")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        fail(422, "Ingress requires a valid HTTPS host and port")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/webhooks/telnyx"
    ):
        fail(422, "Ingress requires an HTTPS host and exact /webhooks/telnyx path")
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    key = "ingress-check:" + data["request_id"]
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        ops._restore_operator(service, s, p)
        prior = ops._by_key(service, s, p, "outcomes", key)
        if prior:
            if prior.data["digest"] != digest:
                fail(409, "Ingress request identity has different input")
            if prior.data.get("receipt"):
                return {"receipt": prior.data["receipt"]}
            fail(409, "Ingress check is pending or failed; use a new explicit check after resolving it")
        if conf.get("version", 0) != data["expected_version"]:
            fail(409, "Provider configuration changed; reload before checking ingress")
        row = put(
            s,
            "outcomes",
            p["tenant_id"],
            p["seller_id"],
            {"kind": "ingress_check", "digest": digest, "status": "pending"},
            key=key,
            now=service.clock(),
        )
        identity = row.id
    nonce = secrets.token_hex(16)
    challenge = {
        "tenant_id": p["tenant_id"],
        "endpoint": endpoint,
        "version": conf.get("version", 0),
        "expires": service.clock() + timedelta(seconds=60),
        "monotonic_end": time.monotonic() + 60,
        "consumed": False,
        "nonce": nonce,
        "id": identity,
    }
    with service.ingress_lock:
        service.ingress_challenges = {
            k: v
            for k, v in service.ingress_challenges.items()
            if v["expires"] > service.clock() and v["monotonic_end"] > time.monotonic()
        }
        rejected = len(service.ingress_challenges) >= 128 or any(
            v["tenant_id"] == p["tenant_id"] for v in service.ingress_challenges.values()
        )
        if not rejected:
            service.ingress_challenges[nonce] = challenge
    if rejected:
        ops._ingress_failed(service, p, identity)
        fail(409, "Another ingress check is active; wait for its deadline")
    return challenge


def ingress_challenge(service, nonce):
    with service.ingress_lock:
        item = service.ingress_challenges.get(nonce)
        if (
            not item
            or item["consumed"]
            or item["expires"] <= service.clock()
            or item["monotonic_end"] <= time.monotonic()
        ):
            fail(404, "Not found")
        current = service.configs.read(item["tenant_id"])
        if (
            current.get("webhook_url", service.settings.webhook_public_url) != item["endpoint"]
            or current.get("version", 0) != item["version"]
        ):
            fail(404, "Not found")
        item["consumed"] = True
        return "opendialer-ingress:" + nonce


def _ingress_finish(service, p, item, response):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from app.providers.telnyx import verify_webhook

    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        ops._restore_operator(service, s, p)
        current = service.configs.read(p["tenant_id"])
        if (
            current.get("webhook_url", service.settings.webhook_public_url) != item["endpoint"]
            or current.get("version", 0) != item["version"]
        ):
            fail(409, "Ingress configuration changed during verification")
        if (
            not item["consumed"]
            or item["expires"] <= service.clock()
            or item["monotonic_end"] <= time.monotonic()
            or response.get("text") != "opendialer-ingress:" + item["nonce"]
        ):
            fail(502, "Ingress proof missing or expired; check the webhook-only proxy and tunnel")
        private = Ed25519PrivateKey.generate()
        stamp, body = str(int(service.clock().timestamp())), b"{}"
        public = base64.b64encode(private.public_key().public_bytes_raw()).decode()
        signature = base64.b64encode(private.sign(stamp.encode() + b"|" + body)).decode()
        assert verify_webhook(body, signature, stamp, public, now=service.clock())
        assert not verify_webhook(b"bad", signature, stamp, public, now=service.clock())
        model = MODELS["webhook_inbox"]
        recent = s.scalars(
            select(model)
            .where(model.tenant_id == p["tenant_id"])
            .order_by(model.created_at.desc(), model.id.desc())
            .limit(100)
        )
        real = next((r.data.get("received_at") for r in recent if r.data.get("signature_verified")), None)
        receipt = {
            "routing": "software_verified",
            "signature_fixture": "software_verified",
            "last_real_provider_event": real,
            "real_event_search_limit": 100,
            "at": service.clock().isoformat(),
            "source_version": item["version"],
            "endpoint_digest": hashlib.sha256(item["endpoint"].encode()).hexdigest(),
        }
        update(ops.required(service, s, "outcomes", item["id"], p), status="completed", receipt=receipt)
        service.configs.write(
            p["tenant_id"],
            {"ingress_evidence": receipt},
            expected={
                "version": current.get("version"),
                "webhook_url": current.get("webhook_url"),
            },
        )
        service.capability_cache.clear()
        return receipt


def _ingress_failed(service, p, identity):
    with service.db.transaction() as s:
        row = ops.required(service, s, "outcomes", identity, p)
        if row.data.get("status") == "pending":
            update(row, status="failed", reason="route_check_incomplete")


async def check_ingress(service, p, data):
    if service.managed.enabled(p["tenant_id"]):
        status = service.managed.status()
        return {
            **status,
            "routing": "software_verified" if status["connected"] else "unavailable",
            "carrier_call_verified": False,
        }
    from app.research.fetch import SafeFetcher

    item = await asyncio.to_thread(ops._ingress_start, service, p, data)
    if "receipt" in item:
        return item["receipt"]
    parsed = urlsplit(item["endpoint"])
    url = f"https://{parsed.netloc}/webhooks/ingress-check/{item['nonce']}"
    try:
        async with asyncio.timeout(10):
            response = await SafeFetcher(
                max_bytes=1024, max_redirects=0, allowed_domains=[parsed.hostname]
            ).fetch(url)
        return await asyncio.to_thread(ops._ingress_finish, service, p, item, response)
    except (TimeoutError, ValueError, OSError, httpx.HTTPError):
        fail(502, "Ingress is unreachable or unsafe; verify the saved hostname and webhook-only route")
    finally:
        with service.ingress_lock:
            service.ingress_challenges.pop(item["nonce"], None)
        await asyncio.to_thread(ops._ingress_failed, service, p, item["id"])


def publish(service, p, policy_id, body):
    now = service.clock()
    with service.db.transaction() as s:
        policy = ops.required(service, s, "policy_versions", policy_id, p)
        campaign = ops.required(service, s, "campaigns", policy.data["campaign_id"], p)
        current = ops.required(service, s, "policy_versions", campaign.data["policy_id"], p)
        config = {
            **current.data,
            **body,
            "published_at": now.isoformat(),
            "publisher_id": p["user_id"],
            "parent_id": current.id,
        }
        config["local_end_hour"] = min(17, config["local_end_hour"])
        if not 0 <= config["local_start_hour"] < config["local_end_hour"] <= 24:
            fail(422, "Invalid reviewed local calling window")
        new = put(s, "policy_versions", p["tenant_id"], p["seller_id"], config, now=now)
        update(campaign, policy_id=new.id)
        audit(
            s,
            p["tenant_id"],
            p["seller_id"],
            p["user_id"],
            "policy.published",
            new.id,
            now,
            {"previous_id": current.id, "reason": body.get("reason", "")},
        )
        emit(s, p["tenant_id"], "policy.published", new, now)
        return doc(new)


def edit_number(service, p, number_id, body):
    from pydantic import ValidationError

    from app.number_operations import ExternalReputation
    from app.optimization.numbering import unresolved_label

    for key, ceiling in (("daily_limit", 10000), ("capacity", 20)):
        if key in body and (type(body[key]) is not int or not 1 <= body[key] <= ceiling):
            fail(422, "Positive integer workload limits are required")
    if "identity_configured" in body and type(body["identity_configured"]) is not bool:
        fail(422, "Identity evidence flags must be booleans")
    if "external_reputation" in body:
        try:
            evidence = ExternalReputation.model_validate(body["external_reputation"])
        except ValidationError:
            fail(422, "Imported health requires source, ownership, nullable label and valid dates")
        if evidence.observed_at > service.clock():
            fail(422, "Imported health cannot be from the future")
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        ops._configuration_actor(service, s, p)
        number = ops.required(service, s, "number_inventory", number_id, p)
        emergency = body.get("lifecycle") == "paused_for_outbound" and set(body) <= {
            "lifecycle",
            "reason",
        }
        identity = (
            None
            if emergency
            else ops.local_edit_in(service, s, p, "number", number_id, number, body, "owner_id")
        )
        if identity and identity[2]:
            return {**doc(number), "receipt": identity[2]}
        if (
            "external_reputation" in body
            and not (evidence.spam_label or "").strip()
            and unresolved_label(number.data.get("external_reputation"))
        ):
            fail(422, "An open label requires an explicit clear observation")
        if body.get("identity_configured") is True and not number.data.get("identity_configured"):
            fail(409, "Configure the owned carrier route; an operator note cannot verify identity")
        next_state = body.get("lifecycle", number.data["lifecycle"])
        order = ["purchased", "identity_configured", "limited_real_use", "healthy"]
        if next_state in order:
            old = number.data["lifecycle"]
            if old in order and order.index(next_state) > order.index(old) + 1:
                fail(409, "Set up this number's route first")
            if next_state in order[1:] and not number.data.get("identity_configured"):
                fail(409, "Confirm the owned carrier connection before promotion")
            if next_state in order[2:] and (
                next_state != old or any(k in body for k in ("daily_limit", "capacity"))
            ):
                fail(409, "Use Resume to put this number back to work")
        elif next_state not in {
            "investigating",
            "paused_for_outbound",
            "remediated",
            "retired_callback_only",
        }:
            fail(422, "Invalid lifecycle")
        values = {
            k: v
            for k, v in body.items()
            if k not in {"expected_version", "request_id", "identity_configured"}
        }
        if "daily_limit" in values and values["daily_limit"] > number.data["daily_limit"]:
            fail(409, "A number's daily calls rise by themselves as it is used")
        update(number, **values, inbound_retained=True)
        audit(
            s,
            p["tenant_id"],
            p["seller_id"],
            p["user_id"],
            "number.updated",
            number.id,
            service.clock(),
            {"reason": body.get("reason", ""), "fields": sorted(values)},
        )
        emit(s, p["tenant_id"], "number.updated", number, service.clock())
        return {
            **doc(number),
            **({"receipt": ops.local_edit_receipt(service, s, p, identity, number)} if identity else {}),
        }


def backup(service, p):
    directory = service.settings.data_dir / "backups"
    directory.mkdir(exist_ok=True, mode=0o700)
    with service.db.transaction(False) as s:
        payload = {
            "tenant_id": p["tenant_id"],
            "seller_id": p["seller_id"],
            "at": service.clock().isoformat(),
            "schema": 2,
            "tables": {
                kind: [
                    {
                        "id": r.id,
                        "tenant_id": r.tenant_id,
                        "seller_id": r.seller_id,
                        "data": r.data,
                        "external_key": r.external_key,
                        "version": r.version,
                        "created_at": r.created_at.isoformat(),
                    }
                    for r in (
                        rows(s, kind, p["tenant_id"])
                        if kind == "tenants"
                        else ops.scoped(service, s, kind, p)
                    )
                ]
                for kind in MODELS
            },
        }
    backup_id = uid()
    blob = json.dumps(payload).encode()
    path = directory / (backup_id + ".backup")
    path.write_bytes(blob)
    copied = None
    if service.settings.backup_copy_dir:
        service.settings.backup_copy_dir.mkdir(parents=True, exist_ok=True)
        copied = str(service.settings.backup_copy_dir / path.name)
        (service.settings.backup_copy_dir / path.name).write_bytes(blob)
    return {
        "backup_id": backup_id,
        "checksum": hashlib.sha256(blob).hexdigest(),
        "encrypted": False,
        "copy": copied,
    }


def restore(service, p, backup_id):
    """Recover missing records into the current store; current facts and later edits win."""
    if not re.fullmatch(r"[0-9a-f-]{36}", backup_id):
        fail(422, "Invalid backup ID")
    path = service.settings.data_dir / "backups" / (backup_id + ".backup")
    if not path.exists():
        fail(404, "Backup not found")
    payload = json.loads(path.read_bytes())
    if (
        payload["tenant_id"] != p["tenant_id"]
        or payload.get("seller_id") != p["seller_id"]
        or payload.get("schema") != 2
    ):
        fail(403, "Backup identity or schema mismatch")
    for kind, values in payload.get("tables", {}).items():
        if kind in RETIRED_TABLES:
            continue  # An older version's table; nothing in this version reads it.
        if kind not in MODELS or any(
            raw.get("tenant_id") != p["tenant_id"]
            or raw.get("seller_id") != ("" if kind == "tenants" else p["seller_id"])
            for raw in values
        ):
            fail(422, "Backup contains records outside this seller's authority")
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        ops._restore_operator(service, s, p)
        recovered = 0
        for kind, model in MODELS.items():
            if kind in {"tenants", "users"}:
                continue  # Business state only, never old access grants, passwords or sessions.
            values = payload["tables"].get(kind, [])
            for offset in range(0, len(values), 250):
                batch = values[offset : offset + 250]
                keys = [r["external_key"] for r in batch if r.get("external_key")]
                current = list(
                    s.execute(
                        select(model.id, model.tenant_id, model.seller_id, model.external_key).where(
                            or_(
                                model.id.in_([r["id"] for r in batch]),
                                (model.tenant_id == p["tenant_id"])
                                & (model.seller_id == p["seller_id"])
                                & model.external_key.in_(keys),
                            )
                        )
                    )
                )
                by_id = {r.id: r for r in current}
                by_key = {r.external_key: r.id for r in current if r.external_key}
                for raw in batch:
                    existing = by_id.get(raw["id"])
                    if existing:
                        if (existing.tenant_id, existing.seller_id) != (p["tenant_id"], p["seller_id"]):
                            fail(409, "Backup record identity conflicts with another authority")
                        if kind == "suppressions" and raw["data"].get("active", True):
                            row = ops.required(service, s, kind, raw["id"], p)
                            if not row.data.get("active") or (
                                raw["data"].get("permanent") and not row.data.get("permanent")
                            ):
                                update(row, **raw["data"])
                        continue
                    if raw.get("external_key") in by_key:
                        fail(
                            409,
                            "Backup request identity conflicts with a current record; review before recovery",
                        )
                    s.add(model(**{**raw, "created_at": dt(raw.get("created_at") or payload["at"])}))
                    recovered += 1
                s.flush()
        for command in ops.scoped(service, s, "command_outbox", p):
            if command.data.get("status") in {"pending", "sending"}:
                update(command, status="uncertain", restore_hold=True)
        update(ops.required(service, s, "sellers", p["seller_id"], p), restore_hold=True)
        for user in ops.scoped(service, s, "users", p):
            update(user, presence={"status": "paused", "audio_ready": False, "pending_optouts": 0})
        audit(
            s,
            p["tenant_id"],
            p["seller_id"],
            p["user_id"],
            "backup.restored",
            backup_id,
            service.clock(),
            {"recovered_records": recovered, "mode": "recover_missing_keep_current"},
        )
    service.raw.clear()
    return {
        "restored": True,
        "mode": "recover_missing_keep_current",
        "recovered_records": recovered,
        "outbound_paused": True,
    }


def _restore_operator(service, session, p):
    ops.required(service, session, "users", p["user_id"], p)


async def reconcile_restore(service, p, review_reference, provider_ids=None):
    """Resume outbound only once every restored live call is proven ended at the carrier."""
    if not review_reference:
        fail(422, "A reviewed carrier reconciliation reference is required")
    provider_ids = provider_ids or {}

    def open_calls():
        with service.db.transaction(False) as s:
            ops._restore_operator(service, s, p)
            model = MODELS["call_attempts"]
            return [
                (call.id, call.version, call.data.get("provider_id") or provider_ids.get(call.id))
                for call in s.scalars(
                    select(model).where(
                        model.tenant_id == p["tenant_id"],
                        model.seller_id == p["seller_id"],
                        model.op_status.not_in(TERMINAL),
                    )
                )
            ]

    def finish(call_id, version, receipt, target):
        verified = call_receipt(receipt, target, simulation=service.settings.simulation)
        if not verified or verified[1]:
            return False
        with service.db.transaction() as s:
            service.db.authorize_stream(s, p)
            call = ops.required(service, s, "call_attempts", call_id, p)
            if call.data["state"] in TERMINAL:
                return True
            if call.version != version:
                return False
            if not call.data.get("provider_id"):
                update(call, provider_id=verified[0])
            ops.finish_call(service, s, p, call, "restored_provider_terminal_receipt")
        return True

    calls, unresolved = await asyncio.to_thread(open_calls), []
    for call_id, version, target in calls:
        try:
            provider = await asyncio.to_thread(ops.carrier, service, p["tenant_id"])
            receipt = await provider.reconcile(call_id, target) if target else None
        except Exception as exc:
            logging.getLogger(__name__).warning("reconcile_restore handled a failure: %s", type(exc).__name__)
            receipt = None
        if not await asyncio.to_thread(finish, call_id, version, receipt, target):
            unresolved.append(call_id)
    if unresolved:
        fail(409, {"code": "AUTHORIZATION_RECONCILIATION_PENDING", "call_ids": unresolved})

    def release():
        with service.db.transaction() as s:
            service.db.authorize_stream(s, p)
            ops._restore_operator(service, s, p)
            for kind, condition in (
                (
                    "call_legs",
                    MODELS["call_legs"].op_status.not_in({"ended", "cancelled", "failed", *TERMINAL}),
                ),
                ("followup_tasks", MODELS["followup_tasks"].op_active.is_(True)),
            ):
                model = MODELS[kind]
                if s.scalar(
                    select(model.id)
                    .where(model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"], condition)
                    .limit(1)
                ):
                    fail(
                        409,
                        "An agent or inbound leg remains; verify or stop it in Recovery before resuming",
                    )
            seller = ops.required(service, s, "sellers", p["seller_id"], p)
            update(seller, restore_hold=False, restore_review_reference=review_reference)
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "restore.reconciled",
                seller.id,
                service.clock(),
                {"calls": len(calls), "reference": review_reference},
            )
        return {"reconciled": True, "calls": len(calls), "outbound_remains_paused_until_rep_ready": True}

    return await asyncio.to_thread(release)
