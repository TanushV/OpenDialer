import hashlib
import math
from datetime import timedelta
from statistics import NormalDist
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.optimization.conversations import conversation_associations as conversation_associations
from app.optimization.numbering import timestamp, wilson
from app.scheduling.models import pooled_predictions

GOALS = {"qualified_conversation", "meeting_held", "sale_won"}
HUMAN = {
    "wrong_party",
    "gatekeeper",
    "right_party",
    "callback_requested",
    "meeting_booked",
    "meeting_held",
    "not_interested",
    "opt_out",
    "qualified_conversation",
    "sale_won",
}
RIGHT = HUMAN - {"wrong_party", "gatekeeper", "opt_out", "not_interested"}
QUALIFIED = {"qualified_conversation", "meeting_booked", "meeting_held", "sale_won"}


def derive_labels(outcome):
    return {
        "human": outcome in HUMAN,
        "right_party": outcome in RIGHT,
        "qualified": outcome in QUALIFIED,
        "meeting_held": outcome == "meeting_held",
        "sale_won": outcome == "sale_won",
    }


def outcome_at(call, now):
    """Only observed dispositions; a later correction is not historical knowledge."""
    versions = [*call.get("outcome_history", []), call]
    known = []
    for version in versions:
        available = timestamp(version.get("outcome_available_at"))
        if not version.get("outcome") or version.get("outcome_source") == "llm_provisional":
            continue
        if available and available > now:
            continue
        known.append({**version, "available": available or now, "timing_known": available is not None})
    # Stored order breaks ties when two corrections share a clock tick.
    return max(reversed(known), key=lambda value: value["available"], default=None)


def conversion_at(event, now):
    versions = [*event.get("history", []), event]
    known = []
    for version in versions:
        occurred = timestamp(version.get("occurred_at"))
        available = timestamp(
            version.get("label_available_at") or version.get("observed_at") or version.get("recorded_at")
        )
        if not occurred or occurred > now or (available and available > now):
            continue
        if version.get("source") == "llm_provisional":
            continue
        retracted_at = timestamp(version.get("retracted_at"))
        retracted = bool(version.get("retracted")) and (retracted_at is None or retracted_at <= now)
        known.append(
            {
                **version,
                "id": event["id"],
                "available": max(occurred, available or now),
                "timing_known": available is not None,
                "retracted": retracted,
                "correction_available": (retracted_at or now) if retracted else None,
            }
        )
    return max(reversed(known), key=lambda value: value["available"], default=None)


def freeze_features(lead, *, health, now, calls=()):
    try:
        local = now.astimezone(ZoneInfo(lead.get("timezone") or "UTC"))
    except ZoneInfoNotFoundError:
        local = now
    previous = [
        timestamp(c["started_at"])
        for c in calls
        if c.get("lead_id") == lead.get("id") and timestamp(c["started_at"]) <= now
    ]
    last = max(previous, default=timestamp(lead.get("last_attempt_at")))
    created = timestamp(lead.get("source_at") or lead.get("created_at")) or now
    return {
        "segment": lead.get("segment", "unknown"),
        "local_hour": local.hour,
        "local_weekday": local.weekday(),
        "attempt_count": int(lead.get("attempt_count", 0)),
        "source_age_days": max(0, (now - created).total_seconds() / 86400),
        "elapsed_hours": max(0, (now - last).total_seconds() / 3600) if last else 720.0,
        "endpoint_evidence": {"confirmed_match": 1, "likely_match": 0.5}.get(lead.get("ownership_status"), 0),
        "number_health": health.get("score"),
        "number_health_missing": int(health.get("score") is None),
        "number_health_samples": int(health.get("samples", 0)),
        "requested_contact": int(bool(lead.get("requested_contact"))),
        "callback_history": sum(
            (outcome_at(c, now) or {}).get("outcome") == "callback_requested"
            for c in calls
            if c.get("lead_id") == lead.get("id")
            and timestamp(c.get("started_at"))
            and timestamp(c["started_at"]) <= now
        ),
    }


