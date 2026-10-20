import hashlib
import json
import math
import statistics
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from app.scheduling.models import contact_labels
from app.scheduling.models import wilson as wilson


class HealthPolicy(BaseModel):
    """Versioned health review defaults; the domain owner, serialized by the settings API."""

    model_config = ConfigDict(extra="forbid")
    min_comparable: int = Field(default=30, ge=10, le=1000, strict=True)
    material_drop: float = Field(default=0.20, gt=0, lt=1, allow_inf_nan=False)
    confidence: float = Field(default=0.95, ge=0.8, le=0.999, allow_inf_nan=False)
    window_days: int = Field(default=90, ge=1, le=365, strict=True)
    callback_proof_days: int = Field(default=30, ge=1, le=90, strict=True)


def timestamp(value):
    if not value:
        return None
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result


def health_values(row, *, now, policy):
    """One observation's replaceable sufficient statistics, never text or a score."""
    start = timestamp(row.get("started_at"))
    if start and not now - timedelta(days=policy["window_days"]) <= start <= now:
        return {}
    human, right = contact_labels(row)
    prediction = row.get("expected_human")
    valid_prediction = type(prediction) in (int, float) and math.isfinite(prediction) and 0 <= prediction <= 1
    known_at = timestamp(row.get("prediction_at"))
    comparable = bool(
        human is not None
        and valid_prediction
        and row.get("feature_schema_version") == 2
        and known_at
        and start
        and known_at <= start
    )
    failed = bool(row.get("delivery_failed") or row.get("outcome") in {"temporary_failure", "route_failure"})
    delivery = human is not None or row.get("delivery_failed") is not None or failed
    duration = row.get("connected_seconds")
    measured = bool(
        human is not None
        and row.get("connected_leg_kind") in {None, "prospect"}
        and type(duration) in (int, float)
        and math.isfinite(duration)
        and duration >= 0
    )
    residual = int(human) - prediction if comparable else 0
    return {
        "samples": 1,
        "human_classified_samples": int(human is not None),
        "human_answers": int(human is True),
        "right_party_samples": int(right is not None),
        "right_party_answers": int(right is True),
        "delivery_samples": int(delivery),
        "delivery_failures": int(delivery and failed),
        "comparable_samples": int(comparable),
        "expected_human_answers": prediction if comparable else 0,
        "residual_sum": residual,
        "residual_squared_sum": residual * residual,
        "legacy_expected_human": prediction if human is not None and valid_prediction else 0,
        "short_connected_calls": int(measured and duration <= 6),
        "connected_duration_samples": int(measured),
        "callbacks": int(bool(row.get("inbound_callback"))),
        "opt_outs": int(row.get("outcome") == "opt_out"),
        "hard_provider_errors": int(bool(row.get("hard_provider_error"))),
    }


def health_stratum(row):
    return tuple(
        str(row.get(k, "unknown"))
        for k in ("campaign_id", "segment", "local_hour", "source", "attempt_count")
    )


