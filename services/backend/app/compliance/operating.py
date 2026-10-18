"""No-recurring-spend policy and seller-authorized, cached prepaid screening."""

import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select

from app.db import MODELS, audit, update
from app.preparation.templates import PreparationPolicy, ScriptTemplate, policy_values, template_version
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
    "callbacks": {
        "transfer_per_invocation_usd": 0.1,
        "standard_tts_per_character_usd": 0.000009,
        "basis": "PAYG transfer invocation and basic speech, plus all actual inbound/destination leg minutes; account taxes/rounding may differ",
        "source": "https://telnyx.com/pricing/voice-api",
        "checked_at": "2026-10-18",
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


def authorized_screen(service, config, phone, max_age_days=31):
    auth = config.get("dnc_seller_authorization") or {}
    try:
        return bool(
            auth.get("reference")
            and auth.get("source_access_verified") is True
            and phone[2:5] in auth.get("npas", [])
            and dt(auth["expires_at"]) > service.clock()
            and (
                (service.settings.simulation and not auth.get("source_refreshed_at"))
                or auth.get("source_refreshed_at")
                and 0
                <= (service.clock() - dt(auth["source_refreshed_at"])).total_seconds()
                <= max_age_days * 86400
            )
        )
    except (ValueError, KeyError, TypeError):
        return False


def cached_screen(service, s, p, phone):
    if "provider_config" not in s.info:
        s.info["provider_config"] = service.vault.read(p["tenant_id"])
    config = s.info["provider_config"]
    max_age = screening_max_age(service, s, p)
    if not authorized_screen(service, config, phone, max_age) or (
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
    if not record or record.data.get("phone") != phone:
        return None
    return usable_screen(service, config, record.data, max_age)


def screening_max_age(service, s, p):
    seller = service.required(s, "sellers", p["seller_id"], p)
    return seller.data.get("operating_policy", {}).get("dnc_source_max_age_days", 31)


def screening_expiry(service, config, data, max_age):
    auth = config["dnc_seller_authorization"]
    limits = [dt(auth["expires_at"]), dt(data["checked_at"]) + timedelta(days=max_age)]
    if auth.get("source_refreshed_at"):
        limits.append(dt(auth["source_refreshed_at"]) + timedelta(days=max_age))
    if data.get("expires_at"):
        limits.append(dt(data["expires_at"]))
    return min(limits)


def usable_screen(service, config, data, max_age):
    auth = config.get("dnc_seller_authorization", {})
    try:
        if (
            data.get("status") == "completed"
            and type(data.get("listed")) is bool
            and dt(data["checked_at"]) <= service.clock() + timedelta(seconds=60)
            and data.get("authorization_reference") == auth.get("reference")
            and authorized_screen(service, config, data["phone"], max_age)
            and screening_expiry(service, config, data, max_age) > service.clock()
            and (service.settings.simulation or not data.get("synthetic"))
        ):
            return data
    except (ValueError, TypeError, KeyError):
        pass
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

    from app.compliance.screening import bind_screening_api

    bind_screening_api(app, service, administrator)
