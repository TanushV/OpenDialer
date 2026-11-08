"""Learning estimates, number health and call counts computed directly from calls (R8.4).

Selection reads pooled and per-cell success counts plus retry continuation; number evidence reads each number's
health; the learning report reads plain counts. Each is one pass over the selected campaign's calls when it is
asked for. There are no saved report views, buckets or refresh deadlines.
"""

import json
from datetime import timedelta

from sqlalchemy import select

from app.optimization.learning import outcome_at, retry_statistics
from app.optimization.numbering import health_from_counts, health_stratum, health_values, timestamp
from app.platform.db import MODELS, doc
from app.scheduling.models import derive_labels, estimate_counts

CELL_LIMIT = 1000
CELL_FIELDS = ("segment", "local_hour", "local_weekday", "attempt_count")


def _add(target, values):
    for name, value in values.items():
        target[name] = target.get(name, 0) + value


def estimate_statistics(rows, leads, *, now):
    """Bounded conditional cells with exact pooled support; never partial local rates.

    `rows` are training rows including submitted calls without a classified outcome; `leads` carry `suppressed`.
    """
    grouped = {}
    for row in rows:
        features = {k: row.get(k) for k in CELL_FIELDS}
        grouped.setdefault(json.dumps(features, sort_keys=True), (features, []))[1].append(row)
    cells = [
        {"features": features, "counts": estimate_counts(members)}
        for _, (features, members) in sorted(grouped.items())
    ]
    total = estimate_counts(rows)
    retry = retry_statistics(
        rows,
        leads,
        now=now,
        # Every observed attempt index counts; the planner applies the current allowance, not this bound.
        max_attempts=max((r["features"].get("attempt_count", 0) for r in rows), default=0) + 2,
    )
    return {
        "costs": {
            "cost_usd": total["cost_usd"],
            "cost_observations": total["cost_observations"],
            "cost_unknown_observations": total["cost_unknown_observations"],
            "level": "pooled_complete_calls",
        },
        "total": total,
        "cells": cells[:CELL_LIMIT],
        "cells_complete": len(cells) <= CELL_LIMIT,
        "retry_observations": retry if len(retry) <= CELL_LIMIT else [],
        "retry_complete": len(retry) <= CELL_LIMIT,
    }


def _health_observation(call, feature, now):
    known = outcome_at(call, now)
    failed = call.get("end_reason") in {"provider_failure", "route_failure"}
    if not known and not failed:
        return None
    feature = feature or {}
    return {
        **feature.get("features", {}),
        **call,
        **(known or {"outcome": None}),
        "feature_schema_version": feature.get(
            "feature_schema_version", feature.get("features", {}).get("feature_schema_version")
        ),
        "prediction_at": feature.get("as_of"),
        "expected_human": feature.get("predictions", {}).get("p_human"),
        "delivery_failed": failed,
    }


def number_health(calls, features, callbacks, *, policy, now):
    """Each number's descriptive health over the policy window, from its outbound calls and return calls."""
    counts, strata, expiry = {}, {}, {}
    window = timedelta(days=policy["window_days"])
    for call in calls:
        if not call.get("number_id") or call.get("inbound"):
            continue
        observation = _health_observation(call, features.get(call.get("feature_snapshot_id")), now)
        values = health_values(observation, now=now, policy=policy) if observation else {}
        if not values:
            continue
        _add(counts.setdefault(call["number_id"], {}), values)
        if values.get("comparable_samples"):
            strata.setdefault(call["number_id"], set()).add(health_stratum(observation))
        if start := timestamp(call.get("started_at")):
            # Saved health stays valid until its oldest call leaves the window.
            expiry[call["number_id"]] = min(start + window, expiry.get(call["number_id"], start + window))
    for task in callbacks:
        at = timestamp(task.get("occurred_at") or task.get("created_at"))
        if task.get("number_id") and at and now - window <= at <= now:
            _add(counts.setdefault(task["number_id"], {}), {"callbacks": 1})
    bounded = sum(map(len, strata.values())) <= CELL_LIMIT
    return {
        number_id: health_from_counts(
            values,
            now=now,
            policy=policy,
            comparison_strata=len(strata.get(number_id, ())) if bounded else None,
            next_expiry_at=expiry[number_id].isoformat() if number_id in expiry else None,
        )
        for number_id, values in counts.items()
    }


def call_counts(calls, *, goal, window_days, now):
    """Plain counts for the learning report: attempts, pickups, right-party answers and open outcome windows."""
    result = dict.fromkeys(
        (
            "attempts",
            "human_pickups",
            "human_classified_attempts",
            "right_party_successes",
            "pending_outcome_windows",
        ),
        0,
    )
    for call in calls:
        submitted = timestamp(
            call.get("submitted_at") or (call.get("started_at") if call.get("provider_id") else None)
        )
        if call.get("inbound") or not submitted or submitted > now:
            continue
        labels = derive_labels(outcome_at(call, now) or {})
        _add(
            result,
            {
                "attempts": 1,
                "human_pickups": int(labels["human"] is True),
                "human_classified_attempts": int(labels["human"] is not None),
                "right_party_successes": int(labels["human"] is True and labels["right_party"] is True),
                "pending_outcome_windows": int(now < submitted + timedelta(days=window_days)),
            },
        )
    return {**result, "outcome_goal": goal, "outcome_window_days": window_days}


def campaign_records(session, p, campaign_id):
    """The selected campaign's leads (with `suppressed`), calls, their feature snapshots and return calls."""
    leads_model, calls_model = MODELS["leads"], MODELS["call_attempts"]

    def scoped(table, *conditions):
        model = MODELS[table]
        query = select(model).where(model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"])
        return [doc(r) for r in session.scalars(query.where(*conditions))]

    leads = scoped("leads", leads_model.op_campaign_id == campaign_id)
    ids = [lead["id"] for lead in leads]
    calls = [
        c
        for i in range(0, len(ids), 500)
        for c in scoped("call_attempts", calls_model.op_lead_id.in_(ids[i : i + 500]))
    ]
    wanted = sorted({c.get("feature_snapshot_id") for c in calls} - {None})
    features = {
        r["id"]: r
        for i in range(0, len(wanted), 500)
        for r in scoped("feature_snapshots", MODELS["feature_snapshots"].id.in_(wanted[i : i + 500]))
    }
    phones = sorted({lead.get("phone") for lead in leads} - {None})
    denied = {
        r["phone"]
        for i in range(0, len(phones), 500)
        for r in scoped("suppressions", MODELS["suppressions"].external_key.in_(phones[i : i + 500]))
        if r.get("active", True)
    }
    callbacks = [t for t in scoped("followup_tasks") if t.get("inbound")]
    return {
        "leads": [{**lead, "suppressed": lead.get("phone") in denied} for lead in leads],
        "calls": calls,
        "features": features,
        "callbacks": callbacks,
    }