def health_from_counts(counts, *, now, policy, comparison_strata, next_expiry_at=None):
    """The same descriptive intervals for incremental and explicit offline reads."""
    policy = HealthPolicy.model_validate(policy or {}).model_dump(exclude={"callback_proof_days"})
    fields = (
        "samples",
        "human_classified_samples",
        "human_answers",
        "right_party_samples",
        "right_party_answers",
        "delivery_samples",
        "delivery_failures",
        "expected_human_answers",
        "comparable_samples",
        "short_connected_calls",
        "connected_duration_samples",
        "callbacks",
        "opt_outs",
    )
    values = {key: counts.get(key, 0) for key in fields}
    n, human_n, human_s = (
        values["comparable_samples"],
        values["human_classified_samples"],
        values["human_answers"],
    )
    delivered = values["delivery_samples"] - values["delivery_failures"]
    z = statistics.NormalDist().inv_cdf((1 + policy["confidence"]) / 2)
    mean = counts.get("residual_sum", 0) / n if n else None
    interval = None
    if n >= policy["min_comparable"]:
        variance = max(0, (counts.get("residual_squared_sum", 0) - n * mean * mean) / (n - 1))
        margin = z * math.sqrt(variance / n)
        interval = [max(-1, mean - margin), min(1, mean + margin)]
    low, high = wilson(delivered, values["delivery_samples"], z=z)
    return {
        **values,
        "human_unknown_samples": values["samples"] - human_n,
        "delivery_success_rate": delivered / values["delivery_samples"]
        if values["delivery_samples"]
        else None,
        "pickup_rate": human_s / human_n if human_n else None,
        "pickup_interval": list(wilson(human_s, human_n, z=z)),
        "delivery_failure_interval": list(
            wilson(values["delivery_failures"], values["delivery_samples"], z=z)
        ),
        "right_party_rate": values["right_party_answers"] / values["right_party_samples"]
        if values["right_party_samples"]
        else None,
        "adjusted_pickup_ratio": (human_s + 5) / (counts.get("legacy_expected_human", 0) + 5),
        "excluded_comparison_samples": values["samples"] - n,
        "comparison_strata": comparison_strata,
        "case_mix_residual_mean": mean,
        "case_mix_interval": interval,
        "comparison_method": "frozen_pre_call_residual_approximate_normal_interval",
        "confidence_low": low,
        "confidence_high": high,
        "confidence_measure": "observed_delivery_success",
        "short_connected_rate": values["short_connected_calls"] / values["connected_duration_samples"]
        if values["connected_duration_samples"]
        else None,
        "hard_provider_error": counts.get("hard_provider_errors", 0) > 0,
        "as_of": now.isoformat(),
        "next_expiry_at": next_expiry_at,
        "assessment_schema": 1,
        "policy_version": hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()[:16],
    }


def _observed_health(observed, now, policy):
    counts, strata, expiry = {}, set(), []
    for row in observed:
        vector = health_values(row, now=now, policy=policy)
        if not vector:
            continue
        for key, value in vector.items():
            counts[key] = counts.get(key, 0) + value
        if vector["comparable_samples"]:
            strata.add(health_stratum(row))
        if row.get("started_at"):
            expiry.append(timestamp(row["started_at"]) + timedelta(days=policy["window_days"]))
    return health_from_counts(
        counts,
        now=now,
        policy=policy,
        comparison_strata=len(strata),
        next_expiry_at=min(expiry).isoformat() if expiry else None,
    )


