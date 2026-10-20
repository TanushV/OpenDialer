"""No-recurring-spend policy and seller-authorized, cached prepaid screening."""

import hashlib
import hmac
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select

from app.config import PROVIDER_DEFAULTS
from app.db import MODELS, audit, update
from app.preparation.templates import PreparationPolicy, ScriptTemplate, policy_values, template_version
from app.service import dt, fail


class DncRefreshPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    interval_hours: int = Field(default=24, ge=24, le=168)
    batch_size: int = Field(default=500, ge=1, le=500)
    max_run_usd: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=6)
    max_day_usd: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=6)

    @model_validator(mode="after")
    def explicit_funding(self):
        if self.enabled and (self.max_run_usd <= 0 or self.max_day_usd <= 0):
            raise ValueError("Enabling prepaid refresh requires explicit positive run and daily budgets")
        return self


class AccountRate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=9)]] = Field(
        min_length=1, max_length=8
    )
    source_reference: str = Field(min_length=1, max_length=1000)
    checked_at: date

    @field_validator("source_reference")
    @classmethod
    def source_not_blank(cls, value):
        if not value.strip():
            raise ValueError("An account rate needs an invoice or quote reference")
        return value.strip()


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
    dnc_refresh: DncRefreshPolicy = Field(default_factory=DncRefreshPolicy)
    account_rates: dict[str, AccountRate] = Field(default_factory=dict, max_length=20)

    @model_validator(mode="after")
    def known_rate_units(self):
        for component, rate in self.account_rates.items():
            if component not in RATE_FIELDS or not set(rate.values) <= set(RATE_FIELDS[component]):
                raise ValueError("Use an advertised component and rate unit")
            if any(value < RATE_MINIMUMS.get(component, 0) for value in rate.values.values()):
                raise ValueError(
                    "Prepaid query admission needs a positive estimate; settle zero charges by receipt"
                )
        return self


class OperatingEdit(OperatingPolicy):
    request_id: str | None = None
    expected_version: int | None = Field(default=None, ge=1)


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
    "lookup": {
        "per_query_usd": None,
        "basis": "Optional PAYG endpoint/carrier lookup; quote required; not spam-label evidence",
    },
    "amd": {
        "per_call_usd": None,
        "basis": "Optional PAYG machine detection; account rate and receipt required",
    },
    "search": {
        "per_tool_request_usd": None,
        "basis": "Optional metered search plus model tokens; exact route rate required",
    },
    "preparation": {
        "input_per_million_usd": None,
        "output_per_million_usd": None,
        "basis": "Configured PAYG model and optional review; no assumed free inference",
    },
    "finalization": {
        "input_per_million_usd": 0.35,
        "output_per_million_usd": 0.75,
        "basis": "Default Cerebras PAYG model plus optional selective review and platform fees",
    },
}

RATE_FIELDS = {
    "voice": ["api_per_minute_usd"],
    "callbacks": ["transfer_per_invocation_usd", "standard_tts_per_character_usd"],
    "number_rental": ["from_monthly_usd"],
    "dnc": ["estimated_per_query_usd"],
    "cerebras": ["input_per_million_usd", "output_per_million_usd"],
    "stt": ["soniox_per_minute_usd", "parakeet_per_minute_usd"],
    "lookup": ["per_query_usd"],
    "amd": ["per_call_usd"],
    "search": ["per_tool_request_usd"],
    "preparation": ["input_per_million_usd", "output_per_million_usd"],
    "finalization": ["input_per_million_usd", "output_per_million_usd"],
}
RATE_MINIMUMS = {"dnc": Decimal("0.000000001")}

