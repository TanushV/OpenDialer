import math
from datetime import UTC, datetime, timedelta

from app.scheduling.models import HUMAN


def wilson(successes, total, *, z=1.96):
    if not total:
        return (0.0, 1.0)
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def timestamp(value):
    if not value:
        return None
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result


def number_health(attempts, *, pooled_attempts, now):
    observed = [
        r
        for r in attempts
        if not r.get("started_at") or now - timedelta(days=90) <= timestamp(r["started_at"]) <= now
    ]
    count = len(observed)
    if not count:
        return {
            "score": None,
            "samples": 0,
            "status": "unknown",
            "confidence_low": 0.0,
            "confidence_high": 1.0,
            "spam_label_confirmed": False,
            "reason": "No observed first-party attempts",
            "evidence_scope": "first_party",
        }
    human_outcomes = HUMAN | {"wrong_party", "qualified_conversation", "sale_won", "opt_out"}
    pooled = [r for r in pooled_attempts if r.get("outcome")]
    pooled_rate = (sum(r["outcome"] in human_outcomes for r in pooled) + 2) / (len(pooled) + 10)
    human = sum(r.get("outcome") in human_outcomes for r in observed)
    failed = sum(
        bool(r.get("delivery_failed")) or r.get("outcome") in {"temporary_failure", "route_failure"}
        for r in observed
    )
    expected = sum(float(r.get("expected_human") or pooled_rate) for r in observed)
    ratio = (human + 5) / (expected + 5)
    delivery = (count - failed + 9) / (count + 10)
    durations = [
        float(r["connected_seconds"])
        for r in observed
        if r.get("connected_seconds") is not None and float(r["connected_seconds"]) >= 0
    ]
    short = sum(seconds <= 6 for seconds in durations)
    low, high = wilson(count - failed, count)
    investigating = count >= 20 and (failed / count > 0.2 or ratio < 0.5)
    return {
        "score": round(min(1.0, delivery * min(1.0, max(0.25, ratio))), 6),
        "samples": count,
        "status": "investigating" if investigating else "observed",
        "delivery_success_rate": delivery,
        "pickup_rate": (human + 1) / (count + 2),
        "adjusted_pickup_ratio": ratio,
        "expected_human_answers": expected,
        "confidence_low": low,
        "confidence_high": high,
        "confidence_measure": "delivery_success",
        "short_connected_calls": short,
        "connected_duration_samples": len(durations),
        "short_connected_rate": short / len(durations) if durations else None,
        "callbacks": sum(bool(r.get("inbound_callback")) for r in observed),
        "spam_label_confirmed": False,
        "evidence_scope": "first_party",
        "reason": "Delivery or case-adjusted answer deterioration needs review"
        if investigating
        else "Pooled case-mix adjustment; no carrier spam-label assertion",
        "as_of": now.isoformat(),
    }


def number_ready(number):
    return (
        number.get("lifecycle") in {"healthy", "limited_real_use"}
        and number.get("provider_present") is not False
        and number.get("identity_configured") is not False
        and number.get("callback_tested") is not False
        and not number.get("provider_recording_enabled")
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
    waiting_for_bound = not bound and any(n["id"] == bound_id and number_ready(n) for n in inventory)
    regional = [n for n in usable if n.get("region") == lead.get("region")]
    general = [n for n in usable if n.get("region") == "general"]
    candidates = (
        [bound] if bound else (general or regional) if strategy == "general_first" else regional or general
    )
    if not candidates or waiting_for_bound:
        return {
            "number_id": None,
            "reason": "Wait for capacity on the bound identity"
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
