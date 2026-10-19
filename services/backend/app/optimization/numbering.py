import hashlib
import json
import math
import statistics
from datetime import UTC, datetime, timedelta

from app.scheduling.models import contact_labels
from app.scheduling.models import wilson as wilson


def timestamp(value):
    if not value:
        return None
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result


def _observed_health(observed, now, policy):
    since = now - timedelta(days=policy["window_days"])
    rows = [r for r in observed if not r.get("started_at") or since <= timestamp(r["started_at"]) <= now]
    labels = [(r, *contact_labels(r)) for r in rows]
    human = [y for _, y, _ in labels if y is not None]
    right = [y for _, _, y in labels if y is not None]
    delivery = [
        not (r.get("delivery_failed") or r.get("outcome") in {"temporary_failure", "route_failure"})
        for r, y, _ in labels
        if y is not None
        or r.get("delivery_failed") is not None
        or r.get("outcome") in {"temporary_failure", "route_failure"}
    ]
    expected, residuals, strata = 0.0, [], set()
    for r, y, _ in labels:
        prediction, prediction_at, started_at = (
            r.get("expected_human"),
            timestamp(r.get("prediction_at")),
            timestamp(r.get("started_at")),
        )
        if (
            y is None
            or type(prediction) not in (int, float)
            or not math.isfinite(prediction)
            or not 0 <= prediction <= 1
            or r.get("feature_schema_version") != 2
            or not prediction_at
            or not started_at
            or prediction_at > started_at
        ):
            continue
        expected += prediction
        residuals.append(y - prediction)
        strata.add(
            tuple(
                str(r.get(k, "unknown"))
                for k in ("campaign_id", "segment", "local_hour", "source", "attempt_count")
            )
        )
    z = statistics.NormalDist().inv_cdf((1 + policy["confidence"]) / 2)
    mean, interval = statistics.mean(residuals) if residuals else None, None
    if len(residuals) >= policy["min_comparable"]:
        margin = z * statistics.stdev(residuals) / math.sqrt(len(residuals))
        interval = [max(-1, mean - margin), min(1, mean + margin)]
    delivered = sum(delivery)
    low, high = wilson(delivered, len(delivery), z=z)
    durations = [
        float(r["connected_seconds"])
        for r, y, _ in labels
        if y is not None
        and r.get("connected_leg_kind") in {None, "prospect"}
        and type(r.get("connected_seconds")) in (int, float)
        and math.isfinite(r["connected_seconds"])
        and r["connected_seconds"] >= 0
    ]
    short = sum(v <= 6 for v in durations)
    legacy_expected = sum(
        r["expected_human"]
        for r, y, _ in labels
        if y is not None
        and type(r.get("expected_human")) in (int, float)
        and math.isfinite(r["expected_human"])
        and 0 <= r["expected_human"] <= 1
    )
    expiry = min(
        (
            timestamp(r["started_at"]) + timedelta(days=policy["window_days"])
            for r in rows
            if r.get("started_at")
        ),
        default=None,
    )
    return {
        "samples": len(rows),
        "human_classified_samples": len(human),
        "human_unknown_samples": len(rows) - len(human),
        "human_answers": sum(human),
        "right_party_samples": len(right),
        "right_party_answers": sum(right),
        "delivery_samples": len(delivery),
        "delivery_failures": len(delivery) - delivered,
        "delivery_success_rate": delivered / len(delivery) if delivery else None,
        "pickup_rate": sum(human) / len(human) if human else None,
        "pickup_interval": list(wilson(sum(human), len(human), z=z)),
        "delivery_failure_interval": list(wilson(len(delivery) - delivered, len(delivery), z=z)),
        "right_party_rate": sum(right) / len(right) if right else None,
        "adjusted_pickup_ratio": (sum(human) + 5) / (legacy_expected + 5),
        "expected_human_answers": expected,
        "comparable_samples": len(residuals),
        "excluded_comparison_samples": len(rows) - len(residuals),
        "comparison_strata": len(strata),
        "case_mix_residual_mean": mean,
        "case_mix_interval": interval,
        "comparison_method": "frozen_pre_call_residual_approximate_normal_interval",
        "confidence_low": low,
        "confidence_high": high,
        "confidence_measure": "observed_delivery_success",
        "short_connected_calls": short,
        "connected_duration_samples": len(durations),
        "short_connected_rate": short / len(durations) if durations else None,
        "callbacks": sum(bool(r.get("inbound_callback")) for r in rows),
        "opt_outs": sum(r.get("outcome") == "opt_out" for r in rows),
        "hard_provider_error": any(r.get("hard_provider_error") for r in rows),
        "as_of": now.isoformat(),
        "next_expiry_at": expiry.isoformat() if expiry else None,
    }


def assess_number_health(observed, baseline, external, policy):
    """Assess observations or saved statistics; current metadata never reuses an old green badge."""
    from app.optimization.api import HealthPolicy

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
    delivery_drop = (
        stats["delivery_samples"] >= policy["min_comparable"]
        and stats["delivery_failure_interval"][0] > policy["material_drop"]
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


def number_health(attempts, *, pooled_attempts, now, external=None, policy=None, configuration_errors=()):
    return assess_number_health(
        attempts,
        {"as_of": now.isoformat(), "attempts": pooled_attempts, "configuration_errors": configuration_errors},
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
