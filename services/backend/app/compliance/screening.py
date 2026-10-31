"""One leased prepaid lookup per phone; uncertain effects require an explicit new charge choice."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.compliance.operating import (
    COSTS,
    ScreeningRequest,
    authority_stamp,
    authorized_screen,
    cached_screen,
    screening_expiry,
    screening_key,
    screening_max_age,
    usable_screen,
)
from app.compliance.phone import normalize_phone
from app.db import MODELS, audit, doc, put, uid, update
from app.providers.dnc import DNCError, PrepaidDNC
from app.providers.inference import InferenceResult
from app.service import dt, fail

UNFINISHED = {"sending", "uncertain", "review_required", "unsubmitted"}
QUERY_ESTIMATE = COSTS["dnc"]["estimated_per_query_usd"]


class ScreeningRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    accept_possible_duplicate_charge: Literal[True]


def active_admin(service, s, p):
    user = service.required(s, "users", p["user_id"], p)
    return not user.data.get("disabled") and user.data.get("role") in {"owner", "admin"}


def lease_active(service, data):
    until = dt(data.get("lease_until"))
    # Legacy sending rows have no lease; a recent submission still cannot overlap.
    if until is None and data.get("status") == "sending" and data.get("started_at"):
        until = dt(data["started_at"]) + timedelta(seconds=30)
    return bool(until and until > service.clock())


def view(service, row, config, max_age):
    data = row.data
    valid = usable_screen(service, config, data, max_age)
    status = data["status"]
    if status == "completed" and not valid:
        status = "expired"
    if status == "sending" and not lease_active(service, data):
        status = "uncertain"
    return {
        "id": row.id,
        "version": row.version,
        "phone": data["phone"],
        "status": status,
        "listed": valid["listed"] if valid else None,
        "checked_at": data.get("checked_at"),
        "expires_at": data.get("expires_at"),
        "can_retry": status in UNFINISHED | {"expired"} and not lease_active(service, data),
        "possible_charge_usd": data.get("estimate_usd", QUERY_ESTIMATE)
        if data.get("request_key") and status != "unsubmitted"
        else 0,
        "previous_attempts": data.get("previous_attempts", 0),
        "reason": data.get("recovery_reason")
        or (
            "Lookup result is unavailable; a previous charge may remain. Another query requires an explicit choice."
            if status in {"uncertain", "review_required"}
            else "Cached result is no longer current; refresh seller authorization and explicitly check again."
            if status == "expired"
            else ""
        ),
    }


def claim(service, p, phone=None, identity=None, retry=None):
    config = service.configs.read(p["tenant_id"])
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        if not active_admin(service, s, p):
            fail(403, "An active administrator is required for prepaid screening")
        model = MODELS["registry_snapshots"]
        row = (
            service.required(s, "registry_snapshots", identity, p)
            if identity
            else s.scalar(
                select(model).where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.external_key == screening_key(service, phone),
                )
            )
        )
        max_age = screening_max_age(service, s, p)
        if retry:
            if row.data.get("kind") != "prepaid":
                fail(404, "Prepaid screening record not found")
            phone = row.data["phone"]
            if row.data.get("retry_idempotency_key") == retry.idempotency_key:
                return None, view(service, row, config, max_age), None
            if row.version != retry.expected_version:
                fail(409, "Screening changed; reload before deciding on another paid query")
        if service.suppressed(s, p, phone):
            if retry:
                fail(403, "INTERNAL_DNC_BLOCKS_LOOKUP")
            return (
                None,
                {"phone": phone, "listed": True, "internal_suppressed": True, "provider": "local_registry"},
                None,
            )
        cached = cached_screen(service, s, p, phone)
        if cached:
            return None, view(service, row, config, max_age) if retry else cached, None
        if row and (
            lease_active(service, row.data)
            or not retry
            and row.data.get("status") in UNFINISHED - {"unsubmitted"}
        ):
            fail(409, "DNC_SCREENING_PENDING_OR_UNCERTAIN_REVIEW_REQUIRED")
        if not config.get("dnc_api_key"):
            fail(409, "DNC_PROVIDER_NOT_CONFIGURED")
        if not authorized_screen(service, config, phone, max_age) or (
            not service.settings.simulation
            and config.get("dnc_seller_authorization", {}).get("seller_id") != p["seller_id"]
        ):
            fail(403, "DNC_SELLER_AUTHORIZATION_REQUIRED")
        key = "dnc:" + uid()
        seller = service.required(s, "sellers", p["seller_id"], p)
        rate = seller.data.get("operating_policy", {}).get("account_rates", {}).get("dnc")
        estimate = float(rate["values"]["estimated_per_query_usd"]) if rate else QUERY_ESTIMATE
        # Retain a legacy/crashed attempt's liability before admitting a new one.
        if row and row.data.get("request_key") and row.data.get("status") in UNFINISHED - {"unsubmitted"}:
            service.usage(
                p,
                "donotcalldnc",
                "dnc_screen",
                InferenceResult({}, {}, None),
                object_id=screening_key(service, phone),
                request_key=row.data["request_key"],
                reserved=row.data.get("estimate_usd", QUERY_ESTIMATE),
                session=s,
            )
        if not service.reserve_budget_in(
            s, p, key, estimate, object_id=screening_key(service, phone), work_kind="dnc_screen"
        ):
            fail(403, "DNC_QUERY_BUDGET_EXHAUSTED")
        values = {
            "kind": "prepaid",
            "estimate_usd": estimate,
            "rate_reference": rate,
            "published": False,
            "phone": phone,
            "status": "sending",
            "request_key": key,
            "started_at": service.clock().isoformat(),
            "lease_owner": uid(),
            "lease_until": (service.clock() + timedelta(seconds=30)).isoformat(),
            "authority_stamp": authority_stamp(config),
            "listed": None,
            "checked_at": None,
            "expires_at": None,
            "recovery_reason": None,
            "previous_attempts": row.data.get("previous_attempts", 0) + 1 if row else 0,
            "retry_idempotency_key": retry.idempotency_key if retry else None,
        }
        if row:
            update(row, **values)
        else:
            row = put(
                s,
                "registry_snapshots",
                p["tenant_id"],
                p["seller_id"],
                values,
                key=screening_key(service, phone),
                now=service.clock(),
            )
        audit(
            s,
            p["tenant_id"],
            p["seller_id"],
            p["user_id"],
            "dnc.query_authorized",
            row.id,
            service.clock(),
            {"request_key": key, "explicit_retry": retry is not None},
        )
        return doc(row), None, config


def query_charge(service, s, p, intent, result):
    """Estimated charge of one definitive answer, from the credit balance when it is unambiguous.

    With two lookups in flight a balance can move by two; larger moves include use outside this
    seller, so the configured per-query price is used instead. Never estimates below one credit.
    """
    rate = Decimal(str(intent.get("estimate_usd", QUERY_ESTIMATE)))
    credits, source = 1, "configured_per_query_rate"
    if result:
        seller = service.required(s, "sellers", p["seller_id"], p)
        prior = seller.data.get("dnc_credit_observation") or {}
        current = result["credits_remaining"]
        if type(prior.get("credits")) is int and 1 <= prior["credits"] - current <= 2:
            credits, source = prior["credits"] - current, "credits_remaining_delta"
        if not prior.get("checked_at") or prior["checked_at"] <= result["checked_at"]:
            update(seller, dnc_credit_observation={"credits": current, "checked_at": result["checked_at"]})
    return float(rate * credits), source


def finish(service, p, intent, result, refusal=None):
    # Billing is recorded even when current authority/source versions reject publication.
    if refusal in {"DNC_PROVIDER_KEY_INVALID", "DNC_PROVIDER_CREDITS_EXHAUSTED"}:
        # The provider refused before any lookup: there is no charge to hold.
        service.release_refused_budget(p, intent["request_key"], refusal)
    elif result or refusal:
        with service.db.transaction() as s:
            service.db.authorize_stream(s, p)
            cost, source = query_charge(service, s, p, intent, result)
            service.settle_estimate_in(s, p, intent["request_key"], cost, source)
    else:
        service.usage(
            p,
            "donotcalldnc",
            "dnc_screen",
            InferenceResult({}, {}, None),
            object_id=screening_key(service, intent["phone"]),
            request_key=intent["request_key"],
            reserved=intent.get("estimate_usd", QUERY_ESTIMATE),
        )
    config = service.configs.read(p["tenant_id"])
    rejected = False
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)
        row = service.required(s, "registry_snapshots", intent["id"], p)
        if (
            row.version != intent["version"]
            or row.data.get("request_key") != intent["request_key"]
            or row.data.get("lease_owner") != intent["lease_owner"]
        ):
            fail(409, "A newer screening attempt owns this result; original charge retained")
        max_age = screening_max_age(service, s, p)
        rejected = not (
            active_admin(service, s, p)
            and lease_active(service, row.data)
            and authority_stamp(config) == intent["authority_stamp"]
            and authorized_screen(service, config, intent["phone"], max_age)
            and not service.suppressed(s, p, intent["phone"])
        )
        if result and not rejected:
            update(
                row,
                **result,
                status="completed",
                authorization_reference=config["dnc_seller_authorization"]["reference"],
                expires_at=screening_expiry(service, config, result, max_age).isoformat(),
                synthetic=service.settings.simulation,
                lease_owner=None,
                lease_until=None,
            )
        elif refusal and not rejected:
            update(
                row, status="failed", listed=None, lease_owner=None, lease_until=None, recovery_reason=refusal
            )
        else:
            update(
                row,
                status="review_required" if rejected else "uncertain",
                listed=None,
                lease_owner=None,
                lease_until=None,
                recovery_reason="Authority, source or internal opt-out changed; no clearance published. Prior charge may remain."
                if rejected
                else "Lookup result was lost or invalid; a previous charge may remain. Another query requires an explicit choice.",
            )
        saved = view(service, row, config, max_age)
    if rejected:
        fail(409, "DNC_AUTHORITY_CHANGED_NO_CLEARANCE")
    return saved


async def execute(service, p, intent, config):
    result = None
    try:
        async with asyncio.timeout(10):
            value = await PrepaidDNC(
                config["dnc_api_key"], http=getattr(service, "http", None), clock=service.clock
            ).check(intent["phone"])
        at = dt(value["checked_at"])
        if (
            value.get("phone") != intent["phone"]
            or type(value.get("listed")) is not bool
            or value.get("provider") != "donotcalldnc"
            or type(value.get("credits_remaining")) is not int
            or value["credits_remaining"] < 0
            or not at
            or not -60 <= (service.clock() - at).total_seconds() <= 300
        ):
            raise DNCError("Invalid original lookup receipt")
        result = {
            key: value[key] for key in ("phone", "listed", "checked_at", "provider", "credits_remaining")
        }
        result["source_authorization_required"] = True
    except asyncio.CancelledError:
        await asyncio.shield(asyncio.to_thread(finish, service, p, intent, None))
        raise
    except DNCError as exc:
        # A definitive refusal is an answer, not an outage: never cleared, never retried as-is.
        refusal = exc.reason if exc.definitive else None
        await asyncio.to_thread(finish, service, p, intent, None, refusal)
        fail(409 if refusal else 503, refusal or "DNC_SCREENING_UNAVAILABLE_NO_CLEARANCE")
    except Exception:
        # A transport/adapter failure is not proof that the provider did not charge.
        # Store only the generic recovery state, never the raw response/exception.
        result = None
    saved = await asyncio.to_thread(finish, service, p, intent, result)
    if result is None:
        fail(503, "DNC_SCREENING_UNAVAILABLE_NO_CLEARANCE")
    return saved


def bind_screening_api(app, service, administrator):
    @app.get("/v1/admin/dnc/screens")
    def screens(p=Depends(administrator)):
        config = service.configs.read(p["tenant_id"])
        with service.db.transaction(False) as s:
            model = MODELS["registry_snapshots"]
            base = select(model).where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.ctx_key == "prepaid",
            )
            pending = list(
                s.scalars(
                    base.where(model.ctx_status.in_(UNFINISHED))
                    .order_by(model.created_at, model.id)
                    .limit(100)
                )
            )
            recent = list(
                s.scalars(
                    base.where(model.ctx_status == "completed")
                    .order_by(model.created_at.desc(), model.id)
                    .limit(20)
                )
            )
            max_age = screening_max_age(service, s, p)
            return {
                "screens": [view(service, row, config, max_age) for row in pending + recent],
                "limited": len(pending) == 100,
            }

    @app.post("/v1/admin/dnc/screen")
    async def screen(data: ScreeningRequest, p=Depends(administrator)):
        phones = list(dict.fromkeys(normalize_phone(value).e164 for value in data.phones))
        if any(not phone or not phone.startswith("+1") for phone in phones):
            fail(422, "Valid US phone numbers are required")
        results = []
        for phone in phones:
            intent, cached, config = await asyncio.to_thread(claim, service, p, phone)
            if cached is not None:
                results.append({**cached, "cached": True})
            else:
                results.append({**await execute(service, p, intent, config), "cached": False})
        return {"results": results, "authorization_required": True}

    @app.post("/v1/admin/dnc/screens/{identity}/retry")
    async def retry(identity: str, data: ScreeningRetry, p=Depends(administrator)):
        intent, cached, config = await asyncio.to_thread(claim, service, p, identity=identity, retry=data)
        return cached if cached is not None else await execute(service, p, intent, config)
