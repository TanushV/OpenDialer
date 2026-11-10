import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, or_, select, text

from app.calling import audio_leg
from app.calling.blockers import Facts, mark_stuck
from app.calling.blockers import blockers as dial_gate
from app.calling.state import TERMINAL
from app.intelligence import Application
from app.number_operations import NumberOperations
from app.optimization.script_lab import ScriptLab
from app.optimization.workflows import OptimizationWorkflows
from app.platform.config import PROVIDER_DEFAULTS, CapabilityStatus
from app.platform.db import MODELS, RETIRED_TABLES, audit, doc, emit, put, rows, uid, update
from app.rep_workflows import RepWorkflows
from app.service import dt, fail
from app.telephony.callbacks import InboundCallbacks
from app.telephony.ingress import WebhookIngress
from app.telephony.recovery import CommandRecovery, call_receipt
from app.telephony.recovery_actions import RecoveryActions
from app.telephony.workflows import TelephonyWorkflows
from app.workspace import WorkspaceReads

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
        {"OUTSIDE_CALLING_WINDOW"},
        "Everyone left is outside calling hours (8 AM to 5 PM in each lead's time zone). "
        "Calling picks up by itself when their hours open.",
    ),
    (
        {"TIMEZONE_UNCERTAIN"},
        "Some leads have no known time zone, so they are not called. Add a state or time zone in Leads.",
    ),
    (
        {"NOT_DUE", "CADENCE_LIMIT"},
        "The rest were called recently and wait at least a day before the next try.",
    ),
    (
        {"COMPANY_CONTACTED_TODAY"},
        "Some companies were already called today; their other contacts wait until tomorrow.",
    ),
    ({"ATTEMPT_LIMIT"}, "Some leads reached their most attempts and are not called again."),
    ({"OUTREACH_HOLD"}, "Some leads are paused, removed, on a paused list, or already booked."),
    (
        {"NUMBER_CONFLICT", "NUMBER_BAD", "NUMBER_DEAD", "NO_VALID_NUMBER", "NEEDS_NUMBER"},
        "Some leads have no usable number. Review shows them.",
    ),
    ({"DNC_NOT_CHECKED"}, "Some HubSpot leads wait for the do-not-call check."),
    ({"CRM_SYNC_STALE"}, "Some HubSpot leads wait for their list to sync. Sync it in Leads."),
    (
        {"REGISTRY_NOT_CLEARED_FOR_ENDPOINT", "SCREENING_PENDING"},
        "Some leads wait for do-not-call screening.",
    ),
    (
        {"INTERNAL_SUPPRESSION", "WRONG_PARTY", "INVALID_ENDPOINT"},
        "Some leads opted out or have a wrong number.",
    ),
]


def why_nobody(reasons):
    reasons = set(reasons)
    found = [text for codes, text in WHY_NOBODY if codes & reasons]
    if not found:
        return (
            "No lead is ready to call. Leads still being researched show on the Leads page; import a list or "
            "sync a HubSpot list to add more."
        )
    return " ".join(found[:3])


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


