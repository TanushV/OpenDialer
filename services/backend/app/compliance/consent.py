"""Scoped consent evidence and an independently durable revocation journal."""

import hashlib
import hmac
import json
import logging
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import select

from app.db import MODELS, audit, doc, emit, get, put, update
from app.service import dt, fail
from app.telephony.reducer import TERMINAL


class ConsentChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint_id: str = Field(min_length=1, max_length=100)
    permissions: list[Literal["live", "transcription", "recording", "prerecorded", "messaging"]] = Field(
        min_length=1, max_length=5
    )
    revoked: StrictBool = False
    evidence_reference: str = Field(min_length=1, max_length=1000)
    acquired_at: AwareDatetime | None = None
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")

    @field_validator("evidence_reference")
    @classmethod
    def reference(cls, value):
        if not value.strip():
            raise ValueError("Evidence reference is required")
        return value.strip()

    @field_validator("permissions")
    @classmethod
    def scopes(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Duplicate permission scope")
        return sorted(values)


def journal_entries(service):
    entries = service.vault.read_journal("consent")
    try:
        for entry in entries:
            if not all(
                isinstance(entry.get(k), str) and entry[k]
                for k in ("id", "tenant_id", "seller_id", "user_id", "phone", "at", "request_hash")
            ):
                raise ValueError("Incomplete consent identity")
            body = ConsentChange.model_validate(entry["request"])
            if not body.revoked or dt(entry["at"]).tzinfo is None:
                raise ValueError("Invalid denial record")
            if request_hash(body.model_dump(mode="json")) != entry["request_hash"]:
                raise ValueError("Consent request changed")
    except (ValueError, KeyError, TypeError):
        fail(409, "Consent journal cannot be verified; recover its protected copy before resuming")
    return entries


def request_hash(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def permission_state(service, s, p, endpoint_id, permission):
    cached = s.info.get("row_cache", {}).get(("consent_evidence", p["tenant_id"], p["seller_id"]))
    if cached is None:
        model = MODELS["consent_evidence"]
        cached = s.scalars(
            select(model).where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.ctx_key == endpoint_id,
            )
        )
    values = [r.data for r in cached if r.data.get("endpoint_id") == endpoint_id]
    revoked = any(
        c.get("revoked") and (not c.get("permissions") or permission in c["permissions"]) for c in values
    )
    granted = any(not c.get("revoked") and permission in c.get("permissions", []) for c in values)
    return granted, revoked


def verify_recovery(service, s, p, entries):
    from app.compliance.suppressions import fingerprint, identity
    from app.compliance.suppressions import journal_entries as suppression_entries

    seller = service.required(s, "sellers", p["seller_id"], p)
    available = {
        e["id"]: e["request_hash"]
        for e in entries
        if e["tenant_id"] == p["tenant_id"] and e["seller_id"] == p["seller_id"]
    }
    available.update(
        {
            identity(e): fingerprint(e)
            for e in suppression_entries(service)
            if e["tenant_id"] == p["tenant_id"] and e["seller_id"] == p["seller_id"]
        }
    )
    if any(
        available.get(identity) != digest
        for identity, digest in seller.data.get("journal_expected", {}).items()
    ):
        fail(
            409,
            "The original revocation is missing from the journal; retry that request after repairing storage",
        )
    if seller.data.get("journal_recovery_required") or (
        p["tenant_id"],
        p["seller_id"],
    ) in service.__dict__.get("_journal_faults", set()):
        try:
            for name in ("consent", "suppression"):
                if (service.settings.data_dir / (name + "-journal.enc")).exists():
                    service.vault.sync_journal(name)
        except OSError:
            fail(503, "Journal storage is still unavailable; outbound remains held")


def revoke_effects(service, s, p, endpoint_id, permissions):
    model = MODELS["call_attempts"]
    calls = list(
        s.scalars(
            select(model)
            .where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.op_endpoint_id == endpoint_id,
                model.op_status.not_in(TERMINAL),
            )
            .order_by(model.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    for call in calls:
        if call.data["state"] in TERMINAL:
            continue
        version = call.version
        if "transcription" in permissions or "live" in permissions:
            if not call.data.get("transcription_disabled"):
                update(
                    call,
                    transcription_disabled=True,
                    transcript_version=call.data.get("transcript_version", 0) + 1,
                )
            # Also fences a raw publication already committed before this revocation.
            service.raw.revoke_scope(p["tenant_id"] + ":" + call.id)
            service.coach.cancel(call.id)
            service.control_intent(
                s, p, call.id, "transcription_stop", {}, target=call.data.get("provider_id")
            )
        if "recording" in permissions:
            service.control_intent(s, p, call.id, "record_stop", {}, target=call.data.get("provider_id"))
        if "prerecorded" in permissions:
            service.control_intent(s, p, call.id, "playback_stop", {}, target=call.data.get("provider_id"))
        if "live" in permissions:
            command = service._by_key(s, p, "command_outbox", call.id)
            if command and command.data["status"] == "pending" and not command.data.get("sent_at"):
                update(command, status="cancelled", reason="CONSENT_REVOKED")
                service.finish_call(s, p, call, "consent_revoked_before_submission")
            else:
                if call.data["state"] != "ENDING":
                    update(call, state="ENDING")
                service.control_intent(s, p, call.id, "hangup", {}, target=call.data.get("provider_id"))
        if call.version != version:
            emit(s, p["tenant_id"], "call.updated", call, service.clock(), {"permission_changed": True})


def replay_in(service, s, *, p=None, entries=None):
    entries = journal_entries(service) if entries is None else entries
    count = 0
    for entry in entries:
        if p and (entry["tenant_id"] != p["tenant_id"] or entry["seller_id"] != p["seller_id"]):
            continue
        scope = {k: entry[k] for k in ("tenant_id", "seller_id", "user_id")}
        seller = get(s, "sellers", scope["seller_id"], scope["tenant_id"])
        if not seller:
            continue  # No authority exists for that seller in this store.
        body = entry["request"]
        key = "consent:" + body["idempotency_key"]
        prior = service._by_key(s, scope, "consent_evidence", key)
        if prior:
            if prior.data.get("request_hash") != entry["request_hash"] or not prior.data.get("revoked"):
                fail(409, "Consent journal conflicts with saved evidence; review the original identity")
        endpoint = get(s, "phone_endpoints", body["endpoint_id"], scope["tenant_id"])
        if endpoint and (
            endpoint.seller_id != scope["seller_id"] or endpoint.data["phone"] != entry["phone"]
        ):
            fail(409, "Consent journal endpoint identity conflicts with current records")
        if not endpoint:
            phone_key = hmac.new(service.db.identity_key, entry["phone"].encode(), hashlib.sha256).hexdigest()
            endpoint = service._by_key(s, scope, "phone_endpoints", phone_key)
        if not endpoint:
            endpoint = put(
                s,
                "phone_endpoints",
                scope["tenant_id"],
                scope["seller_id"],
                {
                    "phone": entry["phone"],
                    "format_status": "valid",
                    "ownership_status": "unknown",
                    "assignment_evidence": "unknown",
                    "reachability_status": "unknown",
                },
                key=entry["phone"],
                object_id=body["endpoint_id"],
                now=dt(entry["at"]),
            )
        revoke_effects(service, s, scope, endpoint.id, body["permissions"])
        if prior:
            if prior.data["endpoint_id"] != endpoint.id:
                update(prior, endpoint_id=endpoint.id)
        else:
            put(
                s,
                "consent_evidence",
                scope["tenant_id"],
                scope["seller_id"],
                {
                    **body,
                    "endpoint_id": endpoint.id,
                    "phone": entry["phone"],
                    "acquired_at": body.get("acquired_at") or entry["at"],
                    "request_hash": entry["request_hash"],
                    "recorded_at": entry["at"],
                },
                key=key,
                object_id=entry["id"],
                now=dt(entry["at"]),
            )
        if prior:
            continue
        audit(
            s,
            scope["tenant_id"],
            scope["seller_id"],
            scope["user_id"],
            "consent.revocation_replayed",
            entry["id"],
            service.clock(),
        )
        count += 1
    return count


def replay(service):
    entries = journal_entries(service)
    if not entries:
        return
    with service.db.transaction() as s:
        for tenant_id, seller_id in sorted({(e["tenant_id"], e["seller_id"]) for e in entries}):
            service.db.authorize_stream(s, {"tenant_id": tenant_id, "seller_id": seller_id})
        replay_in(service, s, entries=entries)


def record(service, p, body):
    journal_started = False
    try:
        with service.db.transaction() as s:
            service.db.authorize_stream(s, p)
            user = service.required(s, "users", p["user_id"], p)
            if user.data.get("disabled") or user.data.get("role") not in {"owner", "admin"}:
                fail(403, "Current administrator authority is required")
            endpoint = service.required(s, "phone_endpoints", body["endpoint_id"], p)
            if body.get("acquired_at") and dt(body["acquired_at"]) > service.clock():
                fail(422, "Permission evidence cannot come from a future time")
            replay_in(service, s, p=p)
            key, digest = "consent:" + body["idempotency_key"], request_hash(body)
            prior = service._by_key(s, p, "consent_evidence", key)
            if prior:
                if prior.data.get("request_hash") != digest:
                    fail(409, "Consent request identity already describes different evidence")
                return doc(prior)
            identity = str(
                uuid5(
                    NAMESPACE_URL,
                    json.dumps(
                        ["opendialer-consent", p["tenant_id"], p["seller_id"], body["idempotency_key"]]
                    ),
                )
            )
            at = service.clock()
            expected = service.required(s, "sellers", p["seller_id"], p).data.get("journal_expected", {})
            if identity in expected and expected[identity] != digest:
                fail(409, "Recover the original revocation request; its identity cannot change evidence")
            if body["revoked"]:
                journal_started = True
                service.vault.append_journal(
                    "consent",
                    {
                        **{k: p[k] for k in ("tenant_id", "seller_id", "user_id")},
                        "id": identity,
                        "at": at.isoformat(),
                        "phone": endpoint.data["phone"],
                        "request": body,
                        "request_hash": digest,
                    },
                )
            if body["revoked"]:
                revoke_effects(service, s, p, endpoint.id, body["permissions"])
            row = put(
                s,
                "consent_evidence",
                p["tenant_id"],
                p["seller_id"],
                {
                    **body,
                    "acquired_at": body.get("acquired_at") or at.isoformat(),
                    "phone": endpoint.data["phone"],
                    "recorded_at": at.isoformat(),
                    "request_hash": digest,
                },
                key=key,
                object_id=identity,
                now=at,
            )
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "consent.recorded",
                identity,
                at,
                {"revoked": body["revoked"], "permissions": body["permissions"]},
            )
            return doc(row)
    except HTTPException:
        raise
    except Exception:
        if not journal_started:
            raise
        service.__dict__.setdefault("_journal_faults", set()).add((p["tenant_id"], p["seller_id"]))
        try:
            with service.db.transaction() as s:
                service.db.authorize_stream(s, p)
                seller = service.required(s, "sellers", p["seller_id"], p)
                update(
                    seller,
                    journal_recovery_required=True,
                    journal_expected={**seller.data.get("journal_expected", {}), identity: digest},
                )
        except Exception as exc:
            # The process-local hold remains when storage itself is unavailable.
            logging.getLogger(__name__).error(
                "Journal recovery hold could not persist: %s", type(exc).__name__
            )
        fail(503, "Revocation receipt is incomplete; outbound held until journal recovery is reviewed")