def training_rows(calls, snapshots, events, *, now, goal, window_days):
    if goal not in GOALS or not 1 <= window_days <= 180:
        raise ValueError("Reviewed conversion goal and bounded outcome window required")
    by_id = {r["id"]: r for r in snapshots}
    known_events = {}
    for event in events:
        known = conversion_at(event, now)
        if known and known.get("kind") == goal:
            key = (known.get("lead_id"), known.get("call_id") or None)
            known_events.setdefault(key, []).append(known)
    result = []
    for call in sorted(calls, key=lambda c: c.get("started_at", "")):
        at = timestamp(call.get("started_at"))
        snapshot = by_id.get(call.get("feature_snapshot_id"))
        if not at or at > now or not snapshot or timestamp(snapshot.get("as_of")) is None:
            continue
        outcome = outcome_at(call, now)
        if timestamp(snapshot["as_of"]) > at or not outcome:
            continue
        end = at + timedelta(days=window_days)
        relevant = [
            e
            for e in (
                *known_events.get((call["lead_id"], None), []),
                *known_events.get((call["lead_id"], call["id"]), []),
            )
            if at <= timestamp(e["occurred_at"]) <= min(now, end)
        ]
        matched = [e for e in relevant if not e["retracted"]]
        labels = derive_labels(outcome["outcome"])
        direct = labels.get(goal if goal != "qualified_conversation" else "qualified", False)
        positive = bool(matched) or direct
        closed = now >= end
        ended = timestamp(call.get("ended_at"))
        positive_times = [e["available"] for e in matched] + ([outcome["available"]] if direct else [])
        label_available = (
            min(positive_times)
            if positive
            else max(
                [
                    end,
                    outcome["available"],
                    *[e["correction_available"] for e in relevant if e["correction_available"]],
                ]
            )
            if closed
            else None
        )
        result.append(
            {
                "call_id": call["id"],
                "lead_id": call["lead_id"],
                "endpoint_id": call.get("endpoint_id", call["lead_id"]),
                "rep_id": call.get("rep_id", "unknown"),
                "number_id": call.get("number_id", "unknown"),
                "campaign_id": call.get("campaign_id"),
                "observations": call.get("observations", []),
                "at": at.isoformat(),
                "features": dict(snapshot["features"]),
                **labels,
                "outcome": outcome["outcome"],
                "outcome_available_at": outcome["available"].isoformat(),
                "label_available_at": label_available.isoformat() if label_available else None,
                "label_timing_quality": "observed"
                if outcome["timing_known"] and all(e["timing_known"] for e in matched)
                else "observation_time_unknown",
                "conversion": int(positive) if positive or closed else None,
                "goal": goal,
                "window_status": "matured" if closed else "resolved" if positive else "pending",
                "window_closed": closed,
                "window_ends_at": end.isoformat(),
                "conversion_event_ids": sorted({e["id"] for e in matched}),
                "action": call.get("treatment_variant_id", "approved-default"),
                "segment": snapshot["features"].get("segment", "unknown"),
                "local_hour": snapshot["features"].get("local_hour", 12),
                "duration_seconds": max(0, (ended - at).total_seconds()) if ended else 0,
                "propensity": snapshot.get("propensity", 1.0),
                "p_human": snapshot.get("predictions", {}).get("p_human"),
                "experiment_arm": snapshot.get("experiment_arm", "baseline"),
                "observed_cost_usd": snapshot.get("observed_cost_usd"),
            }
        )
    return result


