import hashlib
import math
from datetime import timedelta
from statistics import NormalDist
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.optimization.conversations import conversation_associations as conversation_associations
from app.optimization.numbering import timestamp
from app.scheduling.models import (
    OPTIMIZATION_DEFAULTS,
    EstimatePolicy,
    attempt_work_seconds,
    derive_labels,
    pooled_predictions,
    wilson,
)

GOALS = {"qualified_conversation", "meeting_held", "sale_won"}
# The one declared two-arm allocation (EXECUTION_SPEC E08.13: default 50/50). It drives the
# stable cohort, model-trial enrollment and the logged selection probability alike.
CONTROL_FRACTION = 0.5


def hashed_draw(key):
    """Deterministic uniform draw in [0, 1): the one randomization primitive; log its probability."""
    return int(hashlib.sha256(key.encode()).hexdigest()[:16], 16) / 2**64


def outcome_at(call, now):
    """Only observed dispositions; a later correction is not historical knowledge."""
    versions = [*call.get("outcome_history", []), call]
    known = []
    for version in versions:
        available = timestamp(version.get("outcome_available_at"))
        if not version.get("outcome") or version.get("outcome_source") == "llm_provisional":
            continue
        # An unknown availability time is excluded, never assumed known at the cutoff.
        if not available or available > now:
            continue
        known.append({**version, "available": available, "timing_known": True})
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
        if not occurred or occurred > now or not available or available > now:
            continue
        if version.get("source") == "llm_provisional":
            continue
        # A retraction without its own time takes effect when its version became available.
        retracted_at = timestamp(version.get("retracted_at")) or available
        retracted = bool(version.get("retracted")) and retracted_at <= now
        known.append(
            {
                **version,
                "id": event["id"],
                "available": max(occurred, available),
                "timing_known": True,
                "retracted": retracted,
                "correction_available": retracted_at if retracted else None,
            }
        )
    return max(reversed(known), key=lambda value: value["available"], default=None)


