"""No-recurring-spend policy and seller-authorized, cached prepaid screening."""

import asyncio
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select

from app.db import MODELS, audit, put, uid, update
from app.preparation.templates import PreparationPolicy, ScriptTemplate, policy_values, template_version
from app.providers.dnc import DNCError, PrepaidDNC
from app.providers.inference import InferenceResult
from app.service import dt, fail


class OperatingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subscriptions_allowed: Literal[False] = False
    recording_enabled: Literal[False] = False
    raw_retention_seconds: Literal[300] = 300
    inference_data_collection: Literal["deny"] = "deny"
    inference_zdr: Literal[True] = True
    auto_coach_interval_seconds: int = Field(default=8, ge=3, le=60)
    script_template: ScriptTemplate | None = None
    preparation: PreparationPolicy = Field(default_factory=PreparationPolicy)
    transcription_privacy_verified: bool = False
    transcription_privacy_reference: str = Field(default="", max_length=1000)
    dnc_source_max_age_days: int = Field(default=31, ge=1, le=31)


class ScreeningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phones: list[str] = Field(min_length=1, max_length=500)


class DNCAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str = Field(min_length=1, max_length=200)
    npas: list[str] = Field(min_length=1, max_length=1000)
    expires_at: str
    source_access_verified: bool
    source_refreshed_at: str | None = None

    @field_validator("reference")
    @classmethod
    def reference_not_blank(cls, value):
        if not value.strip():
            raise ValueError("Seller access reference is required")
        return value.strip()

    @field_validator("npas")
    @classmethod
    def valid_area_codes(cls, values):
        if any(len(v) != 3 or v[0] not in "23456789" or not v.isascii() or not v.isdigit() for v in values):
            raise ValueError("Use three-digit North American area codes")
        return sorted(set(values))

    @field_validator("expires_at", "source_refreshed_at")
    @classmethod
    def zoned_timestamp(cls, value):
        if value is None:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("A timestamp with UTC offset is required")
        return parsed.isoformat()


COSTS = {
    "application": {"subscription": False, "cost_usd": 0},
    "voice": {
        "basis": "PAYG, all actual legs plus termination and carrier fees",
        "api_per_minute_usd": 0.002,
    },
    "number_rental": {
        "recurring": True,
        "new_commitments_allowed": False,
        "from_monthly_usd": 1,
        "existing_obligations": "Remain payable to the carrier; no automatic release",
    },
    "dnc": {
        "prepaid_minimum_usd": 100,
        "credits": 60000,
        "estimated_per_query_usd": 100 / 60000,
        "recurring": False,
        "seller_registry_access_separate": True,
    },
    "cerebras": {
        "basis": "OpenRouter PAYG endpoint plus platform fees",
        "input_per_million_usd": 0.35,
        "output_per_million_usd": 0.75,
        "platform_fee_fraction": 0.055,
    },
    "stt": {
        "soniox_per_minute_usd": 0.002,
        "parakeet_per_minute_usd": 0.0015,
        "basis": "Verify selected engine, tracks and rounding",
    },
    "external_reputation": {"allowed": False, "excluded_monthly_usd": 100},
    "recording_calendar_crm": {"enabled": False, "cost_usd": 0},
}


def screening_key(service, phone):
    return "prepaid:" + hmac.new(service.db.identity_key, phone.encode(), hashlib.sha256).hexdigest()


def authorized_screen(service, config, phone):
    auth = config.get("dnc_seller_authorization") or {}
    try:
        return bool(
            auth.get("reference")
            and auth.get("source_access_verified") is True
            and phone[2:5] in auth.get("npas", [])
            and dt(auth["expires_at"]) > service.clock()
            and (
                service.settings.simulation
                or auth.get("source_refreshed_at")
                and 0 <= (service.clock() - dt(auth["source_refreshed_at"])).total_seconds() <= 31 * 86400
            )
        )
    except (ValueError, KeyError, TypeError):
        return False