def funnel_predictions(history, features):
    eligible = [r for r in history if r.get("human") is not None]
    local = [
        r
        for r in eligible
        if r.get("segment") == features.get("segment")
        and abs(r.get("local_hour", 12) - features.get("local_hour", 12)) <= 2
    ]
    base = pooled_predictions(
        history, segment=features.get("segment", "unknown"), hour=features.get("local_hour", 12)
    )
    human = sum(r["human"] for r in local)
    right = sum(r.get("right_party", False) for r in local)
    mature = [r for r in local if r.get("window_closed") and r.get("right_party")]
    pooled_mature = [r for r in eligible if r.get("window_closed") and r.get("right_party")]
    prior = (sum(r.get("conversion") or 0 for r in pooled_mature) + 1) / (len(pooled_mature) + 10)
    costs = [r["observed_cost_usd"] for r in local if r.get("observed_cost_usd") is not None]
    return {
        **base,
        "p_human": (human + 10 * (sum(r["human"] for r in eligible) + 2) / (len(eligible) + 10))
        / (len(local) + 10),
        "p_right_party_given_human": (right + 1) / (human + 3),
        "p_conversion_given_right_party": (sum(r.get("conversion") or 0 for r in mature) + 10 * prior)
        / (len(mature) + 10),
        "expected_attempt_cost_usd": sum(costs) / len(costs) if costs else None,
        "cost_observations": len(costs),
        "conversion_observations": len(mature),
        "model_version": "pooled-funnel-v2",
    }


def conversion_score(prediction):
    return (
        prediction.get("p_human", 0.2)
        * prediction.get("p_right_party_given_human", 0.33)
        * prediction.get("p_conversion_given_right_party", 0.1)
        * 60
        / max(1, prediction.get("expected_rep_seconds", 120))
    )


def retry_statistics(rows, leads, *, now, max_attempts):
    """Observed continuation decisions, not conversion-window negatives.

    A subsequent submitted attempt or an explicitly pending next attempt is
    positive. A current terminal/held lead is negative only for its last known
    attempt. Unknown pending work contributes neither success nor failure.
    """
    from collections import defaultdict
    from typing import get_args

    from app.scheduling.models import OutcomeCode

    current = {row["id"]: row for row in leads}
    known, buckets = defaultdict(list), defaultdict(lambda: [0, 0])
    for row in rows:
        if (
            row.get("label_timing_quality") == "observed"
            and row.get("outcome") in get_args(OutcomeCode)
            and timestamp(row.get("outcome_available_at"))
            and timestamp(row["outcome_available_at"]) <= now
            and timestamp(row.get("at"))
            and timestamp(row["at"]) <= now
        ):
            known[row["lead_id"]].append(row)
    for identity, attempts in known.items():
        lead = current.get(identity, {})
        attempts.sort(key=lambda row: (row["features"].get("attempt_count", 0), row["at"]))
        last_by_index = {}
        for row in attempts:
            index = row["features"].get("attempt_count", 0)
            last_by_index[index] = max(row["at"], last_by_index.get(index, row["at"]))
        future_at, latest_at = {}, None
        for index in sorted(last_by_index, reverse=True):
            future_at[index] = latest_at
            latest_at = max(latest_at or last_by_index[index], last_by_index[index])
        for row in attempts:
            index = row["features"].get("attempt_count", 0) + 1
            if index >= max_attempts:
                continue
            next_at = future_at[index - 1]
            subsequent = next_at is not None and next_at > row["at"]
            terminal = bool(
                lead.get("goal_completed")
                or lead.get("outreach_hold")
                or lead.get("suppressed")
                or lead.get("status") in {"converted", "suppressed", "invalid", "exhausted"}
            )
            latest = lead.get("attempt_count") == index
            pending = (
                latest
                and not terminal
                and lead.get("status") == "eligible"
                and timestamp(lead.get("next_attempt_at")) is not None
            )
            if not subsequent and not pending and not (latest and terminal):
                continue
            bucket = buckets[str(row.get("segment") or "unknown"), index]
            bucket[0] += int(subsequent or pending)
            bucket[1] += 1
    return [
        {
            "segment": segment,
            "attempt_index": index,
            "needs_retry_count": values[0],
            "classified_count": values[1],
            "matured": True,
            "available_at": now.isoformat(),
        }
        for (segment, index), values in sorted(buckets.items())
    ]


