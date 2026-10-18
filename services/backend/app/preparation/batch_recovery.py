"""Original deferred-request identity and bounded recovery leases, without resubmission."""

import hashlib
import json
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.db import MODELS, audit, doc, uid, update
from app.service import dt, fail

TERMINAL = {"completed", "expired", "cancelled", "failed"}
PROVIDER_STATES = TERMINAL | {"validating", "in_progress", "finalizing", "cancelling"}


class BatchRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    provider_batch_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,200}$")


def fingerprint(requests):
    return hashlib.sha256(json.dumps(requests, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def active_lease(service, row):
    until = dt(row.data.get("lease_until"))
    if not until and row.data.get("status") == "sending" and row.data.get("submitted_at"):
        until = dt(row.data["submitted_at"]) + timedelta(seconds=180)
    return bool(until and until > service.clock())


def admin(service, s, p):
    user = service.required(s, "users", p["user_id"], p)
    if user.data.get("disabled") or user.data.get("role") not in {"admin", "owner"}:
        fail(403, "An active administrator is required for deferred preparation")


def rows(p):
    model = MODELS["command_outbox"]
    return select(model).where(
        model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"], model.op_kind == "script_batch"
    )


def view(service, row):
    data = row.data
    status = "uncertain" if data["status"] == "sending" and not active_lease(service, row) else data["status"]
    return {
        "id": row.id,
        "version": row.version,
        "status": status,
        "provider_batch_id": data.get("provider_batch_id"),
        "lead_count": len(data.get("lead_ids", [])),
        "published": data.get("published", []),
        "retry_lead_ids": data.get("retry_lead_ids", []),
        "can_reconcile": not active_lease(service, row),
        "reason": "Original submission may have been charged. Verify its provider receipt; do not submit it again."
        if status in {"uncertain", "review_required"}
        else data.get("recovery_reason", ""),
    }


def pending_conflict(service, s, p, lead_ids):
    model = MODELS["command_outbox"]
    pending = list(
        s.scalars(rows(p).where(model.op_status.not_in(TERMINAL)).order_by(model.created_at).limit(101))
    )
    if len(pending) > 100:
        fail(409, "Resolve deferred preparation backlog before submitting more work")
    if any(set(lead_ids).intersection(row.data.get("lead_ids", [])) for row in pending):
        fail(409, "These leads already have an unfinished batch; recover the original submission")


def require_owner(service, s, p, intent):
    admin(service, s, p)
    row = service.required(s, "command_outbox", intent["id"], p)
    if row.data.get("lease_owner") != intent["lease_owner"] or not active_lease(service, row):
        fail(409, "Deferred preparation lease changed; reload its current status")
    return row


def claim_recovery(service, p, identity, request):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        admin(service, s, p)
        row = service.required(s, "command_outbox", identity, p)
        if row.data.get("kind") != "script_batch":
            fail(404, "Script batch not found")
        if request and row.version != request.expected_version:
            fail(409, "Deferred preparation changed; reload before checking again")
        if active_lease(service, row):
            fail(409, "Original batch operation is still active")
        known = row.data.get("provider_batch_id")
        supplied = request.provider_batch_id if request else None
        if known and supplied and known != supplied:
            fail(409, "Provider identity differs from the original submission")
        if row.data.get("status") in TERMINAL and "published" in row.data and "retry_lead_ids" in row.data:
            return {**doc(row), "publication_cached": True}
        if not (known or supplied):
            fail(409, "Provide the original provider batch ID to verify this uncertain submission")
        update(row, lease_owner=uid(), lease_until=(service.clock() + timedelta(seconds=180)).isoformat())
        return {**doc(row), "request_key": row.external_key, "recovery_batch_id": known or supplied}


def release_recovery(service, p, intent, reason=None):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        row = service.required(s, "command_outbox", intent["id"], p)
        if row.data.get("lease_owner") == intent["lease_owner"]:
            update(row, lease_owner=None, lease_until=None, **({"recovery_reason": reason} if reason else {}))


def renew_recovery(service, p, intent):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        row = require_owner(service, s, p, intent)
        update(row, lease_until=(service.clock() + timedelta(seconds=180)).isoformat())


def validate_receipt(result, identity=None):
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("id"), str)
        or not result["id"]
        or result.get("status") not in PROVIDER_STATES
        or identity
        and result["id"] != identity
    ):
        fail(409, "Batch response identity or status is invalid")


async def verify_reference(provider, intent, result):
    validate_receipt(result, intent["recovery_batch_id"])
    if intent.get("provider_batch_id"):
        return
    if intent.get("request_digest"):
        expected = {"opendialer_intent": intent["id"], "request_digest": intent["request_digest"]}
        if result.get("metadata") != expected:
            fail(409, "Provider receipt does not identify the original batch request")
        return
    # Legacy requests had no metadata. Verify the random original custom IDs and
    # exact saved input context, not the operator's batch ID alone.
    if not result.get("input_file_id"):
        fail(409, "Original legacy input file is required to verify this provider reference")
    supplied = await provider.input_requests(result["input_file_id"])
    try:
        if len(supplied) != len(intent["contexts"]):
            raise ValueError("Incomplete input file")
        seen = set()
        for row in supplied:
            key, body = row["custom_id"], row["body"]
            if key in seen or key not in intent["contexts"] or row["url"] != "/v1/responses":
                raise ValueError("Unrelated input")
            seen.add(key)
            context = json.loads(next(item["content"] for item in body["input"] if item["role"] == "user"))
            if (
                context != intent["contexts"][key]
                or body["model"] != intent["model"]
                or body["store"] is not False
            ):
                raise ValueError("Unrelated input context")
    except (ValueError, KeyError, TypeError, StopIteration):
        fail(409, "Legacy input file does not match the original request")


def bind_reference(service, p, intent, result):
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        row = require_owner(service, s, p, intent)
        known = row.data.get("provider_batch_id")
        if known and known != result["id"]:
            fail(409, "Original batch identity changed")
        update(row, provider_batch_id=result["id"])
        audit(
            s,
            p["tenant_id"],
            p["seller_id"],
            p["user_id"],
            "batch.original_verified",
            row.id,
            service.clock(),
            {"provider_batch_id": result["id"]},
        )
