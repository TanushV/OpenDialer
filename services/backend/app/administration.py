import asyncio
import base64
import hashlib
import json
import os
import re

from sqlalchemy import delete, func, or_, select

from app.db import DERIVED_TABLES, MODELS, audit, doc, emit, get, put, rows, uid, update
from app.intelligence import Application
from app.measurement import Measurement
from app.number_operations import NumberOperations
from app.optimization.workflows import OptimizationWorkflows
from app.rep_workflows import RepWorkflows
from app.reporting import ReportProjections
from app.service import dt, fail
from app.telephony.callbacks import InboundCallbacks
from app.telephony.ingress import WebhookIngress
from app.telephony.recovery import CommandRecovery, call_receipt
from app.telephony.recovery_actions import RecoveryActions
from app.telephony.reducer import TERMINAL
from app.telephony.workflows import TelephonyWorkflows
from app.workspace import WorkspaceReads


class Dialer(
    WorkspaceReads,
    ReportProjections,
    RecoveryActions,
    CommandRecovery,
    InboundCallbacks,
    Measurement,
    RepWorkflows,
    NumberOperations,
    WebhookIngress,
    OptimizationWorkflows,
    TelephonyWorkflows,
    Application,
):
    def metrics(self, p):
        from app.optimization.projections import read_metrics

        return read_metrics(self, p)

    def preflight(self, p, *, full=True):
        if not full:
            return {
                "integrity_ok": None,
                "ready_to_call": None,
                "checks_scope": "operational",
                "blockers": [],
                "warnings": [
                    "Run preflight for database and provider readiness; each call is authorized at submission."
                ],
                "mode": self.settings.mode,
                "simulation": self.settings.simulation,
                "version": self.settings.version,
            }
        conf = self.vault.read(p["tenant_id"])
        blockers, warnings = [], []
        with self.db.transaction(False) as s:
            campaign = self.scoped(s, "campaigns", p)[0]
            policy = self.required(s, "policy_versions", campaign.data["policy_id"], p)
            seller = self.required(s, "sellers", p["seller_id"], p)
            if not policy.data.get("approved"):
                blockers.append("POLICY_NOT_PUBLISHED")
            if not seller.data.get("approved"):
                blockers.append("SELLER_NOT_APPROVED")
            if seller.data.get("restore_hold"):
                blockers.append("RESTORE_RECONCILIATION_REQUIRED")
            if getattr(self, "_effect_journal_fault", False) or seller.data.get("effect_journal_pending"):
                blockers.append("EFFECT_JOURNAL_RECOVERY_REQUIRED")
            if seller.data.get("journal_recovery_required") or (
                p["tenant_id"],
                p["seller_id"],
            ) in self.__dict__.get("_journal_faults", set()):
                blockers.append("JOURNAL_RECOVERY_REQUIRED")
            numbers = self.scoped(s, "number_inventory", p)
            healthy = next(
                (n for n in numbers if n.data["lifecycle"] in {"healthy", "limited_real_use"}), None
            )
            if not healthy:
                blockers.append("NO_HEALTHY_NUMBERS")
            from app.compliance.operating import screening_capability, screening_snapshot
            from app.optimization.numbering import select_number
            from app.scheduling.policy import queue_decision

            screening = screening_capability(screening_snapshot(self, s, p))
            lead_model = MODELS["leads"]
            scope = (lead_model.tenant_id == p["tenant_id"], lead_model.seller_id == p["seller_id"])
            total = s.scalar(select(func.count()).select_from(lead_model).where(*scope))
            leads = list(s.scalars(select(lead_model).where(*scope).order_by(lead_model.id).limit(500)))
            self._queue_context(s, p, campaign.id, leads)
            inventory = self.inventory_evidence(s, p, fast=True)
            claims = s.info["active_claims"]
            own = next((c for c in claims if c.rep_id == p["user_id"]), None)
            claimed = {c.endpoint_id for c in claims}
            bindings = {
                r.data["lead_id"]: r.data["number_id"] for r in self.scoped(s, "lead_number_bindings", p)
            }
            by_number = {n.id: n for n in numbers}
            counts = {"cleared_count": 0, "pending_count": 0, "blocked_count": 0, "dialable_queue_count": 0}
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
            if not counts["cleared_count"]:
                blockers.extend(screening["reason_codes"] or ["SCREENING_PENDING"])
            if "STATE_REGISTRY_MISSING" in reasons:
                warnings.append("Some leads require a current authorized state registry source")
            if not counts["dialable_queue_count"]:
                blockers.extend(sorted(queue_reasons) or ["NO_CURRENTLY_DIALABLE_WORK"])
                if total > len(leads):
                    warnings.append("Queue inspection is bounded; further candidates may be eligible")
            if not self.budget_ok(s, p):
                blockers.append("BUDGET_EXHAUSTED")
            pending = sum(
                c.data.get("status") in {"sending", "uncertain"} for c in self.scoped(s, "command_outbox", p)
            )
            if pending:
                warnings.append("Uncertain commands require reconciliation")
        if not self.settings.simulation:
            if not conf.get("telnyx_api_key") or not conf.get("telnyx_connection_id"):
                blockers.append("TELNYX_NOT_CONFIGURED")
            if not conf.get("telnyx_public_key"):
                blockers.append("WEBHOOK_SIGNATURE_KEY_MISSING")
            if not conf.get("webhook_url", self.settings.webhook_public_url).startswith("https://"):
                blockers.append("PUBLIC_WEBHOOK_URL_MISSING")
            if not conf.get("traffic_approval_reference"):
                blockers.append("CARRIER_TRAFFIC_APPROVAL_MISSING")
            warnings.extend(
                [
                    "Live carrier/media behavior requires permissioned test calls",
                    "Each calling attempt is authorized again at submission",
                ]
            )
        else:
            warnings.append("Simulated providers: no real calls, purchases, inference, or carrier billing")
        if not self.inference(p["tenant_id"], "research_extract", p["user_id"]):
            warnings.append("Research inference is unconfigured; approved fallback scripts remain available")
        provider = conf.get("coach_provider", "openrouter")
        if provider == "openrouter" and not conf.get("openrouter_api_key"):
            warnings.append("OpenRouter/Cerebras is unconfigured; approved static coaching remains available")
        elif provider == "groq" and not conf.get("groq_api_key"):
            warnings.append("Groq is unconfigured; approved static coaching remains available")
        integrity = self.db.integrity()
        if not integrity:
            blockers.append("DATABASE_INTEGRITY_FAILURE")
        return {
            "integrity_ok": integrity,
            "ready_to_call": not blockers,
            "blockers": sorted(set(blockers)),
            "warnings": warnings,
            "mode": self.settings.mode,
            "simulation": self.settings.simulation,
            "version": self.settings.version,
            "screening": screening,
            "provider_validation": "simulated"
            if self.settings.simulation
            else "requires live account validation",
        }

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
            if not 0 <= config["local_start_hour"] < config["local_end_hour"] <= 24:
                fail(422, "Invalid reviewed local calling window")
            if config.get("approved") and not self.settings.simulation and not config.get("review_reference"):
                fail(422, "Reviewed campaign policy reference is required")
            if config.get("detached_topology_verified") and not self.settings.simulation:
                if not config.get("topology_test_reference") or not config.get(
                    "late_human_recovery_destination"
                ):
                    fail(422, "Carrier topology proof and late-human recovery destination required")
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

    def add_number(self, p, data):
        from app.compliance.phone import normalize_phone

        phone = normalize_phone(data["phone"])
        if not phone.e164:
            fail(422, "Valid owned number required")
        if not self.settings.simulation and data.get("lifecycle", "purchased") != "purchased":
            fail(409, "New inventory must pass identity/callback/warmup gates")
        with self.db.transaction() as s:
            if any(n.data["phone"] == phone.e164 for n in rows(s, "number_inventory")):
                fail(409, "Number is already assigned to a seller")
            number = put(
                s,
                "number_inventory",
                p["tenant_id"],
                p["seller_id"],
                {
                    "phone": phone.e164,
                    "region": data.get("region", phone.e164[2:5]),
                    "lifecycle": data.get("lifecycle", "purchased"),
                    "daily_limit": data.get("daily_limit", 5),
                    "capacity": data.get("capacity", 1),
                    "identity_configured": False,
                    "callback_tested": False,
                    "synthetic": self.settings.simulation,
                },
                key=phone.e164,
                now=self.clock(),
            )
            audit(s, p["tenant_id"], p["seller_id"], p["user_id"], "number.added", number.id, self.clock())
            return doc(number)

    def edit_number(self, p, number_id, body):
        from pydantic import ValidationError

        from app.number_operations import ExternalReputation

        if "reassignment" in body:
            if set(body) != {"reassignment", "request_id", "expected_version"}:
                fail(422, "Reassignment must be a separate identified edit")
            return self.reassign_number(p, number_id, body)
        for key, ceiling in (("daily_limit", 10000), ("capacity", 20)):
            if key in body and (type(body[key]) is not int or not 1 <= body[key] <= ceiling):
                fail(422, "Positive integer workload limits are required")
        if any(type(body[k]) is not bool for k in ("identity_configured", "callback_tested") if k in body):
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
            if identity:
                self.check_edit_version(number, body)
            if body.get("identity_configured") is True and not number.data.get("identity_configured"):
                fail(409, "Configure the owned carrier route; an operator note cannot verify identity")
            if body.get("callback_tested") is True and not body.get("callback_task_id"):
                fail(409, "Select a matching inbound callback receipt; a note is not evidence")
            if "callback_task_id" in body:
                if not number.data.get("identity_configured"):
                    fail(409, "Configure identity before verifying a callback")
                self.confirm_callback_in(s, p, number, body["callback_task_id"])
            next_state = body.get("lifecycle", number.data["lifecycle"])
            order = ["purchased", "identity_configured", "callback_tested", "limited_real_use", "healthy"]
            if next_state in order:
                old = number.data["lifecycle"]
                if old in order and order.index(next_state) > order.index(old) + 1:
                    fail(409, "Onboarding requires identity and current callback evidence")
                if next_state in order[1:] and not number.data.get("identity_configured"):
                    fail(409, "Confirm the owned carrier connection before promotion")
                if (
                    next_state in order[2:]
                    and not self.number_readiness_in(s, p, number)["callback_evidence_current"]
                ):
                    fail(409, "Current inbound callback proof is required")
                if next_state in order[3:] and (
                    next_state != old or any(k in body for k in ("daily_limit", "capacity"))
                ):
                    fail(409, "Use the evidence and workload review to approve limited use")
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
                if k
                not in {
                    "expected_version",
                    "request_id",
                    "callback_task_id",
                    "identity_configured",
                    "callback_tested",
                }
            }
            if "daily_limit" in values and values["daily_limit"] > number.data["daily_limit"]:
                fail(409, "Use the evidence and workload review before increasing capacity")
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

    def proposal(self, p, campaign_id, capacity=None):
        if capacity is not None and (type(capacity) is not int or not 1 <= capacity <= 10000):
            fail(422, "Positive daily capacity required")
        from app.db import Job
        from app.service import dt

        key = f"planning:{campaign_id}:{capacity or 'configured'}"
        with self.db.transaction(False) as s:
            self.required(s, "campaigns", campaign_id, p)
            current, _, _, _ = self.report_scope(s, p)
            if current["role"] != "admin":
                fail(403, "Administrator access is required to plan inventory")
            generation = self.report_revision(s, current)
            row = self._by_key(s, p, "report_projections", key)
            saved = dict(row.data) if row else {}
        fresh = bool(
            saved and saved["source_revision"] == generation and dt(saved["expires_at"]) > self.clock()
        )
        job_id, refresh_status, refresh_error, attempts = None, None, None, None
        if not fresh:
            with self.db.transaction() as s:
                self.db.authorize_stream(s, current)
                pending = s.scalar(
                    select(Job)
                    .where(
                        Job.tenant_id == p["tenant_id"],
                        Job.key.startswith(key + ":", autoescape=True),
                        Job.status.in_(["queued", "running", "dead"]),
                    )
                    .order_by(Job.available_at.desc(), Job.id)
                    .limit(1)
                )
                if pending:
                    job_id = pending.id
                    refresh_status, refresh_error, attempts = pending.status, pending.error, pending.attempts
                else:
                    job = self.jobs.enqueue_in(
                        s,
                        "ml",
                        {
                            "kind": "forecast_refresh",
                            "seller_id": p["seller_id"],
                            "user_id": p["user_id"],
                            "role": current["role"],
                            "campaign_id": campaign_id,
                            "capacity": capacity,
                        },
                        f"{key}:{generation}:{int(self.clock().timestamp()) // 60}",
                        tenant_id=p["tenant_id"],
                        priority=15,
                    )
                    job_id = job["id"]
                    refresh_status, refresh_error, attempts = job["status"], job.get("error"), job["attempts"]
        return {
            "regions": [],
            "purchased": False,
            **saved.get("report", {}),
            "projection": {
                "status": "ready" if fresh else "stale" if saved else "pending",
                "job_id": job_id,
                "as_of": saved.get("as_of"),
                "source_revision": saved.get("source_revision"),
                "current_revision": generation,
                "refresh_status": refresh_status,
                "refresh_error": refresh_error,
                "attempts": attempts,
            },
        }

    def backup(self, p):
        from app.privacy import persistent_document

        directory = self.settings.data_dir / "backups"
        directory.mkdir(exist_ok=True, mode=0o700)
        checkpoints = {
            name: self.vault.journal_checkpoint(name) for name in ("effects", "consent", "suppression")
        }
        with self.db.transaction(False) as s:
            payload = {
                "tenant_id": p["tenant_id"],
                "seller_id": p["seller_id"],
                "at": self.clock().isoformat(),
                "schema": 1,
                "journal_checkpoints": checkpoints,
                "tables": {
                    kind: [
                        {
                            "id": r.id,
                            "tenant_id": r.tenant_id,
                            "seller_id": r.seller_id,
                            "data": persistent_document(kind, r.data, legacy=True),
                            "external_key": r.external_key,
                            "version": r.version,
                            "created_at": r.created_at.isoformat(),
                        }
                        for r in (
                            rows(s, kind, p["tenant_id"]) if kind == "tenants" else self.scoped(s, kind, p)
                        )
                    ]
                    for kind in MODELS
                    if kind not in DERIVED_TABLES
                },
            }
        backup_id = uid()
        blob = self.vault.cipher.encrypt(json.dumps(payload).encode())
        path = directory / (backup_id + ".backup")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        return {"backup_id": backup_id, "checksum": hashlib.sha256(blob).hexdigest(), "encrypted": True}

    def restore(self, p, backup_id):
        from app.compliance.consent import journal_entries, replay_in
        from app.compliance.suppressions import journal_entries as suppression_entries
        from app.compliance.suppressions import replay_in as replay_suppressions_in
        from app.privacy import persistent_document

        if not re.fullmatch(r"[0-9a-f-]{36}", backup_id):
            fail(422, "Invalid backup ID")
        path = self.settings.data_dir / "backups" / (backup_id + ".backup")
        if not path.exists():
            fail(404, "Backup not found")
        payload = json.loads(self.vault.cipher.decrypt(path.read_bytes()))
        if (
            payload["tenant_id"] != p["tenant_id"]
            or payload.get("seller_id") != p["seller_id"]
            or payload.get("schema") != 1
        ):
            fail(403, "Backup identity or schema mismatch")
        # The installed schema predates observed work intervals; report caches are disposable.
        optional = {"work_intervals", *DERIVED_TABLES}
        if set(MODELS) - optional - payload.get("tables", {}).keys():
            fail(422, "Backup is missing required source tables")
        events = suppression_entries(self)
        revoked = journal_entries(self)
        effects = self.effect_entries()
        for kind, values in payload.get("tables", {}).items():
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
                if kind in {"tenants", "users", "memberships", "sessions"}:
                    # Restore business state, never old access grants, passwords or sessions.
                    continue
                if kind in DERIVED_TABLES:
                    s.execute(
                        delete(model).where(
                            model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"]
                        )
                    )
                    continue
                # Recovery fills gaps; it cannot roll back facts, effects, costs or
                # later human corrections. Do not delete and reconstruct live rows.
                values = payload["tables"].get(kind, [])
                for offset in range(0, len(values), 250):
                    batch = values[offset : offset + 250]
                    current = list(
                        s.execute(
                            select(model.id, model.tenant_id, model.seller_id, model.external_key).where(
                                or_(
                                    model.id.in_([r["id"] for r in batch]),
                                    (
                                        (model.tenant_id == p["tenant_id"])
                                        & (model.seller_id == p["seller_id"])
                                        & model.external_key.in_(
                                            [r["external_key"] for r in batch if r.get("external_key")]
                                        )
                                    ),
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
                                if row.data.get("phone") != raw["data"].get("phone"):
                                    fail(409, "Backup suppression identity changed; review before recovery")
                                if not row.data.get("active") or (
                                    raw["data"].get("permanent") and not row.data.get("permanent")
                                ):
                                    update(row, **raw["data"])
                            if kind == "consent_evidence" and raw["data"].get("revoked"):
                                row = self.required(s, kind, raw["id"], p)
                                if row.data.get("endpoint_id") != raw["data"].get("endpoint_id"):
                                    fail(409, "Backup consent identity changed; review before recovery")
                                if not row.data.get("revoked"):
                                    update(row, **raw["data"])
                            continue
                        if raw.get("external_key") in by_key:
                            fail(
                                409,
                                "Backup request identity conflicts with a current record; review before recovery",
                            )
                        s.add(
                            model(
                                **{
                                    **raw,
                                    "data": persistent_document(kind, raw["data"], legacy=True),
                                    "created_at": dt(raw.get("created_at") or payload["at"]),
                                }
                            )
                        )
                        recovered += 1
                    s.flush()
            s.flush()
            replay_suppressions_in(self, s, p=p, entries=events)
            for command in self.scoped(s, "command_outbox", p):
                if command.data.get("status") in {"pending", "sending"}:
                    update(command, status="uncertain", restore_hold=True)
            replay_in(self, s, p=p, entries=revoked)
            recovered += self.replay_effects_in(s, p=p, entries=effects)
            seller = self.required(s, "sellers", p["seller_id"], p)
            update(seller, restore_hold=True)
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
                {
                    "journal_replayed": len(events),
                    "recovered_records": recovered,
                    "mode": "recover_missing_keep_current",
                },
            )
        self.raw.clear()
        return {
            "restored": True,
            "mode": "recover_missing_keep_current",
            "recovered_records": recovered,
            "suppression_journal_replayed": len(events),
            "outbound_paused": True,
        }

    def _restore_operator(self, session, p):
        user = self.required(session, "users", p["user_id"], p)
        if user.data.get("disabled") or user.data.get("role") not in {"owner", "admin"}:
            fail(403, "Current administrator authority is required for restore")

    def _restore_journal(self, p):
        from app.compliance.consent import journal_entries
        from app.compliance.suppressions import journal_entries as suppression_entries

        records, fingerprint = {}, hashlib.sha256()
        path = self.settings.data_dir / "authorization-journal.enc"
        if path.exists():
            with path.open("rb") as stream:
                for line in stream:
                    entry = json.loads(self.vault.cipher.decrypt(line.strip()))
                    if entry["tenant_id"] != p["tenant_id"] or entry["seller_id"] != p["seller_id"]:
                        continue
                    fingerprint.update(line)
                    previous = records.get(entry["call_id"], {})
                    records[entry["call_id"]] = {
                        **previous,
                        **entry,
                        "provider_id": entry.get("provider_id") or previous.get("provider_id"),
                    }
        for entry in [*journal_entries(self), *suppression_entries(self)]:
            if entry["tenant_id"] == p["tenant_id"] and entry["seller_id"] == p["seller_id"]:
                fingerprint.update(json.dumps(entry, sort_keys=True).encode())
        for entry in self.effect_entries():
            if (entry["tenant_id"], entry["seller_id"]) != (p["tenant_id"], p["seller_id"]):
                continue
            fingerprint.update(json.dumps(entry, sort_keys=True).encode())
            for entity in entry.get("entities", []):
                if entity["table"] != "call_attempts":
                    continue
                previous = records.get(entity["id"], {})
                records[entity["id"]] = {
                    "call_id": entity["id"],
                    "tenant_id": p["tenant_id"],
                    "seller_id": p["seller_id"],
                    "intent": {**previous.get("intent", {}), **entity["data"]},
                    "provider_id": entry.get("provider_id")
                    or entity["data"].get("provider_id")
                    or previous.get("provider_id"),
                }
        return records, fingerprint.hexdigest()

    def _restore_snapshot(self, p, provider_ids):
        records, fingerprint = self._restore_journal(p)
        pending = []
        with self.db.transaction(False) as session:
            self._restore_operator(session, p)
            seller_version = self.required(session, "sellers", p["seller_id"], p).version
            model = MODELS["call_attempts"]
            for call in session.scalars(
                select(model).where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.op_status.not_in(TERMINAL),
                )
            ):
                records.setdefault(call.id, {"call_id": call.id, "intent": dict(call.data)})
            for call_id, entry in records.items():
                current = get(session, "call_attempts", call_id, p["tenant_id"])
                if current and current.seller_id != p["seller_id"]:
                    fail(409, "Journal call belongs to another seller")
                known = entry.get("provider_id")
                saved = current.data.get("provider_id") if current else None
                if known and saved and known != saved:
                    fail(409, "Saved carrier references conflict; review the original call")
                known = known or saved
                supplied = provider_ids.get(call_id)
                if supplied and known and supplied != known:
                    fail(409, "Cannot replace an established carrier reference")
                if current and current.data["state"] in TERMINAL:
                    continue
                pending.append(
                    {
                        "entry": entry,
                        "target": known or supplied,
                        "known": bool(known),
                        "version": current.version if current else None,
                    }
                )
        return pending, records, fingerprint, seller_version

    def _restore_terminal(self, p, item, receipt):
        entry, target = item["entry"], item["target"]
        verified = call_receipt(receipt, target, simulation=self.settings.simulation)
        if not verified or verified[1]:
            return False
        if not item["known"] and not self.settings.simulation:
            try:
                context = json.loads(base64.b64decode(receipt.get("client_state", "")))
            except (ValueError, TypeError):
                context = None
            if (
                not isinstance(context, dict)
                or context.get("call_id") != entry["call_id"]
                or context.get("tenant_id") != p["tenant_id"]
            ):
                return False
        with self.db.transaction() as session:
            self.db.authorize_stream(session, p)
            self._restore_operator(session, p)
            current = get(session, "call_attempts", entry["call_id"], p["tenant_id"])
            if current:
                if current.seller_id != p["seller_id"] or current.data.get("provider_id") not in {
                    None,
                    verified[0],
                }:
                    return False
                if current.data["state"] in TERMINAL:
                    return True
                if current.version != item["version"]:
                    return False
                if not current.data.get("provider_id"):
                    update(current, provider_id=verified[0])
            elif item["version"] is not None:
                return False
            else:
                current = put(
                    session,
                    "call_attempts",
                    p["tenant_id"],
                    p["seller_id"],
                    {
                        **entry["intent"],
                        "provider_id": verified[0],
                        "state": "ENDED",
                        "restore_recovered": True,
                    },
                    object_id=entry["call_id"],
                    now=self.clock(),
                )
            self.finish_call(session, p, current, "restored_provider_terminal_receipt")
        return True

    def _restore_release(self, p, fingerprint, seller_version, review_reference, count):
        from app.compliance.consent import journal_entries, replay_in, verify_recovery
        from app.compliance.suppressions import replay_in as replay_suppressions_in

        with self.db.transaction() as session:
            self.db.authorize_stream(session, p)
            self._restore_operator(session, p)
            seller = self.required(session, "sellers", p["seller_id"], p)
            if seller.version != seller_version or self._restore_journal(p)[1] != fingerprint:
                fail(409, "Restore or authorization journal changed; reconcile again before resuming")
            calls = MODELS["call_attempts"]
            if session.scalar(
                select(calls.id)
                .where(
                    calls.tenant_id == p["tenant_id"],
                    calls.seller_id == p["seller_id"],
                    calls.op_status.not_in(TERMINAL),
                )
                .limit(1)
            ):
                fail(409, "A live call obligation remains; reconcile before resuming")
            revocations = journal_entries(self)
            verify_recovery(self, session, p, revocations)
            replay_suppressions_in(self, session, p=p)
            replay_in(self, session, p=p, entries=revocations)
            update(
                seller,
                restore_hold=False,
                journal_recovery_required=False,
                journal_expected={},
                restore_review_reference=review_reference,
            )
            audit(
                session,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "restore.reconciled",
                seller.id,
                self.clock(),
                {"journal_calls": count, "reference": review_reference},
            )
        self.__dict__.get("_journal_faults", set()).discard((p["tenant_id"], p["seller_id"]))
        return {"reconciled": True, "journal_calls": count, "outbound_remains_paused_until_rep_ready": True}

    async def reconcile_restore(self, p, review_reference, provider_ids=None):
        if not review_reference:
            fail(422, "A reviewed carrier and authorization-journal reconciliation reference is required")
        pending, records, fingerprint, seller_version = await asyncio.to_thread(
            self._restore_snapshot, p, provider_ids or {}
        )
        unresolved = []
        for item in pending:
            try:
                provider = await asyncio.to_thread(self.carrier, p["tenant_id"])
                receipt = await provider.reconcile(item["entry"]["call_id"], item["target"])
            except Exception:
                receipt = None
            if not await asyncio.to_thread(self._restore_terminal, p, item, receipt):
                unresolved.append(item["entry"]["call_id"])
        if unresolved:
            fail(409, {"code": "AUTHORIZATION_RECONCILIATION_PENDING", "call_ids": unresolved})
        return await asyncio.to_thread(
            self._restore_release, p, fingerprint, seller_version, review_reference, len(records)
        )
