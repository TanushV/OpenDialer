"""Bounded historical fixture adapters; production reads corrected contribution counts.

Offline oracles that were once test-only application functions live here, built from the
same production count definitions.
"""

from datetime import UTC, datetime

from app.ml.training import partition_at
from app.optimization.experiments import (
    call_goal_events,
    comparison_from_counts,
    exposure_values,
    reward_values,
    strategy_rewards,
)
from app.optimization.learning import (
    distribution_from_counts,
    drift_from_counts,
    drift_stratum,
    drift_values,
    reward_counts,
    trial_from_counts,
    trial_values,
)
from app.optimization.numbering import timestamp


def learn_distribution(actions, history, *, segment=None, exploration=0.1, control=False, urgent=False):
    return distribution_from_counts(
        actions,
        reward_counts((r for r in history if r.get("action") in actions), segment=segment),
        exploration=exploration,
        control=control,
        urgent=urgent,
    )


def drift_report(reference, recent):
    groups = []
    for rows in (reference, recent):
        counts = {}
        for row in rows:
            group = counts.setdefault(drift_stratum(row), {})
            for key, value in drift_values(row).items():
                group[key] = group.get(key, 0) + value
        groups.append(counts)
    return drift_from_counts(*groups)


def trial_report(assignments, events, *, now, goal, window_days=14, baseline_rate=0.1, minimum_effect=0.05):
    unique, by_lead, counts = {}, {}, {}
    for assignment in sorted(assignments, key=lambda a: a.get("assigned_at", "")):
        unique.setdefault(assignment["lead_id"], assignment)
    for event in events:
        by_lead.setdefault(event.get("lead_id"), []).append(event)
    for assignment in unique.values():
        for key, value in trial_values(
            assignment, by_lead.get(assignment["lead_id"], []), now=now, goal=goal, window_days=window_days
        ).items():
            counts[key] = counts.get(key, 0) + value
    return trial_from_counts(
        counts, goal=goal, window_days=window_days, baseline_rate=baseline_rate, minimum_effect=minimum_effect
    )


def independent_partitions(rows, *, label=None):
    ordered = sorted(rows, key=lambda r: timestamp(r["at"]))
    if len(ordered) < 3:
        return [], [], []
    cut, calibration_cut = int(len(ordered) * 0.6), int(len(ordered) * 0.8)
    cutoffs = (
        timestamp(ordered[cut]["at"]),
        timestamp(ordered[calibration_cut]["at"]),
        datetime.max.replace(tzinfo=UTC),
    )
    return tuple(partition_at(ordered, label, cutoffs)[0])


def comparison_report(
    study,
    assignments,
    calls,
    events,
    feedback,
    *,
    now,
    goal,
    window_days,
    exposures=(),
    seed_rewards=(),
    observations=(),
):
    """Bounded offline adapter. Operational publication reads persisted contributions."""
    events = events + call_goal_events(calls, goal, now=now)
    selected = {}
    for row in sorted(assignments, key=lambda r: (r["assigned_at"], r.get("id", ""))):
        if row.get("comparison_id") == study["id"]:
            selected.setdefault(row["lead_id"], row)
    values, grouped = {}, {}

    def add(target, vector):
        for key, value in vector.items():
            target[key] = target.get(key, 0) + value

    for lead_id, item in selected.items():
        attempts = [
            r
            for r in calls
            if r["lead_id"] == lead_id
            and r.get("submitted_at")
            and timestamp(item["assigned_at"]) <= timestamp(r["started_at"]) <= now
        ]
        due = timestamp(item.get("coverage_due_at"))
        enriched = {
            **item,
            "covered": any(not due or timestamp(r["started_at"]) <= due for r in attempts),
            "policy_unknown": sum(r.get("submission_allowed") is None for r in attempts),
            "policy_violations": sum(r.get("submission_allowed") is False for r in attempts),
        }
        vector = trial_values(enriched, events, now=now, goal=goal, window_days=window_days)
        add(values, vector)
        field = "cluster" if study["family"] == "number_allocation" else "rep_id"
        add(grouped.setdefault(item.get(field, "unknown"), {}), vector)
    arm_by_call = {r["id"]: selected[r["lead_id"]]["arm"] for r in calls if r["lead_id"] in selected}
    for row in feedback:
        arm = arm_by_call.get(row["call_id"])
        if arm is None:
            continue
        vector = {f"quality:{arm}:reviewed_cards": 1}
        for key in ("useful", "grounded", "timely", "distracting"):
            vector[f"quality:{arm}:{key}_count"] = int(isinstance(row.get(key), bool))
            vector[f"quality:{arm}:{key}_true"] = int(row.get(key) is True)
        add(values, vector)
    unique = {}
    for reward in [*seed_rewards, *strategy_rewards(study, list(selected.values()), events, now=now)]:
        unique.setdefault(reward["lead_id"], reward)
    for reward in unique.values():
        add(values, reward_values(reward))
    for row in exposures:
        if row.get("strategy_exposure", {}).get("comparison_id") == study["id"]:
            add(values, {"exposure:" + k: v for k, v in exposure_values(row, observations, now=now).items()})
    return comparison_from_counts(
        {**study, "goal": goal, "window_days": window_days}, values, grouped, now=now
    )