def cached_screen(service, s, p, phone):
    if "provider_config" not in s.info:
        s.info["provider_config"] = service.vault.read(p["tenant_id"])
    config = s.info["provider_config"]
    if not authorized_screen(service, config, phone) or (
        not service.settings.simulation
        and config.get("dnc_seller_authorization", {}).get("seller_id") != p["seller_id"]
    ):
        return None
    model = MODELS["registry_snapshots"]
    key = screening_key(service, phone)
    cache = s.info.get("row_cache", {}).get(("registry_snapshots", p["tenant_id"], p["seller_id"]))
    record = (
        next((r for r in cache if r.external_key == key), None)
        if cache is not None
        else s.scalar(
            select(model).where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.external_key == key,
            )
        )
    )
    if not record:
        return None
    data = record.data
    if (
        data.get("phone") == phone
        and data.get("status") == "completed"
        and data.get("authorization_reference") == config["dnc_seller_authorization"]["reference"]
        and dt(data["expires_at"]) > service.clock()
        and (service.settings.simulation or not data.get("synthetic"))
    ):
        return data
    return None


def bind_operating_api(app, service, administrator):
    @app.get("/v1/admin/operating-policy")
    def get_policy(p=Depends(administrator)):
        with service.db.transaction(False) as s:
            seller = service.required(s, "sellers", p["seller_id"], p)
            policy = OperatingPolicy(
                **{
                    k: v
                    for k, v in policy_values(seller.data.get("operating_policy", {})).items()
                    if k in OperatingPolicy.model_fields
                }
            ).model_dump()
        return {
            **policy,
            "template_version": template_version(policy["script_template"]),
            "template_defaults": ScriptTemplate().model_dump(),
            "coach_provider": service.vault.read(p["tenant_id"]).get("coach_provider", "openrouter"),
            "external_reputation": {
                "available": False,
                "status": "subscription_excluded",
                "observed_health_available": True,
            },
            "dnc": {
                "authorization_required": True,
                "free_prospect_area_codes": 5,
                "prepaid_supported": True,
                "notice": "Prepaid checks do not waive seller-specific National Registry access.",
            },
            "costs": COSTS,
            "rates_checked_at": "2026-10-17",
            "rates_are_estimates": True,
        }

    @app.put("/v1/admin/operating-policy")
    def put_policy(data: OperatingPolicy, p=Depends(administrator)):
        with service.db.transaction() as s:
            seller = service.required(s, "sellers", p["seller_id"], p)
            current = {
                k: v
                for k, v in policy_values(seller.data.get("operating_policy", {})).items()
                if k in OperatingPolicy.model_fields
            }
            try:
                incoming = data.model_dump(exclude_unset=True)
                if "preparation" in incoming:
                    incoming["preparation"] = {**current.get("preparation", {}), **incoming["preparation"]}
                merged = OperatingPolicy(**{**current, **incoming})
            except ValidationError:
                fail(422, "Valid operating settings and an IANA preparation timezone are required")
            if merged.transcription_privacy_verified and not merged.transcription_privacy_reference.strip():
                fail(422, "Provider/account retention evidence is required")
            values = merged.model_dump()
            update(seller, operating_policy=values)
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "operating_policy.updated",
                seller.id,
                service.clock(),
                {"fields": sorted(values)},
            )
        return get_policy(p)

    @app.post("/v1/admin/dnc/screen")
    async def screen(data: ScreeningRequest, p=Depends(administrator)):
        from app.compliance.phone import normalize_phone

        config = service.vault.read(p["tenant_id"])
        if not config.get("dnc_api_key"):
            fail(409, "DNC_PROVIDER_NOT_CONFIGURED")
        phones = list(dict.fromkeys(normalize_phone(value).e164 for value in data.phones))
        if any(not phone or not phone.startswith("+1") for phone in phones):
            fail(422, "Valid US phone numbers are required")
        if any(not authorized_screen(service, config, phone) for phone in phones):
            fail(403, "DNC_SELLER_AUTHORIZATION_REQUIRED")
        if (
            not service.settings.simulation
            and config.get("dnc_seller_authorization", {}).get("seller_id") != p["seller_id"]
        ):
            fail(403, "DNC_SELLER_AUTHORIZATION_REQUIRED")
        provider = PrepaidDNC(
            config["dnc_api_key"], http=getattr(service, "coach_http", None), clock=service.clock
        )
        results = []
        for phone in phones:

            def claim(phone=phone):
                with service.db.transaction() as s:
                    service.db.authorize_stream(s, p)
                    if service.suppressed(s, p, phone):
                        return None, {
                            "phone": phone,
                            "listed": True,
                            "internal_suppressed": True,
                            "provider": "local_registry",
                        }
                    cached = cached_screen(service, s, p, phone)
                    if cached:
                        return None, cached
                    model = MODELS["registry_snapshots"]
                    row = s.scalar(
                        select(model).where(
                            model.tenant_id == p["tenant_id"],
                            model.seller_id == p["seller_id"],
                            model.external_key == screening_key(service, phone),
                        )
                    )
                    if row and row.data.get("status") in {"sending", "uncertain"}:
                        fail(409, "DNC_SCREENING_PENDING_OR_UNCERTAIN_REVIEW_REQUIRED")
                    key = "dnc:" + uid()
                    values = {
                        "kind": "prepaid",
                        "published": False,
                        "phone": phone,
                        "status": "sending",
                        "request_key": key,
                        "started_at": service.clock().isoformat(),
                    }
                    if row:
                        update(row, **values)
                    else:
                        put(
                            s,
                            "registry_snapshots",
                            p["tenant_id"],
                            p["seller_id"],
                            values,
                            key=screening_key(service, phone),
                            now=service.clock(),
                        )
                    return key, None

            key, cached = await asyncio.to_thread(claim)
            if cached:
                results.append({**cached, "cached": True})
                continue

            def mark(status, phone=phone):
                with service.db.transaction() as s:
                    model = MODELS["registry_snapshots"]
                    row = s.scalar(
                        select(model).where(
                            model.tenant_id == p["tenant_id"],
                            model.seller_id == p["seller_id"],
                            model.external_key == screening_key(service, phone),
                        )
                    )
                    update(row, status=status)

            admitted = await asyncio.to_thread(
                service.reserve_optional_budget,
                p,
                key,
                100 / 60000,
                object_id=screening_key(service, phone),
                work_kind="dnc_screen",
            )
            if not admitted:
                await asyncio.to_thread(mark, "unsubmitted")
                fail(403, "DNC_QUERY_BUDGET_EXHAUSTED")
            try:
                result = await provider.check(phone)
            except DNCError:
                # An uncertain provider request is never automatically repeated or cleared.
                await asyncio.to_thread(mark, "uncertain")
                await asyncio.to_thread(
                    service.usage,
                    p,
                    "donotcalldnc",
                    "dnc_screen",
                    InferenceResult({}, {}, None),
                    object_id=screening_key(service, phone),
                    request_key=key,
                    reserved=0,
                )
                fail(503, "DNC_SCREENING_UNAVAILABLE_NO_CLEARANCE")

            def save(phone=phone, result=result, key=key):
                with service.db.transaction() as s:
                    auth = config["dnc_seller_authorization"]
                    expires = min(dt(auth["expires_at"]), service.clock() + timedelta(days=31))
                    values = {
                        **result,
                        "kind": "prepaid",
                        "status": "completed",
                        "published": False,
                        "authorization_reference": auth["reference"],
                        "expires_at": expires.isoformat(),
                        "synthetic": service.settings.simulation,
                    }
                    model = MODELS["registry_snapshots"]
                    row = s.scalar(
                        select(model).where(
                            model.tenant_id == p["tenant_id"],
                            model.seller_id == p["seller_id"],
                            model.external_key == screening_key(service, phone),
                        )
                    )
                    if row:
                        update(row, **values)
                    else:
                        put(
                            s,
                            "registry_snapshots",
                            p["tenant_id"],
                            p["seller_id"],
                            values,
                            key=screening_key(service, phone),
                            now=service.clock(),
                        )
                # The API gives remaining credits, not a monetary billing receipt: retain estimated liability.
                service.usage(
                    p,
                    "donotcalldnc",
                    "dnc_screen",
                    InferenceResult({}, {"credits_remaining": result.get("credits_remaining")}, key),
                    object_id=screening_key(service, phone),
                    request_key=key,
                    reserved=0,
                )

            await asyncio.to_thread(save)
            results.append({**result, "cached": False})
        return {"results": results, "authorization_required": True}