def learn_distribution(actions, history, *, segment=None, exploration=0.1, control=False, urgent=False):
    if not actions or len(actions) != len(set(actions)) or not 0 <= exploration <= 0.3:
        raise ValueError("Unique approved actions and exploration between zero and 0.3 required")
    if control or urgent:
        return {
            "best": actions[0],
            "probabilities": {a: float(a == actions[0]) for a in actions},
            "policy_version": "protected-default-v2",
            "observations": 0,
        }
    mature = [
        r
        for r in history
        if r.get("window_status") == "matured"
        and r.get("conversion") is not None
        and r.get("action") in actions
    ]
    estimates, observations = {}, {}
    for action in actions:
        all_rows = [r for r in mature if r["action"] == action]
        local = [r for r in all_rows if segment is None or r.get("segment") == segment]
        prior = (sum(r["conversion"] for r in all_rows) + 1) / (len(all_rows) + 5)
        weights = [min(20.0, 1 / max(0.01, float(r.get("propensity") or 1))) for r in local]
        estimates[action] = (
            sum(w * r["conversion"] for w, r in zip(weights, local, strict=True)) + 10 * prior
        ) / (sum(weights) + 10)
        observations[action] = len(local)
    # Sparse data retains the reviewed default; exploration collects support for other approved actions.
    supported = [a for a in actions if observations[a] >= 10]
    best = max(supported, key=lambda a: estimates[a]) if supported else actions[0]
    probabilities = {a: exploration / len(actions) + (1 - exploration if a == best else 0) for a in actions}
    return {
        "best": best,
        "probabilities": probabilities,
        "estimates": estimates,
        "observations": observations,
        "policy_version": "pooled-ipw-epsilon-v2",
    }


def future_slots(
    lead,
    history,
    *,
    now,
    policy,
    horizon_days=3,
    predictor=None,
    estimates=None,
    staffed_windows=None,
    rep_minutes=None,
    inventory=None,
    rep_busy_until=None,
):
    """Bounded suggestions through the same attempt/occupancy engine as capacity.

    Callers without declared staff/inventory receive explicitly policy-only
    possibilities; the runtime always supplies its saved staffing and inventory.
    """
    from app.optimization.capacity import (
        MAX_SLOTS,
        Assignment,
        normalize_availability,
        staffed_slots,
        work_items,
    )

    end = now + timedelta(days=horizon_days)
    try:
        zone = ZoneInfo(lead.get("timezone") or "unknown")
    except (ValueError, ZoneInfoNotFoundError):
        return []
    # Cache each local day/hour once; no model or database call per grid point.
    predictions = {}
    for day in range(7):
        for hour in range(24):
            estimate = dict(
                (estimates or {}).get(str(lead.get("segment", "unknown")) + ":" + str(hour))
                or (estimates or {}).get("pooled")
                or funnel_predictions(
                    history, {"segment": lead.get("segment", "unknown"), "local_hour": hour}
                )
            )
            if predictor is not None:
                estimate.update(predictor(hour, day) or {})
            predictions[day, hour] = estimate
    regions = {
        "general",
        lead.get("region") or "general",
        *(n.get("region") or "general" for n in (inventory or [])),
    }
    # Work estimate is fixed for this attempt; differences in optional slot
    # outcome predictions do not create different physical call durations.
    duration = lead.get("predicted_work_seconds", policy.get("handling_seconds", 120))
    row = {
        **lead,
        "predicted_work_seconds": duration,
        "slot_estimates": {
            f"{day}:{hour}:{region}": math.prod(
                estimate.get(k, 0)
                for k in ("p_human", "p_right_party_given_human", "p_conversion_given_right_party")
            )
            for (day, hour), estimate in predictions.items()
            for region in regions
        },
    }
    work, _ = work_items([row], now, end, policy, duration)
    if not work:
        return []
    # Honor a reachable coverage deadline before considering later alternatives.
    deadline = timestamp(lead.get("coverage_due_at"))
    minutes = rep_minutes if rep_minutes is not None else horizon_days * 1440
    windows = (
        staffed_windows if staffed_windows is not None else [{"start_at": now, "end_at": end, "reps": 1}]
    )
    availability = normalize_availability(windows, now, end, minutes, policy)
    grid = max(30, math.ceil((end - now).total_seconds() / (MAX_SLOTS * 30)) * 30)
    slots, owners, _ = staffed_slots(availability, now, end, grid, rep_busy_until)
    assignment = Assignment(work, slots, now, policy, grid, 1, owners, work_budget=minutes * 60)
    if deadline and deadline > work[0].earliest_at:
        before = sum(1 << i for i, at in enumerate(assignment.starts) if at <= deadline)
        if assignment.allowed[0] & before:
            assignment.allowed[0] &= before
    numbers = (
        inventory
        if inventory is not None
        else [
            {
                "id": lead.get("bound_number_id") or "policy-only",
                "region": "general",
                "lifecycle": "healthy",
                "daily_limit": 1,
                "capacity": 1,
            }
        ]
    )
    choices, _ = assignment.solve(numbers, alternatives=8)
    result = []
    for choice in choices:
        at = timestamp(choice["at"])
        local = at.astimezone(zone)
        estimate = predictions[local.weekday(), local.hour]
        result.append(
            {
                **choice,
                "local_hour": local.hour,
                "reason": "requested_callback"
                if lead.get("callback_due_at")
                else "coverage_deadline"
                if deadline and at >= deadline
                else "expected_conversion_per_rep_minute",
                "score": conversion_score(estimate),
                "predictions": estimate,
                "constraint_scope": "declared_staff_and_inventory"
                if staffed_windows is not None and inventory is not None
                else "policy_only",
            }
        )
    return sorted(
        result, key=lambda r: (r["at"] if lead.get("callback_due_at") else "", -r["score"], r["at"])
    )[:8]