# Dated reference rates, not actual invoices. Existing aliases remain for ledger consumers.
for _name, _unit, _classification, _url, _funding in (
    ("application", "local application", "included", "https://github.com/TanushV/OpenDialer", 0),
    ("voice", "actual leg minute", "payg", "https://telnyx.com/pricing/voice-api", 0),
    ("callbacks", "transfer invocation / TTS character", "payg", "https://telnyx.com/pricing/voice-api", 0),
    ("number_rental", "owned number month", "existing_recurring", "https://telnyx.com/pricing/numbers", 0),
    ("dnc", "lookup credit", "payg", "https://www.donotcalldnc.com/api/prepaid-pricing/", 100),
    ("cerebras", "million input / output tokens", "payg", "https://openrouter.ai/openai/gpt-oss-120b", 0),
    ("stt", "actual engine track minute", "payg", "https://telnyx.com/pricing/voice-api", 0),
    (
        "external_reputation",
        "monthly enrollment plus checks",
        "excluded",
        "https://developers.telnyx.com/docs/number-reputation/pricing",
        0,
    ),
    ("recording_calendar_crm", "disabled operation", "excluded", "https://github.com/TanushV/OpenDialer", 0),
    ("lookup", "lookup request", "payg", "https://telnyx.com/pricing/number-lookup", 0),
    ("amd", "detection per call", "payg", "https://telnyx.com/pricing/voice-api", 0),
    ("search", "search tool request plus model tokens", "payg", "https://openai.com/api/pricing/", 0),
    ("preparation", "million input / output tokens", "payg", "https://openai.com/api/pricing/", 0),
    ("finalization", "million input / output tokens", "payg", "https://openrouter.ai/openai/gpt-oss-120b", 0),
):
    COSTS[_name].update(
        currency="USD",
        unit=_unit,
        classification=_classification,
        source_url=_url,
        checked_at=COSTS[_name].get("checked_at", "2026-10-17"),
        account_rate_override=None,
        minimum_funding_usd=_funding if _name in {"application", "dnc", "recording_calendar_crm"} else None,
        funding_status="reference"
        if _name == "dnc"
        else "not_applicable"
        if _name in {"application", "recording_calendar_crm"}
        else "account_dependent",
    )


def cost_register(policy):
    return {
        name: {
            **rate,
            "rate_fields": RATE_FIELDS.get(name, []),
            "minimum_rate": str(RATE_MINIMUMS.get(name, 0)),
            "account_rate_override": policy.get("account_rates", {}).get(name),
        }
        for name, rate in COSTS.items()
    }


def authority_stamp(config):
    return hashlib.sha256(
        json.dumps(config.get("dnc_seller_authorization", {}), sort_keys=True).encode()
    ).hexdigest()


def source_expiry(data, now, max_age, timestamp="refreshed_at"):
    """Original source/access deadlines, shared by required state and national lists."""
    try:
        at = dt(data[timestamp])
        if (
            not at
            or at.tzinfo is None
            or at > now + timedelta(seconds=60 if timestamp == "checked_at" else 0)
        ):
            return None
        return min(
            [at + timedelta(days=min(31, max(1, max_age)))]
            + [dt(data[k]) for k in ("expires_at", "authorization_expires_at") if data.get(k)]
        )
    except (ValueError, TypeError, KeyError):
        return None


