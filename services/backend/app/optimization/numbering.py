import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.scheduling.models import HUMAN


def wilson(successes, total):
    if not total:
        return (0.0, 1.0)
    p, z = successes / total, 1.96
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
        r for r in attempts
        if not r.get("started_at") or now - timedelta(days=90) <= timestamp(r["started_at"]) <= now
    ]
    count = len(observed)
    if not count:
        return {"score": None, "samples": 0, "status": "unknown", "confidence_low": 0.0,
                "confidence_high": 1.0, "spam_label_confirmed": False,
                "reason": "No observed first-party attempts", "evidence_scope": "first_party"}
    human_outcomes = HUMAN | {"wrong_party", "qualified_conversation", "sale_won", "opt_out"}
    pooled = [r for r in pooled_attempts if r.get("outcome")]
    pooled_rate = (sum(r["outcome"] in human_outcomes for r in pooled) + 2) / (len(pooled) + 10)
    human = sum(r.get("outcome") in human_outcomes for r in observed)
    failed = sum(bool(r.get("delivery_failed")) or r.get("outcome") in {"temporary_failure", "route_failure"}
                 for r in observed)
    expected = sum(float(r.get("expected_human") or pooled_rate) for r in observed)
    ratio = (human + 5) / (expected + 5)
    delivery = (count - failed + 9) / (count + 10)
    short = sum(0 < float(r.get("duration_seconds") or 0) <= 6 for r in observed
                if r.get("outcome") in human_outcomes)
    low, high = wilson(count - failed, count)
    investigating = count >= 20 and (failed / count > 0.2 or ratio < 0.5)
    return {
        "score": round(min(1.0, delivery * min(1.0, max(0.25, ratio))), 6),
        "samples": count, "status": "investigating" if investigating else "observed",
        "delivery_success_rate": delivery, "pickup_rate": (human + 1) / (count + 2),
        "adjusted_pickup_ratio": ratio, "expected_human_answers": expected,
        "confidence_low": low, "confidence_high": high, "confidence_measure": "delivery_success",
        "short_connected_calls": short, "callbacks": sum(bool(r.get("inbound_callback")) for r in observed),
        "spam_label_confirmed": False, "evidence_scope": "first_party",
        "reason": "Delivery or case-adjusted answer deterioration needs review" if investigating
                  else "Pooled case-mix adjustment; no carrier spam-label assertion",
        "as_of": now.isoformat(),
    }


def select_number(lead, inventory, *, bound_id=None):
    usable = [n for n in inventory if n.get("lifecycle") in {"healthy", "limited_real_use"}
              and int(n.get("active_obligations", 0)) < int(n.get("capacity", 1))
              and int(n.get("used_today", 0)) + int(n.get("reserved_attempts", 0)) < int(n.get("daily_limit", 0))]
    bound = next((n for n in usable if n["id"] == bound_id), None)
    regional = [n for n in usable if n.get("region") == lead.get("region")]
    general = [n for n in usable if n.get("region") == "general"]
    candidates = [bound] if bound else regional or general
    if not candidates:
        return {"number_id": None, "reason": "No usable bound, regional or general capacity",
                "previous_number_id": bound_id, "reassignment_reason": None}
    selected = min(candidates, key=lambda n: (
        n.get("active_obligations", 0) / max(1, n.get("capacity", 1)),
        n.get("used_today", 0) / max(1, n.get("daily_limit", 1)),
        -(n.get("health", {}).get("score") or 0), n["id"]))
    reason = "existing_binding" if bound else "regional_capacity" if regional else "general_capacity"
    return {"number_id": selected["id"], "reason": reason, "previous_number_id": bound_id,
            "reassignment_reason": "Previous identity is unavailable, paused or at capacity"
            if bound_id and selected["id"] != bound_id else None,
            "health": selected.get("health", {"score": None, "samples": 0}),
            "remaining_daily_capacity": max(0, selected.get("daily_limit", 0) - selected.get("used_today", 0)),
            "inbound_retained": True}


def capacity_forecast(leads, inventory, *, now, horizon_hours=24, rep_minutes=480,
                      handling_seconds=120, daily_capacity_per_number=40, policy=None):
    if daily_capacity_per_number < 1 or not 1 <= horizon_hours <= 168 or rep_minutes < 0 or handling_seconds <= 0:
        raise ValueError("Positive bounded horizon/capacity and nonnegative available rep time required")
    policy = policy or {}
    end, demand = now + timedelta(hours=horizon_hours), {}
    callback_demand = {}
    for lead in leads:
        if lead.get("status", "eligible") != "eligible" or lead.get("suppressed") or lead.get("calling_permission") == "denied":
            continue
        remaining = max(0, policy.get("max_attempts", 4) - lead.get("attempt_count", 0))
        due = max(now, timestamp(lead.get("callback_due_at") or lead.get("next_attempt_at")) or now)
        retry = max(3600, policy.get("min_retry_seconds", 3600))
        count = 0
        try:
            zone = ZoneInfo(lead.get("timezone") or "UTC")
        except ZoneInfoNotFoundError:
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
            callback_demand[region] = callback_demand.get(region, 0) + bool(lead.get("callback_due_at"))
    available_attempts = int(rep_minutes * 60 / handling_seconds)
    remaining_rep = available_attempts
    regions = []
    days = max(1, math.ceil(horizon_hours / 24))
    general_available = sum(max(0, n.get("daily_limit", 0) * days - n.get("used_today", 0) - n.get("reserved_attempts", 0))
                            for n in inventory if n.get("region") == "general"
                            and n.get("lifecycle") in {"healthy", "limited_real_use"})
    for region in sorted(demand, key=lambda r: (-callback_demand.get(r, 0), -demand[r], r)):
        raw = demand[region]
        planned = min(raw, remaining_rep)
        remaining_rep -= planned
        owned = [n for n in inventory if n.get("region") == region
                 and n.get("lifecycle") in {"healthy", "limited_real_use"}]
        usable = sum(max(0, n.get("daily_limit", 0) * days - n.get("used_today", 0)
                         - n.get("reserved_attempts", 0)) for n in owned)
        shared = min(general_available, max(0, planned - usable)) if region != "general" else 0
        general_available -= shared
        deficit = max(0, planned - usable - shared)
        regions.append({"region": region, "forecast_attempts": raw, "planned_attempts": planned,
                        "eligible_leads": sum(l.get("region", "general") == region for l in leads),
                        "healthy_existing": len(owned), "usable_existing_capacity": usable,
                        "general_fallback_capacity": shared, "callback_demand": callback_demand.get(region, 0),
                        "required": math.ceil(planned / (daily_capacity_per_number * days)),
                        "proposed_additional": math.ceil(deficit / (daily_capacity_per_number * days)),
                        "reason": "Existing and general inventory subtracted before any proposed purchase"})
    return {"regions": regions, "horizon_hours": horizon_hours, "rep_capacity_attempts": available_attempts,
            "forecast_attempts": sum(demand.values()),
            "capacity_shortfall": max(0, sum(demand.values()) - available_attempts),
            "purchased": False, "approval_required": True, "estimate_requires_provider_quote": True,
            "monthly_rental_estimate_usd": sum(r["proposed_additional"] for r in regions),
            "estimate_note": "Illustrative rental units; obtain a fresh carrier quote",
            "as_of": now.isoformat()}