def drift_report(reference, recent):
    def brier(data):
        valid = [r for r in data if r.get("p_human") is not None and r.get("human") is not None]
        return (
            sum((float(r["p_human"]) - int(r["human"])) ** 2 for r in valid) / len(valid) if valid else None
        )

    changes = {}
    for key in ("local_hour", "attempt_count", "source_age_days", "number_health_missing"):
        left = [float(r.get("features", {}).get(key) or 0) for r in reference]
        right = [float(r.get("features", {}).get(key) or 0) for r in recent]
        if left and right:
            a, b = sum(left) / len(left), sum(right) / len(right)
            changes[key] = {
                "reference_mean": a,
                "recent_mean": b,
                "relative_change": abs(b - a) / max(1, abs(a)),
            }
    a, b = brier(reference), brier(recent)
    sufficient = min(len(reference), len(recent)) >= 20
    drift = sufficient and (
        any(v["relative_change"] > 0.5 for v in changes.values())
        or (a is not None and b is not None and b > a + 0.05)
    )
    return {
        "drift_detected": bool(drift),
        "reference_brier": a,
        "recent_brier": b,
        "features": changes,
        "reference_rows": len(reference),
        "recent_rows": len(recent),
        "action": "review_or_restore_baseline"
        if drift
        else "retain_and_monitor"
        if sufficient
        else "insufficient_data",
    }


