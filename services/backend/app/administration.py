import asyncio
import base64
import hashlib
import json
import os
import re

from sqlalchemy import delete, or_, select

from app.db import MODELS, audit, doc, emit, get, put, rows, uid, update
from app.intelligence import Application
from app.measurement import Measurement, cohort_metrics
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
        with self.db.transaction(False) as s:
            leads = [doc(r) for r in self.scoped(s, "leads", p)]
            calls = [doc(r) for r in self.scoped(s, "call_attempts", p)]
            events = [doc(r) for r in self.scoped(s, "conversion_events", p)]
            blocked = {
                r.data["phone"] for r in self.scoped(s, "suppressions", p) if r.data.get("active", True)
            }
            costs = [r.data for r in self.scoped(s, "usage_ledger", p)]
            return {
                **cohort_metrics(
                    leads,
                    calls,
                    events,
                    blocked,
                    self.work_in(s, p, calls),
                    self.optimization_config(s, p),
                    now=self.clock(),
                ),
                "reserved_cost_usd": sum(r.get("reserved_usd", 0) for r in costs),
                "verified_cost_usd": sum(
                    (r.get("cost_usd") or 0) - r.get("credited_usd", 0)
                    for r in costs
                    if r.get("billing_verified")
                ),
                "gross_cost_usd": sum(r.get("cost_usd") or 0 for r in costs if r.get("billing_verified")),
                "credited_cost_usd": sum(r.get("credited_usd", 0) for r in costs),
                "billing_verified": bool(costs) and all(r.get("billing_verified") for r in costs),
                "simulation": self.settings.simulation,
            }

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
            if seller.data.get("journal_recovery_required") or (
                p["tenant_id"],
                p["seller_id"],
            ) in self.__dict__.get("_journal_faults", set()):
                blockers.append("JOURNAL_RECOVERY_REQUIRED")
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
        for key, ceiling in (("daily_limit", 10000), ("capacity", 20)):
            if key in body and (type(body[key]) is not int or not 1 <= body[key] <= ceiling):
                fail(422, "Positive integer workload limits are required")
        with self.db.transaction() as s:
            number = self.required(s, "number_inventory", number_id, p)
            next_state = body.get("lifecycle", number.data["lifecycle"])
            order = ["purchased", "identity_configured", "callback_tested", "limited_real_use", "healthy"]
            if next_state in order:
                old = number.data["lifecycle"]
                if old in order and order.index(next_state) > order.index(old) + 1:
                    fail(409, "Warmup stages require identity and real callback evidence")
                merged = {**number.data, **body}
                if (
                    not self.settings.simulation
                    and next_state in order[1:]
                    and merged.get("configuration_status") != "confirmed"
                ):
                    fail(409, "Confirm the owned carrier connection before promoting number identity")
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

    def proposal(self, p, campaign_id, capacity=None):
        if capacity is not None and (type(capacity) is not int or not 1 <= capacity <= 10000):
            fail(422, "Positive daily capacity required")
        with self.db.transaction(False) as s:
            return self.forecast(s, p, campaign_id=campaign_id, capacity=capacity)

    def backup(self, p):
        from app.privacy import persistent_document

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
                    if kind != "report_projections"
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
        optional = {"work_intervals", "report_projections"}
        if set(MODELS) - optional - payload.get("tables", {}).keys():
            fail(422, "Backup is missing required source tables")
        journal = self.settings.data_dir / "suppression-journal.enc"
        events = (
            [json.loads(self.vault.cipher.decrypt(line)) for line in journal.read_bytes().splitlines()]
            if journal.exists()
            else []
        )
        revoked = journal_entries(self)
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
                if kind == "report_projections":
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
            suppressions = {r.data["phone"]: r for r in self.scoped(s, "suppressions", p)}
            for entry in events:
                if entry["tenant_id"] != p["tenant_id"] or entry.get("seller_id") != p["seller_id"]:
                    continue
                existing = suppressions.get(entry["phone"])
                if existing and existing.data.get("active") and existing.data.get("permanent"):
                    continue
                values = {
                    "phone": entry["phone"],
                    "reason": entry["reason"],
                    "permanent": entry["reason"] != "temporary_hold",
                    "active": True,
                    "effective_at": entry["at"],
                }
                if existing:
                    update(existing, **values)
                else:
                    suppressions[entry["phone"]] = put(
                        s,
                        "suppressions",
                        p["tenant_id"],
                        p["seller_id"],
                        values,
                        key=entry["phone"],
                        now=self.clock(),
                    )
            for command in self.scoped(s, "command_outbox", p):
                if command.data.get("status") in {"pending", "sending"}:
                    update(command, status="uncertain", restore_hold=True)
            replay_in(self, s, p=p, entries=revoked)
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
        for entry in journal_entries(self):
            if entry["tenant_id"] == p["tenant_id"] and entry["seller_id"] == p["seller_id"]:
                fingerprint.update(json.dumps(entry, sort_keys=True).encode())
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