def freeze_features(lead, *, health, now, calls=()):
    from app.optimization.conversations import prior_features

    try:
        local = now.astimezone(ZoneInfo(lead.get("timezone") or "unknown"))
        time_known = True
    except ZoneInfoNotFoundError:
        local = now
        time_known = False
    previous = [
        timestamp(c["started_at"])
        for c in calls
        if c.get("lead_id") == lead.get("id") and timestamp(c["started_at"]) <= now
    ]
    last = max(previous, default=timestamp(lead.get("last_attempt_at")))
    created = timestamp(lead.get("source_at") or lead.get("created_at")) or now
    return {
        "feature_schema_version": 2,
        "decision_at": now.isoformat(),
        "local_time_uncertain": not time_known,
        **prior_features(lead, calls, now=now),
        "segment": lead.get("segment", "unknown"),
        "local_hour": local.hour if time_known else None,
        "local_weekday": local.weekday() if time_known else None,
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


def training_rows(calls, snapshots, events, *, now, goal, window_days, include_unclassified=False):
    """As-of rows. Negatives mature only after window close with a classified outcome and known
    attribution; an unattributed lead outcome credits exactly the latest earlier attempt."""
    if goal not in GOALS or not 1 <= window_days <= 180:
        raise ValueError("Reviewed conversion goal and bounded outcome window required")
    by_id = {r["id"]: r for r in snapshots}
    known_events = {}
    for event in events:
        known = conversion_at(event, now)
        if known and known.get("kind") == goal:
            key = (known.get("lead_id"), known.get("call_id") or None)
            known_events.setdefault(key, []).append(known)
    window = timedelta(days=window_days)
    starts, credited = {}, {}
    for call in calls:
        at = timestamp(call.get("started_at"))
        if at and at <= now:
            starts.setdefault(call.get("lead_id"), []).append((at, call["id"]))
    for (lead_id, call_id), versions in known_events.items():
        if call_id is None:
            for event in versions:
                occurred = timestamp(event["occurred_at"])
                touching = [(at, c) for at, c in starts.get(lead_id, []) if at <= occurred <= at + window]
                credited[event["id"]] = max(touching)[1] if touching else None
    result = []
    for call in sorted(calls, key=lambda c: c.get("started_at", "")):
        at = timestamp(call.get("started_at"))
        snapshot = by_id.get(call.get("feature_snapshot_id"))
        if not at or at > now or not snapshot or timestamp(snapshot.get("as_of")) is None:
            continue
        outcome = outcome_at(call, now)
        classified = outcome is not None
        if not outcome and include_unclassified and timestamp(call.get("submitted_at")):
            outcome = {"outcome": None, "available": None, "outcome_source": "unobserved"}
        if timestamp(snapshot["as_of"]) > at or not outcome:
            continue
        end = at + window

        def inside(event, end=end, at=at):
            return at <= timestamp(event["occurred_at"]) <= min(now, end)

        lead_level = [e for e in known_events.get((call["lead_id"], None), []) if inside(e)]
        relevant = [e for e in lead_level if credited.get(e["id"]) == call["id"]] + [
            e for e in known_events.get((call["lead_id"], call["id"]), []) if inside(e)
        ]
        ambiguous = any(not e["retracted"] and credited.get(e["id"]) != call["id"] for e in lead_level)
        matched = [e for e in relevant if not e["retracted"]]
        labels = derive_labels(outcome)
        direct = bool(labels.get(goal if goal != "qualified_conversation" else "qualified"))
        positive = bool(matched or direct)
        closed = now >= end
        negative = closed and classified and not ambiguous and not positive
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
            if negative
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
                "outcome_available_at": outcome["available"].isoformat() if classified else None,
                "label_available_at": label_available.isoformat() if label_available else None,
                "label_timing_quality": "observed" if classified else "observation_time_unknown",
                "conversion": 1 if positive else 0 if negative else None,
                "attribution": "ambiguous_lead_level_outcome" if ambiguous and not positive else "known",
                "goal": goal,
                "window_status": "matured" if closed else "resolved" if positive else "pending",
                "window_closed": closed,
                "window_ends_at": end.isoformat(),
                "conversion_event_ids": sorted({e["id"] for e in matched}),
                "action": call.get("treatment_variant_id", "approved-default"),
                "segment": snapshot["features"].get("segment", "unknown"),
                "local_hour": snapshot["features"].get("local_hour"),
                "local_weekday": snapshot["features"].get("local_weekday"),
                "attempt_count": snapshot["features"].get("attempt_count"),
                "rep_work_seconds": attempt_work_seconds(call),
                "duration_seconds": attempt_work_seconds(call),
                "contact_role": outcome.get("contact_role"),
                "company_id": snapshot.get("company_id", call.get("company_id")),
                "propensity": snapshot.get("propensity", 1.0),
                "p_human": snapshot.get("predictions", {}).get("p_human"),
                "p_right_party_given_human": snapshot.get("predictions", {}).get("p_right_party_given_human"),
                "p_conversion_given_right_party": snapshot.get("predictions", {}).get(
                    "p_conversion_given_right_party"
                ),
                "model_version": snapshot.get("model_version")
                or snapshot.get("predictions", {}).get("model_version"),
                "experiment_arm": snapshot.get("experiment_arm", "baseline"),
                "observed_cost_usd": call.get("observed_cost_usd"),
                "cost_status": call.get("cost_status", "unknown"),
            }
        )
    return result


def funnel_predictions(
    history, features, *, handling_seconds=OPTIMIZATION_DEFAULTS["handling_seconds"], estimate_policy=None
):
    return pooled_predictions(
        history, features=features, handling_seconds=handling_seconds, estimate_policy=estimate_policy
    )


def prior_means(estimate_policy=None):
    policy = EstimatePolicy.model_validate(estimate_policy or {})
    return tuple(a / (a + b) for a, b in (policy.human_prior, policy.right_prior, policy.goal_prior))


_PRIOR_MEANS = prior_means()