def screening_capability(snapshot, endpoint=None):
    """One charge-free national decision over a scoped, already-read snapshot."""
    now, simulation = snapshot["now"], snapshot.get("simulation", False)
    max_age = min(31, max(1, snapshot.get("max_age_days", 31)))
    auth, national = snapshot.get("authorization", {}), snapshot.get("national", [])
    configured = bool(national or snapshot.get("prepaid_configured"))
    mode = (
        "combined"
        if national and snapshot.get("prepaid_configured")
        else "snapshot"
        if national
        else "prepaid"
        if snapshot.get("prepaid_configured")
        else "unconfigured"
    )
    base = {
        "endpoint": endpoint,
        "mode": mode,
        "configured": configured,
        "authorized": False,
        "fresh": False,
        "covered": False,
        "decision": "pending",
        "reason_codes": [],
        "sources": [],
        "expires_at": None,
    }
    if snapshot.get("internal_suppressed"):
        return {**base, "decision": "blocked", "reason_codes": ["INTERNAL_SUPPRESSION"]}

    def expiry(data, timestamp):
        return source_expiry(data, now, max_age, timestamp)

    sources, stale, covered, invalid = [], False, False, False
    for data in national:
        in_scope = endpoint is None or endpoint[2:5] in data.get("npas", [])
        if data.get("source_invalid"):
            invalid |= in_scope  # An unreadable covering source cannot be outvoted by another.
            continue
        valid = (simulation or not data.get("synthetic")) and bool(
            data.get("subscription_reference") or data.get("subscription_id")
        )
        base["authorized"] |= bool(valid)
        covered |= bool(valid and in_scope)
        end = expiry(data, "refreshed_at")
        if valid and in_scope:
            if end and now < end:
                sources.append(
                    {
                        "kind": "snapshot",
                        "id": data.get("id"),
                        "version": data.get("version"),
                        "expires_at": end,
                        "listed": data.get("listed", False),
                        "checked_at": data["refreshed_at"],
                    }
                )
            else:
                stale = True
    auth_valid = bool(
        auth.get("reference")
        and auth.get("source_access_verified") is True
        and (simulation or auth.get("seller_id") == snapshot.get("seller_id"))
    )
    auth_end = expiry(auth, "source_refreshed_at")
    if simulation and not auth.get("source_refreshed_at"):
        try:
            auth_end = dt(auth.get("expires_at"))
        except (ValueError, TypeError):
            auth_end = None
    auth_scope = endpoint is None or endpoint[2:5] in auth.get("npas", [])
    prepaid_authorized = bool(auth_valid and auth_end and now < auth_end)
    base["authorized"] |= prepaid_authorized
    covered |= bool(prepaid_authorized and auth_scope)
    cached = snapshot.get("prepaid") or {}
    result_end = expiry(cached, "checked_at")
    usable = bool(
        endpoint
        and cached.get("phone") == endpoint
        and cached.get("status") == "completed"
        and type(cached.get("listed")) is bool
        and result_end
        and now < result_end
        and cached.get("authority_stamp") == authority_stamp({"dnc_seller_authorization": auth})
        and cached.get("authorization_reference") == auth.get("reference")
        and prepaid_authorized
        and auth_scope
        and (simulation or not cached.get("synthetic"))
    )
    if usable:
        sources.append(
            {
                "kind": "prepaid",
                "id": cached.get("id"),
                "version": cached.get("version"),
                "expires_at": min(auth_end, result_end),
                "listed": cached["listed"],
                "checked_at": cached["checked_at"],
            }
        )
    base["covered"] = covered
    base["fresh"] = bool(sources or endpoint is None and prepaid_authorized)
    # A listing anywhere still lists; otherwise an invalid covering source denies clearance.
    if sources and (not invalid or any(x["listed"] for x in sources)):
        base.update(
            decision="listed" if any(x["listed"] for x in sources) else "clear",
            sources=[{**x, "expires_at": x["expires_at"].isoformat()} for x in sources],
            expires_at=min(x["expires_at"] for x in sources).isoformat(),
        )
        if base["decision"] == "listed":
            base["reason_codes"] = ["REGISTRY_LISTED"]
    elif endpoint is None and prepaid_authorized and not invalid:
        base.update(decision="available", expires_at=auth_end.isoformat())
    else:
        base["reason_codes"] = [
            "REGISTRY_SOURCE_INVALID"
            if invalid
            else "REGISTRY_STALE"
            if stale
            else "SCREENING_PENDING"
            if prepaid_authorized and auth_scope
            else "REGISTRY_COVERAGE_MISSING"
            if configured
            else "REGISTRY_MISSING"
        ]
    return base


def screening_snapshot(service, s, p, phone=None, *, internal_suppressed=None):
    from app.compliance.registry import published_snapshots, registry_contains

    if internal_suppressed is None:
        internal_suppressed = bool(phone and service.suppressed(s, p, phone))
    base = {
        "now": service.clock(),
        "simulation": service.settings.simulation,
        "seller_id": p["seller_id"],
        "internal_suppressed": internal_suppressed,
    }
    if internal_suppressed:
        return base
    if "provider_config" not in s.info:
        s.info["provider_config"] = service.vault.read(p["tenant_id"])
    config = s.info["provider_config"]
    national = []
    for row in published_snapshots(s, p):
        if row.data.get("kind") == "national":
            item = {**row.data, "id": row.id, "version": row.version}
            if phone and phone[2:5] in item.get("npas", []):
                try:
                    item["listed"] = registry_contains(s, row, phone)
                except ValueError:
                    item["source_invalid"] = True
            national.append(item)
    cached = cached_screen(service, s, p, phone) if phone else None
    return {
        **base,
        "max_age_days": screening_max_age(service, s, p),
        "authorization": config.get("dnc_seller_authorization", {}),
        "prepaid_configured": bool(config.get("dnc_api_key")),
        "national": national,
        "prepaid": cached,
    }


def screening_key(service, phone):
    return "prepaid:" + hmac.new(service.db.identity_key, phone.encode(), hashlib.sha256).hexdigest()


