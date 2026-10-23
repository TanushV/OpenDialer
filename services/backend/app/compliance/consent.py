"""Scoped consent evidence; a revocation immediately stops the permissions it names."""

import hashlib
import json
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import select

from app.db import MODELS, audit, doc, emit, put, update
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
            service.stop_agent_transcription_in(s, p, call)
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


def record(service, p, body):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        user = service.required(s, "users", p["user_id"], p)
        if user.data.get("disabled") or user.data.get("role") not in {"owner", "admin"}:
            fail(403, "Current administrator authority is required")
        endpoint = service.required(s, "phone_endpoints", body["endpoint_id"], p)
        if body.get("acquired_at") and dt(body["acquired_at"]) > service.clock():
            fail(422, "Permission evidence cannot come from a future time")
        key, digest = "consent:" + body["idempotency_key"], request_hash(body)
        prior = service._by_key(s, p, "consent_evidence", key)
        if prior:
            if prior.data.get("request_hash") != digest:
                fail(409, "Consent request identity already describes different evidence")
            return doc(prior)
        identity = str(
            uuid5(
                NAMESPACE_URL,
                json.dumps(["opendialer-consent", p["tenant_id"], p["seller_id"], body["idempotency_key"]]),
            )
        )
        at = service.clock()
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
