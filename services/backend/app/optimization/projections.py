"""Replaceable source contributions for shared operational metrics.

Raw text never enters a vector. Lead uniqueness and rep-time union have their
own source units instead of being summed from individual call attempts.
"""

import math
import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from app.optimization.learning import conversion_at, outcome_at
from app.optimization.numbering import timestamp
from app.scheduling.models import derive_labels


def unit_key(definition, namespace, kind, identity):
    if (
        not re.fullmatch(r"[a-f0-9]{64}", definition)
        or not re.fullmatch(r"[a-zA-Z0-9-]{1,36}", namespace)
        or kind not in {"lead", "call", "rep-day", "usage", "help"}
        or not isinstance(identity, str)
        or not 1 <= len(identity) <= 128
    ):
        raise ValueError("Invalid scoped contribution identity")
    return f"v{definition}:{namespace}:{kind}:{identity}"


def lead_values(lead, calls, events, *, suppressed, config, now):
    original = bool(lead.get("originally_eligible", True))
    eligible = lead.get("status") == "eligible" and not suppressed and not lead.get("outreach_hold")
    label = (
        "qualified" if config["conversion_goal"] == "qualified_conversation" else config["conversion_goal"]
    )
    outcomes = [
        derive_labels(outcome_at(call, now) or {})
        for call in calls
        if call.get("lead_id") == lead["id"]
        and timestamp(call.get("started_at"))
        and timestamp(call["started_at"]) <= now
    ]
    known = [
        derive_labels(value["kind"])
        for event in events
        if event.get("lead_id") == lead["id"]
        and (value := conversion_at(event, now))
        and not value["retracted"]
    ]
    due = timestamp(lead.get("coverage_due_at"))
    return {
        "original_cohort": 1,
        "original_eligible_denominator": int(original),
        "eligible_now": int(bool(eligible)),
        "suppressed": int(suppressed),
        "right_party_unique": int(original and any(row["right_party"] is True for row in outcomes)),
        "held_meetings": int(original and any(row["meeting_held"] is True for row in outcomes + known)),
        "unique_conversions": int(original and any(row[label] is True for row in outcomes + known)),
        "coverage_overdue": int(bool(eligible and due and due < now and not lead.get("attempt_count"))),
    }


def call_values(call, *, config, now):
    submitted = timestamp(
        call.get("submitted_at") or (call.get("started_at") if call.get("provider_id") else None)
    )
    values = dict.fromkeys(
        (
            "attempts",
            "human_pickups",
            "human_classified_attempts",
            "human_unknown_attempts",
            "right_party_conditioning_count",
            "right_party_successes",
            "pending_outcome_windows",
        ),
        0,
    )
    if not submitted or submitted > now:
        return values
    labels = derive_labels(outcome_at(call, now) or {})
    values.update(
        attempts=1,
        human_pickups=int(labels["human"] is True),
        human_classified_attempts=int(labels["human"] is not None),
        human_unknown_attempts=int(labels["human"] is None),
        right_party_conditioning_count=int(labels["human"] is True and labels["right_party"] is not None),
        right_party_successes=int(labels["human"] is True and labels["right_party"] is True),
        pending_outcome_windows=int(now < submitted + timedelta(days=config["window_days"])),
    )
    return values


def _microusd(value):
    if isinstance(value, bool):
        raise ValueError("Money must be finite and nonnegative")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError("Money must be finite and nonnegative")
        return int((amount * 1_000_000).to_integral_value(rounding=ROUND_HALF_EVEN))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid monetary receipt") from exc


def usage_values(row):
    """Derived totals use micro-dollars; original receipt precision is untouched."""
    verified = bool(row.get("billing_verified"))
    gross = _microusd(row["cost_usd"]) if verified else 0
    credited = _microusd(row.get("credited_usd", 0))
    if credited > gross and verified:
        raise ValueError("Credit exceeds the original verified charge")
    return {
        "usage_count": 1,
        "unverified_usage_count": int(not verified),
        "reserved_microusd": _microusd(row.get("reserved_usd", 0)),
        "gross_microusd": gross,
        "credited_microusd": credited,
        "verified_microusd": gross - credited if verified else 0,
    }


def rep_day_values(intervals, calls, *, rep_id, day, now):
    from app.measurement import work_summary

    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    work = work_summary(
        [row for row in intervals if row.get("rep_id") == rep_id],
        [row for row in calls if row.get("rep_id") == rep_id],
        now=now,
        start_at=start,
        end_at=start + timedelta(days=1),
    )
    return {
        "rep_work_seconds": work["seconds"] or 0,
        "work_evidence_count": int(rep_id in work["by_rep"]),
        "stale_intervals": work["stale_intervals"],
    }


def help_values(event, feedback, *, now):
    feedback = feedback or {}
    exposure = event.get("strategy_exposure", {})
    generated = event.get("action", "SHOW_CARD") == "SHOW_CARD"
    at = timestamp(exposure.get("displayed_at"))
    displayed = bool(generated and at and at <= now)
    used = exposure.get("used_by_rep") if displayed else None
    values = {
        "generated_guidance": int(generated),
        "displayed_guidance": int(displayed),
        "rep_use_yes": int(generated and used is True),
        "rep_use_no": int(generated and used is False),
        "rep_use_unknown": int(generated and used is None),
        "reviewed_cards": int(
            any(feedback.get(k) is not None for k in ("useful", "grounded", "timely", "distracting"))
        ),
    }
    for key in ("useful", "grounded", "timely", "distracting"):
        values[key + "_true"] = int(feedback.get(key) is True)
        values[key + "_count"] = int(isinstance(feedback.get(key), bool))
    return values


def delta_values(previous, current):
    """Atomic replacement subtracts the old vector exactly once, including zeros."""
    delta = {}
    for key in sorted(previous.keys() | current.keys()):
        old, new = previous.get(key, 0), current.get(key, 0)
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (old, new)
        ):
            raise ValueError("Contribution values must be finite numbers")
        if old != new:
            delta[key] = new - old
    return delta
