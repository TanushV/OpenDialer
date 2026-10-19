"""Shared observed labels and inexpensive sparse estimates; no ML runtime imports."""

import hashlib
import json
import math
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

OutcomeCode = Literal[
    "right_party",
    "gatekeeper",
    "wrong_party",
    "voicemail",
    "no_answer",
    "busy",
    "screening",
    "ivr",
    "callback_requested",
    "meeting_booked",
    "meeting_held",
    "not_interested",
    "opt_out",
    "invalid_endpoint",
    "temporary_failure",
    "qualified_conversation",
    "sale_won",
]
ContactRole = Literal["right_party", "gatekeeper", "wrong_party", "unknown"]

OUTCOME_LABELS = {
    "right_party": (True, True),
    "qualified_conversation": (True, True),
    "gatekeeper": (True, False),
    "wrong_party": (True, False),
    "callback_requested": (True, None),
    "meeting_booked": (True, None),
    "meeting_held": (True, None),
    "sale_won": (True, None),
    "not_interested": (True, None),
    "opt_out": (True, None),
    "no_answer": (False, None),
    "busy": (False, None),
    "voicemail": (False, None),
    "screening": (False, None),
    "ivr": (False, None),
    "temporary_failure": (None, None),
    "invalid_endpoint": (None, None),
}
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class EstimatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    human_prior: tuple[Positive, Positive] = (2, 8)
    right_prior: tuple[Positive, Positive] = (1, 2)
    goal_prior: tuple[Positive, Positive] = (1, 3)
    shrinkage: Positive = 10
    work_prior: Positive = 5

    @property
    def version(self):
        return (
            "estimates-v2:"
            + hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()[
                :16
            ]
        )


def prediction_key(features):
    day = features.get("local_weekday")
    return json.dumps(
        [
            features.get("segment", "unknown"),
            features.get("local_hour"),
            None if day is None else day >= 5,
            features.get("attempt_count"),
        ],
        separators=(",", ":"),
    )


def company_group(lead):
    """Conservative evaluation grouping from supplied identity, never a model input."""
    for field in ("company_id", "domain", "company"):
        value = " ".join(str(lead.get(field) or "").casefold().split())
        if value and value != "unknown":
            return field + ":" + hashlib.sha256(value.encode()).hexdigest()
    return None


def saved_prediction(profile, features, config):
    """A stale configuration never silently supplies a differently defined prior."""
    policy = EstimatePolicy.model_validate(config.get("estimate_policy", {}))
    work = config.get("handling_seconds", 120)
    if (
        profile.get("goal") == config.get("conversion_goal")
        and profile.get("estimate_policy_version") == policy.version
        and profile.get("work_assumption_seconds") == work
    ):
        found = profile.get("predictions", {}).get(prediction_key(features)) or profile.get("pooled")
        if found:
            return dict(found)
    return pooled_predictions([], features=features, handling_seconds=work, estimate_policy=policy)


def derive_labels(value):
    row = value if isinstance(value, dict) else {"outcome": value}
    code = row.get("outcome")
    source = row.get("outcome_source") or row.get("source") or "legacy_unknown"
    confirmed = source != "llm_provisional" and code in OUTCOME_LABELS
    human, right = OUTCOME_LABELS.get(code, (None, None)) if confirmed else (None, None)
    role = row.get("contact_role")
    if human is True and role in {"right_party", "wrong_party", "gatekeeper"}:
        right = role == "right_party"
    return {
        "human": human,
        "right_party": right,
        "qualified": code == "qualified_conversation" if confirmed else None,
        "meeting_held": code == "meeting_held" if confirmed else None,
        "sale_won": code == "sale_won" if confirmed else None,
        "outcome_source": source,
        "label_timing_quality": "observed" if row.get("outcome_available_at") else "observation_time_unknown",
    }


def contact_labels(row):
    labels = derive_labels(row)
    return labels["human"], labels["right_party"]


def wilson(successes, total, *, z=1.96):
    if not total:
        return (0.0, 1.0)
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def _finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _instant(value):
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        return at.replace(tzinfo=UTC) if at and at.tzinfo is None else at
    except (ValueError, AttributeError, TypeError):
        return None


def attempt_work_seconds(row):
    """Union observed occupancy; detached playback and missing time are not rep work."""
    if row.get("rep_work_incomplete"):
        return None
    start = _instant(row.get("submitted_at") or (row.get("started_at") if row.get("provider_id") else None))
    end = _instant(row.get("rep_released_at") or row.get("ended_at"))
    if row.get("detached") and not row.get("rep_released_at"):
        return None
    spans = []
    if start and end and end >= start:
        spans.append((start, end))
    for interval in row.get("rep_work_intervals", []):
        a, b = _instant(interval.get("started_at")), _instant(interval.get("ended_at"))
        if a and b and b >= a:
            spans.append((a, b))
    if not spans:
        value = row.get("rep_work_seconds", row.get("duration_seconds"))
        return float(value) if _finite(value) else None
    total, previous = 0.0, None
    for a, b in sorted(spans):
        total += max(0, (b - max(a, previous or a)).total_seconds())
        previous = max(b, previous or b)
    return total