def conversion_score(prediction, estimate_policy=None):
    """Rank value p_h * p_r * p_g / max(10, work); a missing stage uses the policy's prior mean."""
    means = _PRIOR_MEANS if estimate_policy is None else prior_means(estimate_policy)
    return (
        prediction.get("p_human", means[0])
        * prediction.get("p_right_party_given_human", means[1])
        * prediction.get("p_conversion_given_right_party", means[2])
        / max(10, prediction.get("expected_rep_seconds", OPTIMIZATION_DEFAULTS["handling_seconds"]))
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


def reward_counts(history, *, segment=None):
    """The one IPW reward definition (weight 1/propensity, capped at 20).

    A logged propensity must be a finite probability in (0, 1]; anything else is rejected and
    counted, never coerced into certainty.
    """
    counts = {}
    for row in history:
        if row.get("window_status") != "matured" or row.get("conversion") is None or not row.get("action"):
            continue
        action, propensity = row["action"], row.get("propensity")
        if type(propensity) not in (int, float) or not math.isfinite(propensity) or not 0 < propensity <= 1:
            vector = {"rejected_propensity": 1}
        else:
            weight = min(20.0, 1 / propensity)
            vector = {"all_n": 1, "all_success": row["conversion"]}
            if segment is None or row.get("segment") == segment:
                vector.update(n=1, weight=weight, weighted_success=weight * row["conversion"])
        for key, value in vector.items():
            name = f"reward:{action}:{key}"
            counts[name] = counts.get(name, 0) + value
    return counts


def distribution_from_counts(actions, counts, *, exploration=0.1, control=False, urgent=False):
    """The same finite strategy estimate for incremental and offline evidence."""
    if not actions or len(actions) != len(set(actions)) or not 0 <= exploration <= 0.3:
        raise ValueError("Unique approved actions and exploration between zero and 0.3 required")
    if control or urgent:
        return {
            "best": actions[0],
            "probabilities": {a: float(a == actions[0]) for a in actions},
            "policy_version": "protected-default-v2",
            "observations": 0,
        }
    estimates, observations = {}, {}
    for action in actions:
        values = {
            k: counts.get(f"reward:{action}:{k}", 0)
            for k in ("all_n", "all_success", "n", "weight", "weighted_success")
        }
        prior = (values["all_success"] + 1) / (values["all_n"] + 5)
        estimates[action] = (values["weighted_success"] + 10 * prior) / (values["weight"] + 10)
        observations[action] = values["n"]
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


DRIFT_POLICY = {
    "version": "comparable-stages-v2",
    "minimum_support": 20,
    "brier_increase": 0.05,
    "missing_fraction_change": 0.20,
}


DRIFT_FEATURES = (
    "local_hour",
    "attempt_count",
    "source_age_days",
    "number_health_missing",
    "conversation_evidence_missing",
)
DRIFT_STAGES = (
    ("human", "human", "p_human", None),
    ("right_party", "right_party", "p_right_party_given_human", "human"),
    ("conversion", "conversion", "p_conversion_given_right_party", "right_party"),
)


def drift_stratum(row):
    if not row.get("model_version"):
        return None
    f = row.get("features", {})
    return (
        row["model_version"],
        row.get("campaign_id"),
        row.get("goal"),
        f.get("feature_schema_version"),
        f.get("segment"),
        f.get("attempt_count"),
    )


def drift_values(row):
    """Additive counts/losses; no row identities or conversation content."""

    def numeric(v):
        return isinstance(v, (int, float)) and math.isfinite(v)

    values = {"rows": 1}
    for key in DRIFT_FEATURES:
        value = row.get("features", {}).get(key)
        values[key + ":n"] = int(numeric(value))
        values[key + ":sum"] = float(value) if numeric(value) else 0
    for stage, label, probability, condition in DRIFT_STAGES:
        known = (
            row.get(label) in (0, 1)
            and numeric(row.get(probability))
            and 0 <= row[probability] <= 1
            and (condition is None or row.get(condition) == 1)
            and (stage != "conversion" or row.get("window_closed") or row.get("window_status") == "matured")
        )
        values[stage + ":n"] = int(bool(known))
        values[stage + ":loss"] = (row[probability] - row[label]) ** 2 if known else 0
    return values


def drift_from_counts(reference, recent):
    """Same paired-stratum review policy for stored totals and offline fixtures."""
    groups = (reference, recent)
    common = {
        key
        for key in reference.keys() & recent.keys()
        if key is not None and reference[key].get("rows", 0) > 0 and recent[key].get("rows", 0) > 0
    }
    totals = [sum(v.get("rows", 0) for v in group.values()) for group in groups]
    compared = [sum(group[key].get("rows", 0) for key in common) for group in groups]
    changes = {}
    for field in DRIFT_FEATURES:
        n = [sum(group[key].get(field + ":n", 0) for key in common) for group in groups]
        sums = [sum(group[key].get(field + ":sum", 0) for key in common) for group in groups]
        means = [v / count if count else None for v, count in zip(sums, n, strict=True)]
        missing = [count - known for count, known in zip(compared, n, strict=True)]
        changes[field] = {
            "reference_mean": means[0],
            "recent_mean": means[1],
            "reference_missing": missing[0],
            "recent_missing": missing[1],
            "missing_fraction_change": abs(missing[1] / compared[1] - missing[0] / compared[0])
            if all(compared)
            else None,
        }
    support = {}
    for stage, _, _, _ in DRIFT_STAGES:
        ns, weighted, weight = [0, 0], [0.0, 0.0], 0
        for key in common:
            counts = [group[key].get(stage + ":n", 0) for group in groups]
            for i, n in enumerate(counts):
                ns[i] += n
            paired = min(counts)
            if paired >= DRIFT_POLICY["minimum_support"]:
                weight += paired
                for i, group in enumerate(groups):
                    weighted[i] += paired * group[key].get(stage + ":loss", 0) / counts[i]
        support[stage] = {
            "reference": ns[0],
            "recent": ns[1],
            "paired_support": weight,
            "reference_brier": weighted[0] / weight if weight else None,
            "recent_brier": weighted[1] / weight if weight else None,
        }
    sufficient = any(v["paired_support"] for v in support.values())
    missing_shift = any(
        v["missing_fraction_change"] is not None
        and v["missing_fraction_change"] >= DRIFT_POLICY["missing_fraction_change"]
        for v in changes.values()
    )
    drift = bool(
        sufficient
        and any(
            v["paired_support"] and v["recent_brier"] > v["reference_brier"] + DRIFT_POLICY["brier_increase"]
            for v in support.values()
        )
    )
    return {
        "definition_version": DRIFT_POLICY["version"],
        "policy": dict(DRIFT_POLICY),
        "model_versions": sorted({k[0] for k in common}),
        "stage_support": support,
        "drift_detected": drift,
        "missingness_shift": missing_shift,
        "reference_brier": support["human"]["reference_brier"],
        "recent_brier": support["human"]["recent_brier"],
        "features": changes,
        "reference_rows": totals[0],
        "recent_rows": totals[1],
        "excluded_reference": totals[0] - compared[0],
        "excluded_recent": totals[1] - compared[1],
        "action": "insufficient_comparable_data"
        if not common
        else "insufficient_data"
        if not sufficient
        else "review_or_restore_baseline"
        if drift
        else "review_data_coverage"
        if missing_shift
        else "retain_and_monitor",
        "scope": "Comparable model, campaign, goal, feature version, segment and attempt strata; equal stratum weights across periods. Descriptive warning; no automatic activation.",
    }


def trial_values(assignment, events, *, now, goal, window_days):
    """Intent-to-treat counts from evidence known by now, as strategy rewards are."""
    start = timestamp(assignment["assigned_at"])
    end = start + timedelta(days=window_days)
    converted = any(
        known["lead_id"] == assignment["lead_id"]
        and known.get("kind") == goal
        and not known["retracted"]
        and start <= timestamp(known["occurred_at"]) <= min(now, end)
        for event in events
        if event.get("lead_id") == assignment["lead_id"]
        and (known := conversion_at({"id": None, **event}, now))
    )
    matured = end <= now
    prefix = assignment["arm"] + ":"
    return {
        prefix + "leads": 1,
        prefix + "matured_leads": int(matured),
        prefix + "conversions": int(converted),
        prefix + "matured_conversions": int(matured and converted),
        prefix + "covered_matured": int(matured and bool(assignment.get("covered"))),
        "policy_unknown": assignment.get("policy_unknown", 1),
        "policy_violations": assignment.get("policy_violations", 0),
    }


def trial_from_counts(values, *, goal, window_days=14, baseline_rate=0.1, minimum_effect=0.05):
    arms, pending = {}, 0
    for arm in ("control", "candidate"):
        counts = {
            key: values.get(arm + ":" + key, 0)
            for key in ("leads", "matured_leads", "conversions", "matured_conversions", "covered_matured")
        }
        matured = counts["matured_leads"]
        low, high = wilson(counts["matured_conversions"], matured)
        arms[arm] = {
            **{key: value for key, value in counts.items() if key != "covered_matured"},
            "rate": counts["matured_conversions"] / matured if matured else None,
            "confidence_low": low,
            "confidence_high": high,
            "coverage_rate": counts["covered_matured"] / matured if matured else None,
        }
        pending += counts["leads"] - matured
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
    policy_unknown = values.get("policy_unknown", 0)
    violations = values.get("policy_violations", 0)
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


def stable_control(lead_id, control_fraction=CONTROL_FRACTION):
    return hashed_draw("cohort:" + lead_id) < control_fraction


def cohort_arm(lead_id):
    """The one optimization arm definition for enrollment, serving and decision records."""
    return "control" if stable_control(lead_id) else "candidate"


def choose_cohort_candidate(candidates, *, now, draw=None, control_fraction=CONTROL_FRACTION):
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
            if draw is None:
                draw = hashed_draw(f"selection:{now.isoformat()}:{control[0]['id']}:{candidate[0]['id']}")
            selected = control[0] if draw < control_fraction else candidate[0]
            probability = control_fraction if selected["arm"] == "control" else 1 - control_fraction
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