def screening_page(service, s, p, phones, denied):
    """Charge-free, bounded planning snapshot using the same per-endpoint decision."""
    from app.compliance.registry import published_snapshots, registry_contains

    if len(phones) > 500:
        raise ValueError("At most500 endpoints per screening snapshot")
    base = screening_snapshot(service, s, p)
    publications = {r.id: r for r in published_snapshots(s, p)}
    keys = [screening_key(service, phone) for phone in phones if phone not in denied]
    model = MODELS["registry_snapshots"]
    prepaid = (
        {
            row.data.get("phone"): {**row.data, "id": row.id, "version": row.version}
            for row in s.scalars(
                select(model).where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.external_key.in_(keys),
                )
            )
        }
        if keys
        else {}
    )
    result = {}
    for phone in phones:
        national = []
        if phone not in denied:
            for source in base["national"]:
                item = dict(source)
                if phone[2:5] in item.get("npas", []):
                    try:
                        item["listed"] = registry_contains(s, publications[item["id"]], phone)
                    except ValueError:
                        item["source_invalid"] = True
                national.append(item)
        result[phone] = screening_capability(
            {
                **base,
                "internal_suppressed": phone in denied,
                "national": national,
                "prepaid": prepaid.get(phone),
            },
            phone,
        )
    return result


def prepaid_snapshot(service, config, max_age, data=None):
    auth = config.get("dnc_seller_authorization") or {}
    return {
        "now": service.clock(),
        "simulation": service.settings.simulation,
        "seller_id": auth.get("seller_id"),
        "authorization": auth,
        "max_age_days": max_age,
        "prepaid_configured": bool(config.get("dnc_api_key")),
        "prepaid": data,
    }


def authorized_screen(service, config, phone, max_age_days=31):
    decision = screening_capability(prepaid_snapshot(service, config, max_age_days), phone)
    return decision["authorized"] and decision["covered"]


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
    decision = screening_capability(prepaid_snapshot(service, config, max_age, data), data.get("phone"))
    return data if any(source["kind"] == "prepaid" for source in decision["sources"]) else None


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
            ).model_dump(mode="json")
            version = seller.data.get("operating_policy_version", 1)
        return {
            **policy,
            "version": version,
            "template_version": template_version(policy["script_template"]),
            "template_defaults": ScriptTemplate().model_dump(),
            "coach_provider": service.vault.read(p["tenant_id"]).get(
                "coach_provider", PROVIDER_DEFAULTS.coach_provider
            ),
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
            "costs": cost_register(policy),
            "rates_checked_at": "2026-10-17",
            "rates_are_estimates": True,
        }

    @app.put("/v1/admin/operating-policy")
    def put_policy(data: OperatingEdit, p=Depends(administrator)):
        with service.db.transaction() as s:
            seller = service.required(s, "sellers", p["seller_id"], p)
            incoming = data.model_dump(mode="json", exclude_unset=True)
            identity = service.local_edit_in(
                s, p, "operating_policy", seller.id, seller, incoming, "owner_id"
            )
            version = seller.data.get("operating_policy_version", 1)
            if identity[2]:
                return {**get_policy(p), "edit_receipt": identity[2]}
            if data.expected_version != version:
                fail(
                    409,
                    {
                        "code": "version_conflict",
                        "current_version": version,
                        "message": "Operating policy changed; reload before saving",
                    },
                )
            incoming.pop("request_id", None)
            incoming.pop("expected_version", None)
            current = {
                k: v
                for k, v in policy_values(seller.data.get("operating_policy", {})).items()
                if k in OperatingPolicy.model_fields
            }
            try:
                for field in ("preparation", "dnc_refresh"):
                    if field in incoming:
                        incoming[field] = {**current.get(field, {}), **incoming[field]}
                merged = OperatingPolicy(**{**current, **incoming})
            except ValidationError:
                fail(422, "Valid operating settings and an IANA preparation timezone are required")
            if merged.transcription_privacy_verified and not merged.transcription_privacy_reference.strip():
                fail(422, "Provider/account retention evidence is required")
            if any(rate.checked_at > service.clock().date() for rate in merged.account_rates.values()):
                fail(422, "An account rate cannot have a future verification date")
            values = merged.model_dump(mode="json")
            asset = (values.get("script_template") or {}).get("final_voicemail_asset")
            if asset and not any(
                asset
                in service.required(s, "policy_versions", campaign.data["policy_id"], p).data.get(
                    "approved_assets", []
                )
                for campaign in service.scoped(s, "campaigns", p)
            ):
                fail(422, "Choose a voicemail asset already approved for this seller's campaign")
            update(seller, operating_policy=values, operating_policy_version=version + 1)
            receipt = service.local_edit_receipt(s, p, identity, seller, object_version=version + 1)
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
        return {**get_policy(p), "edit_receipt": receipt}

    from app.compliance.screening import bind_screening_api

    bind_screening_api(app, service, administrator)