def _cell(rows, features):
    segment = features.get("segment")
    if not segment or segment == "unknown":
        return set(range(len(rows))), "pooled"
    local = {i for i, r in enumerate(rows) if r.get("segment") == segment}
    hour, day = features.get("local_hour"), features.get("local_weekday")
    if (
        hour is None
        or day is None
        or not any(
            rows[i].get("local_hour") is not None and rows[i].get("local_weekday") is not None for i in local
        )
    ):
        return local, "segment"
    timed = {
        i
        for i in local
        if rows[i].get("local_hour") is not None
        and rows[i].get("local_weekday") is not None
        and min(abs(rows[i]["local_hour"] - hour), 24 - abs(rows[i]["local_hour"] - hour)) <= 2
        and (rows[i]["local_weekday"] >= 5) == (day >= 5)
    }
    attempt = features.get("attempt_count")
    if attempt is not None and any(rows[i].get("attempt_count") is not None for i in timed):
        timed = {i for i in timed if rows[i].get("attempt_count") == attempt}
        return timed, "segment_time_attempt"
    return timed, "segment_time"


def _weighted_mean(values):
    """Normalize positive finite weights before arithmetic to avoid overflow."""
    scale = max(weight for _, weight in values)
    total = math.fsum(weight / scale for _, weight in values)
    return math.fsum(value * ((weight / scale) / total) for value, weight in values)


def _stage_counts(rows, indexes, stage):
    values, missing, excluded = [], 0, 0
    for i in indexes:
        row = rows[i]
        conditioning = (
            True if stage == "human" else row.get("human" if stage == "right_party" else "right_party")
        )
        if conditioning != 1:
            excluded += 1
            continue
        value = row.get(stage)
        if stage == "conversion" and not (
            row.get("window_closed") or row.get("window_status") in {"resolved", "matured"}
        ):
            value = None
        if value is not None and value in (0, 1):
            values.append(int(value))
        else:
            missing += 1
    return sum(values), len(values), missing, excluded


def pooled_predictions(
    history, *, segment=None, hour=None, features=None, handling_seconds=120, estimate_policy=None
):
    policy = EstimatePolicy.model_validate(estimate_policy or {})
    features = features if features is not None else {"segment": segment, "local_hour": hour}
    if not _finite(handling_seconds) or handling_seconds < 10:
        raise ValueError("An explicit finite work assumption of at least 10 seconds is required")
    rows = []
    for item in history:
        labels = derive_labels(item)
        provisional = item.get("outcome_source", item.get("source")) == "llm_provisional"
        # Immutable dataset rows already have as-of labels. Never override provisional evidence.
        labels.update({key: item[key] for key in ("human", "right_party") if key in item and not provisional})
        rows.append({**item.get("features", {}), **item, **labels})
    cell, level = _cell(rows, features)
    outside = set(range(len(rows))) - cell
    support, predictions = {}, {}
    for stage, prior, key in (
        ("human", policy.human_prior, "p_human"),
        ("right_party", policy.right_prior, "p_right_party_given_human"),
        ("conversion", policy.goal_prior, "p_conversion_given_right_party"),
    ):
        s, n, missing, excluded = _stage_counts(rows, cell, stage)
        os, on, _, _ = _stage_counts(rows, outside, stage)
        a, b = prior
        pooled = _weighted_mean([(os / on if on else 0, on), (1, a), (0, b)])
        predictions[key] = (
            _weighted_mean([(s / n if n else 0, n), (1, a), (0, b)])
            if level == "pooled"
            else _weighted_mean([(s / n if n else 0, n), (pooled, policy.shrinkage)])
        )
        support[stage] = {
            "cell_known": n,
            "cell_successes": s,
            "cell_unknown": missing,
            "conditioning_excluded": excluded,
            "outside_known": on,
            "observed_interval": list(wilson(s, n)),
            "interval_kind": "observed_rate_wilson",
        }
    work = [v for i in cell if (v := attempt_work_seconds(rows[i])) is not None]
    costs = [
        rows[i]["observed_cost_usd"]
        for i in cell
        if _finite(rows[i].get("observed_cost_usd")) and rows[i].get("cost_status") == "settled"
    ]
    return {
        **predictions,
        "p_next_step_given_right_party": predictions["p_conversion_given_right_party"],
        "expected_rep_seconds": _weighted_mean(
            [(v, 1) for v in work] + [(handling_seconds, policy.work_prior)]
        ),
        "work_observations": len(work),
        "work_assumption_seconds": handling_seconds,
        "expected_attempt_cost_usd": sum(costs) / len(costs) if costs else None,
        "cost_observations": len(costs),
        "cost_unknown_observations": len(cell) - len(costs),
        "observations": support["human"]["cell_known"],
        "conversion_observations": support["conversion"]["cell_known"],
        "stage_support": support,
        "estimate_level": level,
        "estimate_policy_version": policy.version,
        "prior_only": not any(v["cell_known"] or v["outside_known"] for v in support.values()),
        "model_version": "pooled-funnel-v3",
    }
