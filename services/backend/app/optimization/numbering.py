import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
):
    if (
        daily_capacity_per_number < 1
        or not 1 <= horizon_hours <= 168
        or rep_minutes < 0
        or handling_seconds <= 0
    ):
        raise ValueError("Positive bounded horizon/capacity and nonnegative available rep time required")
    policy = policy or {}
    if staffed_windows:
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
        )
    end, demand = now + timedelta(hours=horizon_hours), {}
    callback_demand, eligible_counts = {}, {}
    unknown_windows = 0
    for lead in leads:
        if (
            lead.get("status", "eligible") != "eligible"
            or lead.get("suppressed")
            or lead.get("calling_permission") == "denied"
            or lead.get("outreach_hold")
            or lead.get("goal_completed")
        ):
            continue
        remaining = max(0, policy.get("max_attempts", 4) - lead.get("attempt_count", 0))
        due = max(now, timestamp(lead.get("callback_due_at") or lead.get("next_attempt_at")) or now)
        retry = max(3600, policy.get("min_retry_seconds", 3600))
        count = 0
        try:
            if not lead.get("timezone"):
                raise ZoneInfoNotFoundError("unknown")
            zone = ZoneInfo(lead["timezone"])
        except ZoneInfoNotFoundError:
            unknown_windows += 1
            continue
        while due < end and count < remaining:
            local = due.astimezone(zone)
            if policy.get("local_start_hour", 9) <= local.hour < policy.get("local_end_hour", 20):
                count += 1
                due += timedelta(seconds=retry)
            else:
                due += timedelta(hours=1)
        if count:
            region = lead.get("region") or "general"
            demand[region] = demand.get(region, 0) + count
            eligible_counts[region] = eligible_counts.get(region, 0) + 1
            callback_demand[region] = callback_demand.get(region, 0) + bool(lead.get("callback_due_at"))
    available_attempts = int(rep_minutes * 60 / handling_seconds)
    days = max(1, math.ceil(horizon_hours / 24))
    added_capacity = daily_capacity_per_number * days
    waiting_states = {
        "purchased",
        "identity_configured",
        "callback_tested",
        "investigating",
        "paused_for_outbound",
    }
    order = sorted(demand, key=lambda r: (-callback_demand.get(r, 0), -demand[r], r))
    regional, general, waiting, general_waiting, owned_counts = {}, 0, {}, 0, {}
    for number in inventory:
        region = number.get("region", "general")
        state = number.get("lifecycle")
        if number_ready(number):
            capacity = max(
                0,
                number.get("daily_limit", 0) * days
                - number.get("used_today", 0)
                - number.get("reserved_attempts", 0),
            )
            if region == "general":
                general += capacity
            else:
                regional[region] = regional.get(region, 0) + capacity
            owned_counts[region] = owned_counts.get(region, 0) + 1
        elif state in waiting_states or state in {"healthy", "limited_real_use"}:
            capacity = max(daily_capacity_per_number, number.get("daily_limit", 0)) * days
            if region == "general":
                general_waiting += capacity
            else:
                waiting[region] = waiting.get(region, 0) + capacity
    allocated = {r: min(demand[r], regional.get(r, 0)) for r in order}
    shared = {}
    # A general pool is debited once, even when the lead's region is also "general".
    for region in order:
        shared[region] = min(general, demand[region] - allocated[region])
        general -= shared[region]
    planned = {r: 0 for r in order}
    remaining_rep = available_attempts
    # Protect callbacks, then use existing inventory before proposing additions for unstaffed work.
    for region in order:
        planned[region] = min(callback_demand.get(region, 0), remaining_rep)
        remaining_rep -= planned[region]
    for region in order:
        extra = min(remaining_rep, max(0, allocated[region] + shared[region] - planned[region]))
        planned[region] += extra
        remaining_rep -= extra
    for region in order:
        extra = min(remaining_rep, demand[region] - planned[region])
        planned[region] += extra
        remaining_rep -= extra
    regions, spare_new_capacity, proposed = [], 0, 0
    for region in order:
        usable = min(allocated[region], planned[region])
        shared_used = min(shared[region], planned[region] - usable)
        deficit = planned[region] - usable - shared_used
        owned_waiting = min(deficit, waiting.get(region, 0))
        shared_waiting = min(general_waiting, deficit - owned_waiting)
        general_waiting -= shared_waiting
        awaiting_review = owned_waiting + shared_waiting
        unmet = deficit - awaiting_review
        reuse_new = min(unmet, spare_new_capacity)
        spare_new_capacity -= reuse_new
        add = math.ceil((unmet - reuse_new) / added_capacity)
        spare_new_capacity += add * added_capacity - (unmet - reuse_new)
        proposed += add
        regions.append(
            {
                "region": region,
                "forecast_attempts": demand[region],
                "planned_attempts": planned[region],
                "eligible_leads": eligible_counts[region],
                "healthy_existing": owned_counts.get(region, 0),
                "usable_existing_capacity": usable,
                "general_fallback_capacity": shared_used,
                "capacity_waiting_for_review": awaiting_review,
                "outbound_capacity_shortfall": deficit,
                "callback_demand": callback_demand.get(region, 0),
                "required": math.ceil(planned[region] / added_capacity),
                "proposed_additional": add,
                "proposed_pool": "general",
                "incremental_capacity_used": unmet,
                "reason": "Reuse owned capacity; shared additions cover sparse regions. Resolve waiting inventory first.",
            }
        )
    existing = sum(r["usable_existing_capacity"] + r["general_fallback_capacity"] for r in regions)
    demand_total = sum(demand.values())
    number_shortfall = min(demand_total, available_attempts) - existing
    minimum = math.ceil(number_shortfall / added_capacity)
    return {
        "regions": regions,
        "horizon_hours": horizon_hours,
        "rep_capacity_attempts": available_attempts,
        "forecast_attempts": demand_total,
        "existing_only_capacity": existing,
        "staffing_shortfall": max(0, demand_total - available_attempts),
        "number_shortfall": number_shortfall,
        "capacity_shortfall": max(0, demand_total - existing) + unknown_windows,
        "window_unknown_leads": unknown_windows,
        "minimum_additional_numbers": minimum,
        "proposed_additional_numbers": proposed,
        "lower_bound": minimum,
        "optimality": "proven_small_case" if days == 1 else "aggregate_lower_bound",
        "optimality_scope": "Homogeneous shared numbers and horizon workload; reviewed staffing and time buckets still govern execution",
        "existing_plus_addition_capacity": min(
            demand_total, available_attempts, existing + minimum * added_capacity
        ),
        "purchased": False,
        "approval_required": True,
        "purchase_allowed": False,
        "purchase_blocker": "New rentals are recurring commitments",
        "estimate_requires_provider_quote": True,
        "monthly_rental_estimate_usd": proposed,
        "estimate_note": "Illustrative rental units only; new rentals blocked by the no-subscription policy",
        "cold_start": not any(n.get("health", {}).get("samples", 0) for n in inventory),
        "expected_conversion_lift": None,
        "assumptions": {
            "daily_slots_per_added_number": daily_capacity_per_number,
            "handling_seconds": handling_seconds,
            "rep_minutes": rep_minutes,
            "retry_demand": "Conservative upper bound within the allowed attempt budget",
            "future_readiness": "Waiting inventory is not currently usable capacity",
        },
        "as_of": now.isoformat(),
    }