def assess_number_health(observed, baseline, external, policy):
    """Assess observations or saved statistics; current metadata never reuses an old green badge.

    Delivery deterioration is a drop relative to a supported comparable reference (other
    numbers' delivery in the same window), never the policy margin read as an absolute rate.
    """
    policy = HealthPolicy.model_validate(policy or {}).model_dump(exclude={"callback_proof_days"})
    version = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()[:16]
    now = timestamp(baseline["as_of"])
    cached = isinstance(observed, dict)
    usable = cached and observed.get("assessment_schema") == 1 and observed.get("policy_version") == version
    usable = (
        usable
        and timestamp(observed.get("as_of")) <= now
        and (not observed.get("next_expiry_at") or now < timestamp(observed["next_expiry_at"]))
    )
    stats = dict(observed) if usable else _observed_health([] if cached else observed, now, policy)
    supported = stats["comparable_samples"] >= policy["min_comparable"]
    pickup_drop = bool(
        stats["case_mix_interval"] and stats["case_mix_interval"][1] < -policy["material_drop"]
    )
    reference = dict(baseline.get("delivery_reference") or {})
    if baseline.get("attempts"):
        pooled = _observed_health(baseline["attempts"], now, policy)
        reference = {"samples": pooled["delivery_samples"], "failures": pooled["delivery_failures"]}
    reference_n, reference_failures = reference.get("samples", 0), reference.get("failures", 0)
    z = statistics.NormalDist().inv_cdf((1 + policy["confidence"]) / 2)
    # Without a supported comparable reference, the drop is measured from complete delivery
    # and labeled as that assumption; it is never compared to the margin as a raw rate.
    supported_reference = reference_n >= policy["min_comparable"]
    reference_high = wilson(reference_failures, reference_n, z=z)[1] if supported_reference else 0.0
    delivery_drop = bool(
        stats["delivery_samples"] >= policy["min_comparable"]
        and stats["delivery_failure_interval"][0] - reference_high > policy["material_drop"]
    )
    errors = list(baseline.get("configuration_errors", []))
    if stats["hard_provider_error"]:
        errors.append("hard_provider_error")
    external = dict(external) if external else None
    fresh = bool(
        external
        and external.get("source")
        and external.get("ownership_reference")
        and timestamp(external.get("observed_at"))
        and timestamp(external["observed_at"]) <= now
        and timestamp(external.get("expires_at"))
        and now < timestamp(external["expires_at"])
    )
    flag = (
        fresh
        and external.get("spam_label") is not None
        and external.get("spam_label") not in {"clear", "not_flagged"}
    )
    reasons = (
        errors
        + (["case_mix_pickup_drop"] if pickup_drop else [])
        + (["delivery_deterioration"] if delivery_drop else [])
    )
    if flag:
        reasons.append("imported_external_flag")
    if not supported:
        reasons.append("low_comparable_support")
    if cached and not usable:
        reasons.append("health_refresh_required")
    status = (
        "blocked"
        if errors
        else "investigating"
        if pickup_drop or delivery_drop or flag
        else "observed"
        if supported
        else "unknown"
    )
    return {
        **stats,
        "assessment_schema": 1,
        "status": status,
        "score": stats["delivery_success_rate"]
        if stats["delivery_samples"] >= policy["min_comparable"] and not errors
        else None,
        "score_measure": "observed_delivery_success",
        "spam_label_confirmed": False,
        "external": external,
        "external_fresh": fresh,
        "delivery_reference": {
            "source": "other_numbers" if supported_reference else "assumed_complete_delivery",
            "samples": reference_n,
            "failures": reference_failures,
            "failure_upper": reference_high,
        },
        "assessed_at": now.isoformat(),
        "evidence_scope": "first_party_with_separate_imported_evidence",
        "reason_codes": reasons,
        "reason": "; ".join(reasons)
        if reasons
        else "Comparable observations; no carrier spam-label assertion",
        "policy": policy,
        "policy_version": version,
        "refresh_required": cached and not usable,
        "recommended_action": "hold_outbound"
        if errors
        else "review_workload"
        if status == "investigating"
        else "retain_reviewed_workload",
    }


def number_health(
    attempts,
    *,
    pooled_attempts,
    now,
    external=None,
    policy=None,
    configuration_errors=(),
    delivery_reference=None,
):
    return assess_number_health(
        attempts,
        {
            "as_of": now.isoformat(),
            "attempts": pooled_attempts,
            "configuration_errors": configuration_errors,
            "delivery_reference": delivery_reference,
        },
        external,
        policy,
    )


def route_fingerprint(number, route_version):
    values = [number.get(k) for k in ("id", "provider_id", "connection_id", "provider_recording_enabled")]
    return hashlib.sha256(json.dumps([*values, route_version], separators=(",", ":")).encode()).hexdigest()


def callback_current(number, now, route_version=None):
    proof = number.get("callback_proof")
    if not proof:
        return bool(
            number.get("synthetic")
            and number.get("registration_reference") == "SIMULATION"
            and number.get("callback_tested")
        )
    return bool(
        proof.get("number_id") == number["id"]
        and timestamp(proof.get("expires_at"))
        and timestamp(proof.get("observed_at"))
        and timestamp(proof["observed_at"]) <= now < timestamp(proof["expires_at"])
        and proof.get("route_fingerprint") == route_fingerprint(number, proof.get("route_version"))
        and (route_version is None or proof.get("route_version") == route_version)
        and (number.get("synthetic") or not proof.get("simulated"))
    )


