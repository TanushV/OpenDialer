import hashlib
import json
import logging
import os
import re

from sqlalchemy import delete, select

from app.db import MODELS, ActiveClaim, Job, audit, doc, emit, get, put, rows, uid, update
from app.intelligence import Application
from app.optimization.workflows import OptimizationWorkflows
from app.service import dt, fail
from app.telephony.reducer import TERMINAL
from app.telephony.workflows import TelephonyWorkflows


class Dialer(OptimizationWorkflows, TelephonyWorkflows, Application):
    def metrics(self, p):
        with self.db.transaction(False) as s:
            leads = self.scoped(s, "leads", p)
            calls = self.scoped(s, "call_attempts", p)
            suppressed = [lead for lead in leads if self.suppressed(s, p, lead.data["phone"])]
            eligible = [lead for lead in leads if lead not in suppressed]
            right = {
                c.data["lead_id"]
                for c in calls
                if c.data.get("outcome")
                in {"right_party", "meeting_booked", "meeting_held", "callback_requested"}
            }
            held = {c.data["lead_id"] for c in calls if c.data.get("outcome") == "meeting_held"}
            human = sum(
                c.data.get("outcome")
                in {"right_party", "gatekeeper", "meeting_booked", "meeting_held", "callback_requested"}
                for c in calls
            )
            costs = [r.data for r in self.scoped(s, "usage_ledger", p)]
            overdue = sum(
                dt(lead.data["coverage_due_at"]) < self.clock() and not lead.data.get("attempt_count")
                for lead in eligible
            )
            return {
                "original_cohort": len(leads),
                "eligible_now": len(eligible),
                "suppressed": len(suppressed),
                "attempts": len(calls),
                "human_pickups": human,
                "right_party_unique": len(right),
                "held_meetings": len(held),
                "unique_reach_per_original_lead": len(right) / len(leads) if leads else 0,
                "held_meetings_per_original_lead": len(held) / len(leads) if leads else 0,
                "coverage_overdue": overdue,
                "coverage_shortfall": max(0, overdue - 40),
                "reserved_cost_usd": sum(r.get("reserved_usd", 0) for r in costs),
                "verified_cost_usd": sum(r.get("cost_usd") or 0 for r in costs if r.get("billing_verified")),
                "billing_verified": bool(costs) and all(r.get("billing_verified") for r in costs),
                "original_eligible_denominator": len(leads),
                "pending_outcome_windows": sum(
                    (self.clock() - dt(c.data["started_at"])).days < 14 for c in calls
                ),
                "simulation": self.settings.simulation,
            }

    def preflight(self, p):
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
            if not any(
                n.data["lifecycle"] in {"healthy", "limited_real_use"}
                for n in self.scoped(s, "number_inventory", p)
            ):
                blockers.append("NO_HEALTHY_NUMBERS")
            if policy.data.get("registry_required", True):
                registries = [
                    r
                    for r in self.scoped(s, "registry_snapshots", p)
                    if r.data.get("published") and r.data["kind"] == "national"
                ]
                if not registries:
                    blockers.append("REGISTRY_MISSING")
                elif all((self.clock() - dt(r.data["refreshed_at"])).days > 31 for r in registries):
                    blockers.append("REGISTRY_STALE")
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
            "blockers": blockers,
            "warnings": warnings,
            "mode": self.settings.mode,
            "simulation": self.settings.simulation,
            "version": self.settings.version,
            "provider_validation": "simulated"
            if self.settings.simulation
            else "requires live account validation",
        }

    def snapshot(self, p):
        with self.db.transaction(False) as s:
            campaign = self.scoped(s, "campaigns", p)[0]
            policy = self.required(s, "policy_versions", campaign.data["policy_id"], p)
            user = self.required(s, "users", p["user_id"], p)
            tables = {
                name: [doc(r) for r in self.scoped(s, table, p)]
                for name, table in [
                    ("numbers", "number_inventory"),
                    ("calls", "call_attempts"),
                    ("registries", "registry_snapshots"),
                    ("playbooks", "playbooks"),
                    ("models", "model_versions"),
                    ("tasks", "followup_tasks"),
                    ("trackers", "trackers"),
                ]
            }
            if p["role"] == "rep":
                tables["calls"] = [c for c in tables["calls"] if c["rep_id"] == p["user_id"]]
            # Never distribute compliance dataset contents or raw source/transcript/secret tables in global snapshots.
            tables["registries"] = [
                {k: v for k, v in r.items() if k not in {"listed_numbers", "bitmap"}}
                for r in tables["registries"]
            ]
            jobs = [
                {"id": j.id, "queue": j.queue, "status": j.status, "attempts": j.attempts, "error": j.error}
                for j in s.scalars(select(Job).where(Job.tenant_id == p["tenant_id"]))
            ]
            current = {
                "mode": self.settings.mode,
                "simulation": self.settings.simulation,
                "seller": doc(self.required(s, "sellers", p["seller_id"], p)),
                "campaign": doc(campaign),
                "policy": doc(policy),
                "user": {
                    "id": user.id,
                    "name": user.data["name"],
                    "role": user.data["role"],
                    "presence": user.data.get("presence", {}),
                },
                "jobs": jobs,
                **tables,
            }
        return {**current, "leads": self.leads(p), "metrics": self.metrics(p), "readiness": self.preflight(p)}

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
        with self.db.transaction() as s:
            number = self.required(s, "number_inventory", number_id, p)
            next_state = body.get("lifecycle", number.data["lifecycle"])
            order = ["purchased", "identity_configured", "callback_tested", "limited_real_use", "healthy"]
            if next_state in order:
                old = number.data["lifecycle"]
                if old in order and order.index(next_state) > order.index(old) + 1:
                    fail(409, "Warmup stages require identity and real callback evidence")
                merged = {**number.data, **body}
                if next_state in {"callback_tested", "limited_real_use", "healthy"} and not merged.get(
                    "identity_configured"
                ):
                    fail(409, "Seller identity must be configured")
                if next_state in {"limited_real_use", "healthy"} and not merged.get("callback_tested"):
                    fail(409, "Inbound callback test evidence is required")
                if (
                    not self.settings.simulation
                    and next_state == "healthy"
                    and not merged.get("real_use_review_reference")
                ):
                    fail(409, "Review of legitimate limited real use is required")
            elif next_state not in {
                "investigating",
                "paused_for_outbound",
                "remediated",
                "retired_callback_only",
            }:
                fail(422, "Invalid lifecycle")
            update(number, **body)
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "number.updated",
                number.id,
                self.clock(),
                {"reason": body.get("reason", "")},
            )
            return doc(number)

    def proposal(self, p, campaign_id, capacity=40):
        if capacity < 1:
            fail(422, "Positive daily capacity required")
        with self.db.transaction(False) as s:
            return self.forecast(s, p, campaign_id=campaign_id, capacity=capacity)

    async def recover(self, p):
        self.jobs.recover_expired()
        with self.db.transaction() as s:
            self._expire_claims(s)
            calls = [doc(c) for c in self.scoped(s, "call_attempts", p) if c.data["state"] not in TERMINAL]
            commands = [
                doc(c)
                for c in self.scoped(s, "command_outbox", p)
                if c.data.get("status") in {"pending", "sending", "uncertain"}
            ]
        for command in commands:
            if command["kind"] == "dial":
                if command["status"] == "pending":
                    await self.dispatch_call(p, command["call_id"])
                else:
                    receipt = await self.carrier(p["tenant_id"]).reconcile(
                        command["call_id"], command.get("provider_id")
                    )
                    if receipt:
                        with self.db.transaction() as s:
                            call = self.required(s, "call_attempts", command["call_id"], p)
                            if call.data["state"] not in TERMINAL:
                                update(
                                    call,
                                    provider_id=receipt["call_control_id"],
                                    state="RINGING" if self.settings.simulation else "DIALING",
                                )
                            update(
                                self.required(s, "command_outbox", command["id"], p),
                                status="completed",
                                provider_id=receipt["call_control_id"],
                            )
            elif command["kind"] == "hangup":
                with self.db.transaction(False) as s:
                    target_call = self.required(s, "call_attempts", command["call_id"], p)
                    provider_id = command.get("provider_id") or target_call.data.get("provider_id")
                if not provider_id:
                    continue
                try:
                    await self.carrier(p["tenant_id"]).hangup(provider_id, command["call_id"] + ":hangup")
                    with self.db.transaction() as s:
                        update(
                            self.required(s, "command_outbox", command["id"], p),
                            status="completed",
                            provider_id=provider_id,
                        )
                        if self.settings.simulation:
                            self.finish_call(
                                s,
                                p,
                                self.required(s, "call_attempts", command["call_id"], p),
                                "simulated_recovery_hangup",
                            )
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "Carrier reconciliation deferred: %s", type(exc).__name__
                    )
                    continue
        for call in calls:
            if call["state"] == "ENDING" and call.get("provider_id"):
                receipt = await self.carrier(p["tenant_id"]).reconcile(call["id"], call["provider_id"])
                if receipt and (
                    receipt.get("is_alive") is False
                    or receipt.get("state") in {"hangup", "ended", "completed"}
                ):
                    with self.db.transaction() as s:
                        self.finish_call(
                            s,
                            p,
                            self.required(s, "call_attempts", call["id"], p),
                            "provider_terminal_reconciliation",
                        )
                    continue
            if (self.clock() - dt(call["started_at"])).total_seconds() > self.settings.max_call_seconds:
                await self.hangup(p, call["id"])
        return {"reconciled": len(commands), "examined_calls": len(calls)}

    def backup(self, p):
        directory = self.settings.data_dir / "backups"
        directory.mkdir(exist_ok=True, mode=0o700)
        with self.db.transaction(False) as s:
            payload = {
                "tenant_id": p["tenant_id"],
                "seller_id": p["seller_id"],
                "at": self.clock().isoformat(),
                "schema": 1,
                "tables": {
                    kind: [
                        {
                            "id": r.id,
                            "tenant_id": r.tenant_id,
                            "seller_id": r.seller_id,
                            "data": r.data,
                            "external_key": r.external_key,
                            "version": r.version,
                        }
                        for r in rows(s, kind, p["tenant_id"])
                    ]
                    for kind in MODELS
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
        if not re.fullmatch(r"[0-9a-f-]{36}", backup_id):
            fail(422, "Invalid backup ID")
        path = self.settings.data_dir / "backups" / (backup_id + ".backup")
        if not path.exists():
            fail(404, "Backup not found")
        payload = json.loads(self.vault.cipher.decrypt(path.read_bytes()))
        if payload["tenant_id"] != p["tenant_id"] or payload.get("schema") != 1:
            fail(403, "Backup identity or schema mismatch")
        journal = self.settings.data_dir / "suppression-journal.enc"
        events = (
            [json.loads(self.vault.cipher.decrypt(line)) for line in journal.read_bytes().splitlines()]
            if journal.exists()
            else []
        )
        with self.db.transaction() as s:
            # Preserve append-only current audit events even when restoring older business state.
            current_audit = [
                {
                    "id": r.id,
                    "tenant_id": r.tenant_id,
                    "seller_id": r.seller_id,
                    "data": r.data,
                    "external_key": r.external_key,
                    "version": r.version,
                }
                for r in self.scoped(s, "audit_events", p)
            ]
            for kind, model in MODELS.items():
                s.execute(delete(model).where(model.tenant_id == p["tenant_id"]))
                values = payload["tables"][kind]
                if kind == "audit_events":
                    values = list({r["id"]: r for r in [*values, *current_audit]}.values())
                for raw in values:
                    s.add(model(**raw))
            s.execute(delete(ActiveClaim).where(ActiveClaim.tenant_id == p["tenant_id"]))
            s.flush()
            for entry in events:
                if entry["tenant_id"] != p["tenant_id"]:
                    continue
                if not any(r.data["phone"] == entry["phone"] for r in self.scoped(s, "suppressions", p)):
                    put(
                        s,
                        "suppressions",
                        p["tenant_id"],
                        p["seller_id"],
                        {
                            "phone": entry["phone"],
                            "reason": entry["reason"],
                            "permanent": entry["reason"] != "temporary_hold",
                            "active": True,
                            "effective_at": entry["at"],
                        },
                        key=entry["phone"],
                        now=self.clock(),
                    )
            for command in self.scoped(s, "command_outbox", p):
                if command.data.get("status") in {"pending", "sending"}:
                    update(command, status="uncertain", restore_hold=True)
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
                {"journal_replayed": len(events)},
            )
        return {"restored": True, "suppression_journal_replayed": len(events), "outbound_paused": True}

    async def reconcile_restore(self, p, review_reference, provider_ids=None):
        if not review_reference:
            fail(422, "A reviewed carrier and authorization-journal reconciliation reference is required")
        journal_path = self.settings.data_dir / "authorization-journal.enc"
        records = {}
        if journal_path.exists():
            for line in journal_path.read_bytes().splitlines():
                entry = json.loads(self.vault.cipher.decrypt(line))
                if entry["tenant_id"] == p["tenant_id"] and entry["seller_id"] == p["seller_id"]:
                    previous = records.get(entry["call_id"], {})
                    records[entry["call_id"]] = {
                        **previous,
                        **entry,
                        "provider_id": entry.get("provider_id") or previous.get("provider_id"),
                    }
        unresolved = []
        for call_id, entry in records.items():
            with self.db.transaction(False) as session:
                current = get(session, "call_attempts", call_id, p["tenant_id"])
                if current and current.data["state"] in TERMINAL:
                    continue
            provider_id = (provider_ids or {}).get(call_id) or entry.get("provider_id")
            receipt = await self.carrier(p["tenant_id"]).reconcile(call_id, provider_id)
            ended = receipt and (
                receipt.get("is_alive") is False or receipt.get("state") in {"hangup", "ended", "completed"}
            )
            if not ended:
                unresolved.append(call_id)
                continue
            with self.db.transaction() as session:
                current = get(session, "call_attempts", call_id, p["tenant_id"])
                if not current:
                    current = put(
                        session,
                        "call_attempts",
                        p["tenant_id"],
                        p["seller_id"],
                        {
                            **entry["intent"],
                            "provider_id": receipt.get("call_control_id", provider_id),
                            "state": "ENDED",
                            "restore_recovered": True,
                        },
                        object_id=call_id,
                        now=self.clock(),
                    )
                else:
                    self.finish_call(session, p, current, "restored_provider_terminal_receipt")
        if unresolved:
            fail(409, {"code": "AUTHORIZATION_RECONCILIATION_PENDING", "call_ids": unresolved})
        with self.db.transaction() as session:
            seller = self.required(session, "sellers", p["seller_id"], p)
            update(seller, restore_hold=False, restore_review_reference=review_reference)
            audit(
                session,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "restore.reconciled",
                seller.id,
                self.clock(),
                {"journal_calls": len(records), "reference": review_reference},
            )
        return {
            "reconciled": True,
            "journal_calls": len(records),
            "outbound_remains_paused_until_rep_ready": True,
        }