class Dialer(
    WorkspaceReads,
    RecoveryActions,
    CommandRecovery,
    InboundCallbacks,
    RepWorkflows,
    NumberOperations,
    WebhookIngress,
    ScriptLab,
    OptimizationWorkflows,
    TelephonyWorkflows,
    Application,
):
    def dial_blockers_in(self, s, p, *, rep, integrity_ok=True):
        """Everything that stops calling now (`calling/blockers.py`). `rep` adds the rep's own audio."""
        conf = self.configs.read(p["tenant_id"])
        seller = self.required(s, "sellers", p["seller_id"], p)
        presence = self.required(s, "users", p["user_id"], p).data.get("presence", {}) if rep else {}
        numbers = self.scoped(s, "number_inventory", p)
        audio = None
        if rep:
            agent = presence.get("agent_call_control_id")
            audio = bool(agent and presence.get("audio_ready") and presence.get("telephony_ready"))
            if audio and not self.settings.simulation:
                leg = audio_leg.find(s, p, provider_id=agent, rep_id=p["user_id"])
                audio = bool(leg and leg.data.get("state") == "answered")
        return self._mark_stuck(
            dial_gate(
                Facts(
                    carrier_setup_required=not self.settings.simulation,
                    integrity_ok=integrity_ok,
                    restore_hold=bool(seller.data.get("restore_hold")),
                    telnyx_configured=bool(conf.get("telnyx_api_key") and conf.get("telnyx_connection_id")),
                    signature_key=bool(conf.get("telnyx_public_key")),
                    audio_setup=bool(conf.get("sip_username") and conf.get("telephony_credential_id")),
                    connection=self.managed.connection_code(p["tenant_id"]),
                    campaign=bool(next(iter(self.scoped(s, "campaigns", p)), None)),
                    number=self.number_setup_blocker(s, p, numbers),
                    test_number=next(
                        (n.data["phone"] for n in numbers if n.data.get("identity_configured")), None
                    ),
                    pending_optouts=presence.get("pending_optouts", 0),
                    rep_audio_ready=audio,
                    channel_limited=(conf.get("channel_limited_until") or "") > self.clock().isoformat(),
                )
            )
        )

    @property
    def watchdog(self):
        from app.calling.watchdog import Watchdog

        if "_watchdog" not in self.__dict__:
            self.__dict__["_watchdog"] = Watchdog()
        return self.__dict__["_watchdog"]

    def _mark_stuck(self, found):
        return mark_stuck(found, self.watchdog.stuck)

    async def watch_calling(self, p):
        """R9E.6: repair a self-clearing blocker that outlived its promise, and log it."""
        import logging

        with self.db.transaction(False) as s:
            codes = [b.code for b in self.dial_blockers_in(s, p, rep=False)]
        for code in self.watchdog.observe(codes, time.monotonic()):
            logging.getLogger(__name__).warning("Watchdog: %s outlived its promise; repairing it", code)
            await self.repair_blocker(p, code)

    async def repair_blocker(self, p, code):
        managed = self.managed
        if code == "CONNECTION_SETUP_CHANGING":
            if not managed.edit_lock.locked():
                managed.configuring = False
        elif code.startswith("CONNECTION_") and managed.enabled(p["tenant_id"]):
            await managed.close()
            await managed.start()
        elif code == "TELNYX_CALL_LIMIT_REACHED":
            self.configs.write(p["tenant_id"], {"channel_limited_until": None})

    def setup_rows(self, p):
        """Settings: one true status and one row per thing to set (section 7, R7.2)."""
        from app.calling.blockers import CATALOG
        from app.calling.setup import rows
        from app.compliance.phone import normalize_phone
        from app.providers.codex import profile_home

        conf = self.configs.read(p["tenant_id"])
        live = {}
        if self.settings.simulation:
            live["connection"] = (True, "Simulated workspace: call events are simulated")
        elif not self.managed.enabled(p["tenant_id"]):
            live["connection"] = (False, "Import your calling setup file to connect call events")
        else:
            code = self.managed.connection_code(p["tenant_id"])
            live["connection"] = (code is None, "Connected" if code is None else CATALOG[code][0])
        home = profile_home(self.settings.data_dir, p["user_id"])
        signed_in = (home / "auth.json").is_file() and (home / "account.json").is_file()
        live["chatgpt"] = (signed_in, "Signed in on this Mac" if signed_in else "Not signed in")
        phone = normalize_phone(conf.get("test_phone") or "")
        live["test_phone"] = (bool(phone.e164), phone.e164 or "Add your own number for test calls")
        with self.db.transaction(False) as s:
            numbers = self.scoped(s, "number_inventory", p)
            code = self.number_setup_blocker(s, p, numbers)
            ready = sum(1 for n in numbers if n.data.get("lifecycle") in {"healthy", "limited_real_use"})
            live["numbers"] = (code is None, f"{ready} ready" if code is None else CATALOG[code][0])
            campaign = next(iter(self.scoped(s, "campaigns", p)), None)
            script = (campaign.data.get("script") or {}) if campaign else {}
            live["script"] = (
                bool(script.get("template")),
                f"Version {script.get('version')} saved" if script.get("template") else "No script saved yet",
            )
            policy = (
                self.required(s, "policy_versions", campaign.data["policy_id"], p).data if campaign else {}
            )
            start, end = policy.get("local_start_hour", 9), policy.get("local_end_hour", 17)
            valid = isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= 24
            live["hours"] = (
                valid,
                f"{start}:00 to {end}:00 in each lead's time" if valid else "Fix the hours",
            )
            first = next(iter(self.dial_blockers_in(s, p, rep=False)), None)
        return {
            "status": first.detail() if first else None,
            "rows": rows(conf, conf.get("_setup_checks") or {}, live),
        }

    async def check_setup_row(self, p, row_id):
        """Run the real check behind one row and store its result with the value it checked (R7.3)."""
        from fastapi import HTTPException

        from app.calling import setup
        from app.providers import openrouter, telnyx

        spec = setup.BY_ID.get(row_id)
        if not spec:
            raise HTTPException(404, "Unknown setting")
        conf = self.configs.read(p["tenant_id"])
        if row_id in setup.LIVE:
            return self.setup_rows(p)
        if not all(conf.get(field) for field in spec.fields):
            raise HTTPException(409, f"Add the {spec.label} first")
        http = getattr(self, "http", None)
        if self.settings.simulation:
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
            row_id: setup.check_record(spec, conf, ok, detail, self.clock().isoformat()),
        }
        self.configs.write(p["tenant_id"], {"_setup_checks": checks})
        return self.setup_rows(p)

    def preflight(self, p, *, full=True, deep=False):
        if not full:
            cached = self.capability_cache.get((p["tenant_id"], p["seller_id"]), {})
            return {
                "integrity_ok": None,
                "ready_to_call": None,
                "checks_scope": "operational",
                "blockers": [],
                "warnings": [
                    "Run preflight for database and provider readiness; each call is authorized at submission."
                ],
                "mode": "local",
                "simulation": self.settings.simulation,
                "version": self.settings.version,
                **cached,
                "capabilities_stale": not cached
                or (self.clock() - dt(cached["capabilities_as_of"])).total_seconds() > 60,
            }
        conf = self.configs.read(p["tenant_id"])
        blockers, warnings = [], []
        integrity = self.database_integrity(deep=deep)
        with self.db.transaction(False) as s:
            # A missing campaign or budget is a named blocker, not an IndexError.
            campaign = next(iter(self.scoped(s, "campaigns", p)), None)
            policy = self.required(s, "policy_versions", campaign.data["policy_id"], p) if campaign else None
            seller = self.required(s, "sellers", p["seller_id"], p)
            operating = dict(seller.data.get("operating_policy", {}))
            source_version = f"{conf.get('version', 0)}:{seller.version}:{policy.version if policy else 0}"
            blockers.extend(b.code for b in self.dial_blockers_in(s, p, rep=False, integrity_ok=integrity))
            numbers = self.scoped(s, "number_inventory", p)
            from app.compliance.operating import screening_capability, screening_snapshot

            screening = screening_capability(screening_snapshot(self, s, p))
            screening["enabled"] = bool(policy and policy.data.get("dnc_screening"))
            state_required = bool(policy and policy.data.get("state_registry_required", False))
            screening["state_registry_required"] = state_required
            if campaign:
                self._preflight_queue(s, p, campaign, numbers, screening, blockers, warnings)
            if not self.scoped(s, "budgets", p):
                blockers.append("BUDGET_NOT_CONFIGURED")
            pending = sum(
                c.data.get("status") in {"sending", "uncertain"} for c in self.scoped(s, "command_outbox", p)
            )
            if pending:
                warnings.append("Uncertain commands require reconciliation")
        if self.settings.simulation:
            warnings.append("Simulated providers: no real calls, purchases, inference, or carrier billing")
        inference = bool(self.inference(p["tenant_id"], "research_extract", p["user_id"]))
        if not inference:
            warnings.append("Research inference is unconfigured; approved fallback scripts remain available")
        provider = conf.get("coach_provider", PROVIDER_DEFAULTS.coach_provider)
        if provider == "openrouter" and not conf.get("openrouter_api_key"):
            warnings.append("OpenRouter/Cerebras is unconfigured; approved static coaching remains available")
        capabilities = capability_statuses(
            {**conf, "webhook_url": conf.get("webhook_url", self.settings.webhook_public_url)},
            operating,
            screening,
            simulation=self.settings.simulation,
            source_version=source_version,
            ingress=conf.get("ingress_evidence"),
            inference=inference,
        )
        if self.managed.enabled(p["tenant_id"]):
            connected = self.managed.status()["connected"]
            for capability in capabilities:
                if capability["id"] == "local_ingress":
                    capability.update(
                        state="software_verified" if connected else "degraded",
                        blocks="none" if connected else "voice",
                        reason_codes=[
                            "MANAGED_CONNECTION_READY" if connected else "MANAGED_CONNECTION_UNAVAILABLE"
                        ],
                        remedy="Keep OpenDialer open. Reconnect in Settings if disconnected.",
                    )
        cache = {"capabilities": capabilities, "capabilities_as_of": self.clock().isoformat()}
        with self.ingress_lock:
            self.capability_cache[(p["tenant_id"], p["seller_id"])] = cache
            while len(self.capability_cache) > 128:
                self.capability_cache.pop(next(iter(self.capability_cache)))
        return {
            **cache,
            "capabilities_stale": False,
            "integrity_ok": integrity,
            "integrity_check": "full" if deep else "quick",
            "ready_to_call": not blockers,
            "blockers": sorted(set(blockers)),
            "warnings": warnings,
            "mode": "local",
            "simulation": self.settings.simulation,
            "version": self.settings.version,
            "screening": screening,
            "provider_validation": "simulated"
            if self.settings.simulation
            else "requires live account validation",
        }

    def calling_readiness(self, p):
        """R9E.5: what pressing Start would do now. Run while idle, so a problem shows before Start, not after."""
        check = self.preflight(p, full=True)
        with self.db.transaction(False) as s:
            found = [
                b.detail() for b in self.dial_blockers_in(s, p, rep=False, integrity_ok=check["integrity_ok"])
            ]
        screening = check.get("screening") or {}
        callable_now = screening.get("callable_when_ready_count", 0)
        reasons = set(screening.get("queue_reason_codes") or []) - SETUP_REASONS
        return {
            "blockers": found,
            "callable_now": callable_now,
            "inspected": screening.get("inspected_count", 0),
            "why_none": None if callable_now else why_nobody(reasons),
            "checked_at": self.clock().isoformat(),
        }

    def database_integrity(self, *, deep=False):
        """Routine readiness runs SQLite's quick_check; the full page check is an explicit deep run."""
        if deep:
            return self.db.integrity()
        with self.db.transaction(False) as s:
            return s.execute(text("PRAGMA quick_check")).scalar() == "ok"

    def _preflight_queue(self, s, p, campaign, numbers, screening, blockers, warnings):
        from app.optimization.numbering import select_number
        from app.scheduling.policy import queue_decision

        lead_model = MODELS["leads"]
        scope = (lead_model.tenant_id == p["tenant_id"], lead_model.seller_id == p["seller_id"])
        total = s.scalar(select(func.count()).select_from(lead_model).where(*scope))
        leads = list(s.scalars(select(lead_model).where(*scope).order_by(lead_model.id).limit(500)))
        self._queue_context(s, p, campaign.id, leads)
        inventory = self.inventory_evidence(s, p, fast=True)
        claims = s.info["active_claims"]
        own = next((c for c in claims if c.rep_id == p["user_id"]), None)
        claimed = {c.endpoint_id for c in claims}
        bindings = {r.data["lead_id"]: r.data["number_id"] for r in self.scoped(s, "lead_number_bindings", p)}
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
            context = self.context(s, p, lead, by_number.get(allocation["number_id"]))
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
                context, lead.data, own_claim=own, endpoint_claimed=lead.data["endpoint_id"] in claimed
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

    def number_setup_blocker(self, s, p, numbers):
        """The one next step for caller numbers, or None when any number can call now."""
        active = [n for n in numbers if n.data.get("lifecycle") != "retired_callback_only"]
        readiness = [(n, self.number_readiness_in(s, p, n)) for n in active]
        if any(r["outbound_ready"] for _, r in readiness):
            return None
        if not active:
            return "NO_CALLER_NUMBER"
        if all(r["configuration_errors"] or not n.data.get("identity_configured") for n, r in readiness):
            return "CALLER_NUMBER_ROUTE_REQUIRED"
        if not self.workspace_test_passed(s, p):
            return "CALLER_NUMBER_CALLBACK_TEST_REQUIRED"
        return "CALLER_NUMBER_PAUSED"

    def no_lead_detail(self, s, p, reasons):
        """The refusal for Next call: the plain reason nobody can be called, most useful first."""
        detail = {"code": "NO_ELIGIBLE_LEADS", "reasons": sorted(reasons), "message": why_nobody(reasons)}
        if not set(reasons) & NUMBER_REASONS:
            return detail
        # Number setup itself is a blocker (`dial_blockers_in`) checked before the queue runs; here a
        # number exists but none is free for these leads now.
        detail["message"] = (
            "No eligible caller number is available now. Check assigned numbers, active calls, "
            "and daily limits in Numbers."
        )
        if set(reasons) - NUMBER_REASONS:
            detail["message"] += " Other leads may be outside calling hours or already called today."
        return detail

    def _ingress_start(self, p, data):
        conf = self.configs.read(p["tenant_id"])
        endpoint = conf.get("webhook_url", self.settings.webhook_public_url)
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
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            self._restore_operator(s, p)
            prior = self._by_key(s, p, "outcomes", key)
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
                now=self.clock(),
            )
            identity = row.id
        nonce = secrets.token_hex(16)
        challenge = {
            "tenant_id": p["tenant_id"],
            "endpoint": endpoint,
            "version": conf.get("version", 0),
            "expires": self.clock() + timedelta(seconds=60),
            "monotonic_end": time.monotonic() + 60,
            "consumed": False,
            "nonce": nonce,
            "id": identity,
        }
        with self.ingress_lock:
            self.ingress_challenges = {
                k: v
                for k, v in self.ingress_challenges.items()
                if v["expires"] > self.clock() and v["monotonic_end"] > time.monotonic()
            }
            rejected = len(self.ingress_challenges) >= 128 or any(
                v["tenant_id"] == p["tenant_id"] for v in self.ingress_challenges.values()
            )
            if not rejected:
                self.ingress_challenges[nonce] = challenge
        if rejected:
            self._ingress_failed(p, identity)
            fail(409, "Another ingress check is active; wait for its deadline")
        return challenge

    def ingress_challenge(self, nonce):
        with self.ingress_lock:
            item = self.ingress_challenges.get(nonce)
            if (
                not item
                or item["consumed"]
                or item["expires"] <= self.clock()
                or item["monotonic_end"] <= time.monotonic()
            ):
                fail(404, "Not found")
            current = self.configs.read(item["tenant_id"])
            if (
                current.get("webhook_url", self.settings.webhook_public_url) != item["endpoint"]
                or current.get("version", 0) != item["version"]
            ):
                fail(404, "Not found")
            item["consumed"] = True
            return "opendialer-ingress:" + nonce

    def _ingress_finish(self, p, item, response):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from app.providers.telnyx import verify_webhook

        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            self._restore_operator(s, p)
            current = self.configs.read(p["tenant_id"])
            if (
                current.get("webhook_url", self.settings.webhook_public_url) != item["endpoint"]
                or current.get("version", 0) != item["version"]
            ):
                fail(409, "Ingress configuration changed during verification")
            if (
                not item["consumed"]
                or item["expires"] <= self.clock()
                or item["monotonic_end"] <= time.monotonic()
                or response.get("text") != "opendialer-ingress:" + item["nonce"]
            ):
                fail(502, "Ingress proof missing or expired; check the webhook-only proxy and tunnel")
            private = Ed25519PrivateKey.generate()
            stamp, body = str(int(self.clock().timestamp())), b"{}"
            public = base64.b64encode(private.public_key().public_bytes_raw()).decode()
            signature = base64.b64encode(private.sign(stamp.encode() + b"|" + body)).decode()
            assert verify_webhook(body, signature, stamp, public, now=self.clock())
            assert not verify_webhook(b"bad", signature, stamp, public, now=self.clock())
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
                "at": self.clock().isoformat(),
                "source_version": item["version"],
                "endpoint_digest": hashlib.sha256(item["endpoint"].encode()).hexdigest(),
            }
            update(self.required(s, "outcomes", item["id"], p), status="completed", receipt=receipt)
            self.configs.write(
                p["tenant_id"],
                {"ingress_evidence": receipt},
                expected={
                    "version": current.get("version"),
                    "webhook_url": current.get("webhook_url"),
                },
            )
            self.capability_cache.clear()
            return receipt

    def _ingress_failed(self, p, identity):
        with self.db.transaction() as s:
            row = self.required(s, "outcomes", identity, p)
            if row.data.get("status") == "pending":
                update(row, status="failed", reason="route_check_incomplete")

    async def check_ingress(self, p, data):
        if self.managed.enabled(p["tenant_id"]):
            status = self.managed.status()
            return {
                **status,
                "routing": "software_verified" if status["connected"] else "unavailable",
                "carrier_call_verified": False,
            }
        from app.research.fetch import SafeFetcher

        item = await asyncio.to_thread(self._ingress_start, p, data)
        if "receipt" in item:
            return item["receipt"]
        parsed = urlsplit(item["endpoint"])
        url = f"https://{parsed.netloc}/webhooks/ingress-check/{item['nonce']}"
        try:
            async with asyncio.timeout(10):
                response = await SafeFetcher(
                    max_bytes=1024, max_redirects=0, allowed_domains=[parsed.hostname]
                ).fetch(url)
            return await asyncio.to_thread(self._ingress_finish, p, item, response)
        except (TimeoutError, ValueError, OSError, httpx.HTTPError):
            fail(502, "Ingress is unreachable or unsafe; verify the saved hostname and webhook-only route")
        finally:
            with self.ingress_lock:
                self.ingress_challenges.pop(item["nonce"], None)
            await asyncio.to_thread(self._ingress_failed, p, item["id"])

    def publish(self, p, policy_id, body):
        now = self.clock()
        with self.db.transaction() as s:
            policy = self.required(s, "policy_versions", policy_id, p)
            campaign = self.required(s, "campaigns", policy.data["campaign_id"], p)
            current = self.required(s, "policy_versions", campaign.data["policy_id"], p)
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

    def edit_number(self, p, number_id, body):
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
            if evidence.observed_at > self.clock():
                fail(422, "Imported health cannot be from the future")
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            self._configuration_actor(s, p)
            number = self.required(s, "number_inventory", number_id, p)
            emergency = body.get("lifecycle") == "paused_for_outbound" and set(body) <= {
                "lifecycle",
                "reason",
            }
            identity = (
                None if emergency else self.local_edit_in(s, p, "number", number_id, number, body, "owner_id")
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
                self.clock(),
                {"reason": body.get("reason", ""), "fields": sorted(values)},
            )
            emit(s, p["tenant_id"], "number.updated", number, self.clock())
            return {
                **doc(number),
                **({"receipt": self.local_edit_receipt(s, p, identity, number)} if identity else {}),
            }

    def backup(self, p):
        directory = self.settings.data_dir / "backups"
        directory.mkdir(exist_ok=True, mode=0o700)
        with self.db.transaction(False) as s:
            payload = {
                "tenant_id": p["tenant_id"],
                "seller_id": p["seller_id"],
                "at": self.clock().isoformat(),
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
                            rows(s, kind, p["tenant_id"]) if kind == "tenants" else self.scoped(s, kind, p)
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
        if self.settings.backup_copy_dir:
            self.settings.backup_copy_dir.mkdir(parents=True, exist_ok=True)
            copied = str(self.settings.backup_copy_dir / path.name)
            (self.settings.backup_copy_dir / path.name).write_bytes(blob)
        return {
            "backup_id": backup_id,
            "checksum": hashlib.sha256(blob).hexdigest(),
            "encrypted": False,
            "copy": copied,
        }

    def restore(self, p, backup_id):
        """Recover missing records into the current store; current facts and later edits win."""
        if not re.fullmatch(r"[0-9a-f-]{36}", backup_id):
            fail(422, "Invalid backup ID")
        path = self.settings.data_dir / "backups" / (backup_id + ".backup")
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
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            self._restore_operator(s, p)
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
                                row = self.required(s, kind, raw["id"], p)
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
            for command in self.scoped(s, "command_outbox", p):
                if command.data.get("status") in {"pending", "sending"}:
                    update(command, status="uncertain", restore_hold=True)
            update(self.required(s, "sellers", p["seller_id"], p), restore_hold=True)
            for user in self.scoped(s, "users", p):
                update(user, presence={"status": "paused", "audio_ready": False, "pending_optouts": 0})
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "backup.restored",
                backup_id,
                self.clock(),
                {"recovered_records": recovered, "mode": "recover_missing_keep_current"},
            )
        self.raw.clear()
        return {
            "restored": True,
            "mode": "recover_missing_keep_current",
            "recovered_records": recovered,
            "outbound_paused": True,
        }

    def _restore_operator(self, session, p):
        self.required(session, "users", p["user_id"], p)

    async def reconcile_restore(self, p, review_reference, provider_ids=None):
        """Resume outbound only once every restored live call is proven ended at the carrier."""
        if not review_reference:
            fail(422, "A reviewed carrier reconciliation reference is required")
        provider_ids = provider_ids or {}

        def open_calls():
            with self.db.transaction(False) as s:
                self._restore_operator(s, p)
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
            verified = call_receipt(receipt, target, simulation=self.settings.simulation)
            if not verified or verified[1]:
                return False
            with self.db.transaction() as s:
                self.db.authorize_stream(s, p)
                call = self.required(s, "call_attempts", call_id, p)
                if call.data["state"] in TERMINAL:
                    return True
                if call.version != version:
                    return False
                if not call.data.get("provider_id"):
                    update(call, provider_id=verified[0])
                self.finish_call(s, p, call, "restored_provider_terminal_receipt")
            return True

        calls, unresolved = await asyncio.to_thread(open_calls), []
        for call_id, version, target in calls:
            try:
                provider = await asyncio.to_thread(self.carrier, p["tenant_id"])
                receipt = await provider.reconcile(call_id, target) if target else None
            except Exception:
                receipt = None
            if not await asyncio.to_thread(finish, call_id, version, receipt, target):
                unresolved.append(call_id)
        if unresolved:
            fail(409, {"code": "AUTHORIZATION_RECONCILIATION_PENDING", "call_ids": unresolved})

        def release():
            with self.db.transaction() as s:
                self.db.authorize_stream(s, p)
                self._restore_operator(s, p)
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
                        .where(
                            model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"], condition
                        )
                        .limit(1)
                    ):
                        fail(
                            409,
                            "An agent or inbound leg remains; verify or stop it in Recovery before resuming",
                        )
                seller = self.required(s, "sellers", p["seller_id"], p)
                update(seller, restore_hold=False, restore_review_reference=review_reference)
                audit(
                    s,
                    p["tenant_id"],
                    p["seller_id"],
                    p["user_id"],
                    "restore.reconciled",
                    seller.id,
                    self.clock(),
                    {"calls": len(calls), "reference": review_reference},
                )
            return {"reconciled": True, "calls": len(calls), "outbound_remains_paused_until_rep_ready": True}

        return await asyncio.to_thread(release)