def number_ready(number, *, now=None, strict=False, route_version=None):
    if number.get("outbound_ready") is False:
        return False
    if strict or number.get("callback_proof"):
        now = now or datetime.now(UTC)
        if not callback_current(number, now, route_version):
            return False
    return (
        number.get("lifecycle") in {"healthy", "limited_real_use"}
        and number.get("provider_present") is not False
        and number.get("identity_configured") is not False
        and number.get("callback_tested") is not False
        and not number.get("provider_recording_enabled")
        and (
            not strict
            or (
                number.get("configuration_status") == "confirmed"
                and number.get("provider_recording_enabled") is False
            )
        )
    )


def select_number(lead, inventory, *, bound_id=None, strategy="regional_first"):
    usable = [
        n
        for n in inventory
        if number_ready(n)
        and int(n.get("active_obligations", 0)) < int(n.get("capacity", 1))
        and int(n.get("used_today", 0)) + int(n.get("reserved_attempts", 0)) < int(n.get("daily_limit", 0))
    ]
    bound = next((n for n in usable if n["id"] == bound_id), None)
    waiting_for_bound = bool(bound_id and not bound)
    reassignment_required = waiting_for_bound and not any(
        n["id"] == bound_id and number_ready(n) for n in inventory
    )
    regional = [n for n in usable if n.get("region") == lead.get("region")]
    general = [n for n in usable if n.get("region") == "general"]
    candidates = (
        [bound] if bound else (general or regional) if strategy == "general_first" else regional or general
    )
    if not candidates or waiting_for_bound:
        return {
            "number_id": None,
            "reason_code": "reassignment_required"
            if reassignment_required
            else "bound_capacity"
            if waiting_for_bound
            else "no_capacity",
            "reason": "Explicit reassignment required; previous inbound identity is retained"
            if reassignment_required
            else "Wait for capacity on the bound identity"
            if waiting_for_bound
            else "No usable bound, regional or general capacity",
            "previous_number_id": bound_id,
            "reassignment_reason": None,
        }
    selected = min(
        candidates,
        key=lambda n: (
            n.get("active_obligations", 0) / max(1, n.get("capacity", 1)),
            n.get("used_today", 0) / max(1, n.get("daily_limit", 1)),
            -(n.get("health", {}).get("score") or 0),
            n["id"],
        ),
    )
    reason = (
        "existing_binding" if bound else "general_capacity" if selected in general else "regional_capacity"
    )
    return {
        "number_id": selected["id"],
        "strategy": strategy,
        "reason": reason,
        "previous_number_id": bound_id,
        "reassignment_reason": "Previous identity is unavailable, paused or at capacity"
        if bound_id and selected["id"] != bound_id
        else None,
        "health": selected.get("health", {"score": None, "samples": 0}),
        "remaining_daily_capacity": max(0, selected.get("daily_limit", 0) - selected.get("used_today", 0)),
        "inbound_retained": True,
    }


def capacity_forecast(
    leads,
    inventory,
    *,
    now,
    horizon_hours=24,
    rep_minutes=480,
    handling_seconds=120,
    daily_capacity_per_number=40,
    policy=None,
    staffed_windows=None,
    additional_ready_after_hours=0,
    rep_busy_until=None,
    retry_observations=(),
    schedule_row_limit=1000,
):
    if (
        daily_capacity_per_number < 1
        or not 1 <= horizon_hours <= 168
        or rep_minutes < 0
        or handling_seconds <= 0
    ):
        raise ValueError("Positive bounded horizon/capacity and nonnegative available rep time required")
    policy = policy or {}
    from app.optimization.capacity import time_forecast

    return time_forecast(
        leads,
        inventory,
        now=now,
        horizon_hours=horizon_hours,
        rep_minutes=rep_minutes,
        handling_seconds=handling_seconds,
        daily_capacity_per_number=daily_capacity_per_number,
        policy=policy,
        staffed_windows=staffed_windows,
        additional_ready_after_hours=additional_ready_after_hours,
        rep_busy_until=rep_busy_until,
        retry_observations=retry_observations,
        schedule_row_limit=schedule_row_limit,
    )