def trial_report(assignments, events, *, now, goal, window_days=14, baseline_rate=0.1, minimum_effect=0.05):
    unique = {}
    for assignment in sorted(assignments, key=lambda a: a.get("assigned_at", "")):
        unique.setdefault(assignment["lead_id"], assignment)
    events_by_lead = {}
    for event in events:
        if event.get("kind") == goal and not event.get("retracted"):
            events_by_lead.setdefault(event.get("lead_id"), []).append(timestamp(event["occurred_at"]))
    arms, pending = {}, 0
    for arm in ("control", "candidate"):
        selected = [a for a in unique.values() if a["arm"] == arm]
        matured = [a for a in selected if timestamp(a["assigned_at"]) + timedelta(days=window_days) <= now]

        def converted(assignment):
            start = timestamp(assignment["assigned_at"])
            return any(
                start <= at <= min(now, start + timedelta(days=window_days))
                for at in events_by_lead.get(assignment["lead_id"], [])
            )

        total_positive = sum(converted(a) for a in selected)
        positive = sum(converted(a) for a in matured)
        low, high = wilson(positive, len(matured))
        arms[arm] = {
            "leads": len(selected),
            "matured_leads": len(matured),
            "conversions": total_positive,
            "matured_conversions": positive,
            "rate": positive / len(matured) if matured else None,
            "confidence_low": low,
            "confidence_high": high,
            "coverage_rate": sum(bool(a.get("covered")) for a in matured) / len(matured) if matured else None,
        }
        pending += len(selected) - len(matured)
    base = min(0.95, max(0.01, baseline_rate))
    target = min(0.99, base + minimum_effect)
    pooled = (base + target) / 2
    required = math.ceil(
        (
            (
                NormalDist().inv_cdf(0.975) * math.sqrt(2 * pooled * (1 - pooled))
                + NormalDist().inv_cdf(0.8) * math.sqrt(base * (1 - base) + target * (1 - target))
            )
            / (target - base)
        )
        ** 2
    )
    a, b = arms["control"], arms["candidate"]
    difference = {
        "low": b["confidence_low"] - a["confidence_high"],
        "high": b["confidence_high"] - a["confidence_low"],
    }
    sufficient = min(a["matured_leads"], b["matured_leads"]) >= required and difference["low"] > 0
    policy_unknown = sum(a.get("policy_unknown", 1) for a in unique.values())
    violations = sum(a.get("policy_violations", 0) for a in unique.values())
    coverage_delta = (b["coverage_rate"] or 0) - (a["coverage_rate"] or 0)
    sufficient = sufficient and policy_unknown == 0 and violations == 0 and coverage_delta >= 0
    return {
        "arms": arms,
        "pending_leads": pending,
        "goal": goal,
        "window_days": window_days,
        "difference_interval": difference,
        "required_per_arm": required,
        "planned_power": 0.8,
        "minimum_detectable_absolute_effect": minimum_effect,
        "sufficient_evidence": sufficient,
        "coverage_delta": coverage_delta,
        "policy_violations": violations,
        "policy_unknown": policy_unknown,
        "evidence_scope": "observed controlled cohort; no automatic causal guarantee",
    }


def stable_control(lead_id):
    return int(hashlib.sha256(lead_id.encode()).hexdigest()[:8], 16) % 10 == 0


def choose_cohort_candidate(candidates, *, now, draw):
    urgent = [
        r
        for r in candidates
        if r.get("requested_contact")
        or timestamp(r.get("callback_due_at"))
        and timestamp(r["callback_due_at"]) <= now
        or timestamp(r.get("coverage_due_at"))
        and timestamp(r["coverage_due_at"]) <= now
    ]
    if urgent:
        from app.scheduling.policy import rank_candidates

        selected = rank_candidates(urgent, now)[0]
        probability, reason = 1.0, "callback_requested_contact_or_coverage"
    else:
        control = sorted(
            [r for r in candidates if r["arm"] == "control"], key=lambda r: (-r["p_human"], r["id"])
        )
        candidate = sorted(
            [r for r in candidates if r["arm"] == "candidate"], key=lambda r: (-r["score"], r["id"])
        )
        if not control and not candidate:
            return None
        if control and candidate:
            selected = control[0] if draw < 0.1 else candidate[0]
            probability = 0.1 if selected["arm"] == "control" else 0.9
        else:
            selected = (control or candidate)[0]
            probability = 1.0
        reason = "fixed_control_capacity_then_within_group_expected_value"
    return {
        "lead_id": selected["id"],
        "lead_selection_probability": probability,
        "selection_reason": reason,
        "eligible_lead_ids": [r["id"] for r in candidates],
    }
