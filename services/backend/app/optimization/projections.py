"""Report totals computed directly from source records.

Every source record (lead, call attempt, usage receipt, helper event) contributes a vector
of counts; vectors are summed into groups: seller totals, per-rep totals and the detail groups
of numbers, comparisons, trials, estimates and drift. Raw text never enters a vector. The pure
per-record functions keep the definitions in one place; `compute` evaluates the selected
campaign's records in pages and the saved report views publish the result.
"""

import hashlib
import json
import math
from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from sqlalchemy import select

from app.db import MODELS, doc
from app.optimization.learning import (
    conversion_at,
    drift_from_counts,
    drift_stratum,
    drift_values,
    outcome_at,
    retry_statistics,
    reward_counts,
    training_rows,
    trial_from_counts,
    trial_values,
)
from app.optimization.numbering import timestamp
from app.scheduling.models import derive_labels, estimate_counts

PAGE = 500
NAMESPACE = "current"


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
    # The campaign outcome window attributes a conversion event to an attempt, as
    # training does: it counts only when it occurred within window_days of one.
    window = timedelta(days=config["window_days"])
    starts = [
        at
        for call in calls
        if call.get("lead_id") == lead["id"] and (at := timestamp(call.get("started_at"))) and at <= now
    ]
    known = [
        (
            derive_labels(value["kind"]),
            any(at <= timestamp(value["occurred_at"]) <= at + window for at in starts),
        )
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
        "held_meetings": int(
            original and any(row["meeting_held"] is True for row in outcomes + [row for row, _ in known])
        ),
        "unique_conversions": int(
            original
            and (
                any(row[label] is True for row in outcomes)
                or any(row[label] is True and inside for row, inside in known)
            )
        ),
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
            "connect_recent_n",
            "connect_recent_s",
            "connect_recent_unknown",
            "connect_reference_n",
            "connect_reference_s",
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
    if submitted >= now - timedelta(days=1):
        values.update(
            connect_recent_n=int(labels["human"] is not None),
            connect_recent_s=int(labels["human"] is True),
            connect_recent_unknown=int(labels["human"] is None),
        )
    elif submitted >= now - timedelta(days=28):
        values.update(
            connect_reference_n=int(labels["human"] is not None),
            connect_reference_s=int(labels["human"] is True),
        )
    return values


def training_values(call, feature, events, *, config, now):
    """One attempt's current label counts, without saving a second training row."""
    data = training_rows(
        [call],
        [feature] if feature else [],
        events,
        now=now,
        goal=config["conversion_goal"],
        window_days=config["window_days"],
        include_unclassified=True,
    )
    row = data[0] if data and outcome_at(call, now) else {}
    return {
        **{"estimate:" + k: v for k, v in estimate_counts(data).items()},
        **{
            k: v
            for k, v in reward_counts([row] if row else []).items()
            if k.rsplit(":", 1)[-1].startswith("all_")
        },
        "dataset_rows": int(bool(row)),
        "dataset_pending_rows": int(bool(row) and row["conversion"] is None),
        "dataset_matured_rows": int(bool(row) and row["window_closed"]),
        "dataset_positive_conversions": int(bool(row) and row["conversion"] == 1),
        "dataset_missing_snapshot_calls": int(bool(outcome_at(call, now)) and not feature),
    }


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


def observed_call_cost(values):
    """A complete call is one eligible attempt with a checked receipt set."""
    eligible = bool(values.get("eligible"))
    complete = eligible and values.get("verified_sets", 0) > 0 and not values.get("incomplete")
    return {
        "observed_call_cost_microusd": values.get("settled_microusd", 0) if complete else 0,
        "observed_cost_calls": int(bool(complete)),
        "unknown_cost_calls": int(eligible and not complete),
    }


def _call_cost_group(snapshot, values, namespace):
    kind, row = snapshot["kind"], snapshot["row"]
    if kind == "call" and row and not row.get("inbound"):
        identity, counts = row["id"], {"eligible": values.get("dataset_rows", 0)}
    elif kind == "usage" and row:
        identity = row.get("call_id") or row.get("object_id")
        if not identity or row.get("status") == "released":
            return []
        settled = row.get("status") == "settled" and row.get("cost_usd") is not None
        counts = {
            "incomplete": int(not settled),
            "verified_sets": int(
                bool(settled and row.get("kind") == "call_authorization" and row.get("billing_verified"))
            ),
            "settled_microusd": (_microusd(row["cost_usd"]) - _microusd(row.get("credited_usd", 0)))
            if settled
            else 0,
        }
    else:
        return []
    return [
        (
            {
                "bucket_kind": "call_cost",
                "bucket_day": None,
                "campaign_id": snapshot["campaign_id"],
                "definition_version": snapshot["definition"],
                "namespace": namespace,
                "call_id": identity,
            },
            counts,
        )
    ]


LATENCY_FIELDS = (
    "state_ms",
    "retrieval_ms",
    "stt_ms",
    "inference_ms",
    "delivery_ms",
    "render_ms",
    "event_to_render_ms",
)


# Fixed upper-bound bins: exact whole milliseconds through100, then10% plus1ms rounding.
# One hundred million distinct provider timings cannot enlarge a bucket forever.
LATENCY_BOUNDS = tuple(range(101)) + tuple(sorted({math.ceil(100 * 1.1**i) for i in range(1, 93)}))


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
    timing = {**event.get("latency", {}), **feedback, **exposure.get("timing", {})}
    for key in LATENCY_FIELDS:
        value = timing.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            continue
        bound = next((v for v in LATENCY_BOUNDS if v >= value), "overflow")
        values[f"latency:{key}:{bound}"] = 1
    return values


def coaching_from_counts(values):
    result = {
        "reviewed_cards": values.get("reviewed_cards", 0),
        "latency_support": {},
        "latency_quantization": {
            "upper_bounds": True,
            "exact_integer_ms_through": 100,
            "relative_width": 0.1,
            "rounding_ms": 1,
        },
        "evidence_scope": "Recorded provider and reviewer measurements; bounded percentile upper estimates. Live STT target remains unverified.",
    }
    for key in ("useful", "grounded", "timely", "distracting"):
        n = values.get(key + "_count", 0)
        result[key + "_fraction"] = values.get(key + "_true", 0) / n if n else None
    for key in LATENCY_FIELDS:
        counts = [(v, values.get(f"latency:{key}:{v}", 0)) for v in (*LATENCY_BOUNDS, "overflow")]
        n = sum(count for _, count in counts)
        result["latency_support"][key] = n
        threshold, cumulative, percentile = math.ceil(n * 0.95), 0, None
        for bound, count in counts:
            cumulative += count
            if n and cumulative >= threshold:
                percentile = bound if bound != "overflow" else None
                break
        result["p95_" + key] = percentile
    return result


def definition_version(config, epoch):
    """Inputs that change saved vectors. Health thresholds apply when read; only the
    health window decides which attempts enter a number's buckets."""
    return hashlib.sha256(
        json.dumps(
            {
                "metrics": "cohort-work-v15",
                "epoch": epoch,
                "goal": config["conversion_goal"],
                "window_days": config["window_days"],
                "health_window_days": config.get("health_policy", {}).get("window_days"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _trial_groups(snapshot, now, namespace):
    from app.optimization.experiments import call_goal_events

    earliest = {}
    for item in sorted(snapshot["assignments"], key=lambda r: (r["assigned_at"], r["id"])):
        if item.get("family", "optimization") == "optimization" and item.get("epoch"):
            earliest.setdefault(item["epoch"], item)
    decisions = {r["id"]: r for r in snapshot["decisions"]}
    features = {r["id"]: r for r in snapshot["features"]}
    events = [
        known
        for event in snapshot["events"]
        if (known := conversion_at(event, now)) and known["timing_known"] and not known["retracted"]
    ]
    groups = []
    for epoch, assignment in earliest.items():
        goal = assignment.get("goal", snapshot["config"]["conversion_goal"])
        days = assignment.get("outcome_window_days", snapshot["config"]["window_days"])
        start, due = timestamp(assignment["assigned_at"]), timestamp(assignment.get("coverage_due_at"))
        attempts = [
            r
            for r in snapshot["calls"]
            if (r.get("submitted_at") or r.get("provider_id"))
            and (at := timestamp(r.get("started_at")))
            and start <= at <= now
        ]
        evidence = [decisions.get(r.get("submission_decision_id")) for r in attempts]
        values = trial_values(
            {
                **assignment,
                "covered": any(not due or timestamp(r["started_at"]) <= due for r in attempts),
                "policy_unknown": sum(r is None for r in evidence),
                "policy_violations": sum(r is not None and not r.get("allowed", False) for r in evidence),
            },
            events + call_goal_events(snapshot["calls"], goal, now=now),
            now=now,
            goal=goal,
            window_days=days,
        )
        study = snapshot["studies"].get(epoch, {})
        for call in attempts:
            if (
                not study
                or not call.get("submitted_at")
                or timestamp(call["started_at"]) > timestamp(study["ends_at"])
            ):
                continue
            actual = (
                features.get(call.get("feature_snapshot_id"), {}).get("predictions", {}).get("model_version")
            )
            tested = actual == study["model_id"]
            key = (
                ("candidate_tested_model_attempts" if tested else "candidate_other_model_attempts")
                if assignment["arm"] == "candidate"
                else "control_tested_model_attempts"
                if tested
                else None
            )
            if key:
                values[key] = values.get(key, 0) + 1
        groups.append(
            (
                {
                    "bucket_kind": "trial",
                    "bucket_day": None,
                    "campaign_id": snapshot["campaign_id"],
                    "definition_version": snapshot["definition"],
                    "namespace": namespace,
                    "epoch": epoch,
                    "goal": goal,
                    "window_days": days,
                    "study": study,
                },
                values,
            )
        )
    return groups


def _comparison_groups(snapshot, now, namespace):
    from app.optimization.experiments import (
        call_goal_events,
        exposure_values,
        reward_values,
        strategy_rewards,
    )

    assignments = {}
    for item in sorted(snapshot["assignments"], key=lambda r: (r["assigned_at"], r["id"])):
        if item.get("comparison_id"):
            assignments.setdefault(item["comparison_id"], item)
    studies = {r["id"]: r for r in snapshot["comparisons"]}
    decisions = {r["id"]: r for r in snapshot["decisions"]}
    groups = []

    def add(study, values, stratum=None, rep_id=None):
        if not values:
            return
        groups.append(
            (
                {
                    "bucket_kind": "comparison",
                    "bucket_day": None,
                    "campaign_id": snapshot["campaign_id"],
                    "definition_version": snapshot["definition"],
                    "namespace": namespace,
                    "comparison_id": study["id"],
                    "stratum": stratum,
                    "rep_id": rep_id,
                },
                values,
            )
        )

    for identity, study in studies.items():
        assignment = assignments.get(identity)
        values = {}
        if snapshot["kind"] == "lead":
            goal, days = study["goal"], study["window_days"]
            events = snapshot["events"] + call_goal_events(snapshot["calls"], goal, now=now)
            if assignment:
                start, due = (
                    timestamp(assignment["assigned_at"]),
                    timestamp(assignment.get("coverage_due_at")),
                )
                attempts = [
                    r
                    for r in snapshot["calls"]
                    if r.get("submitted_at") and start <= timestamp(r["started_at"]) <= now
                ]
                evidence = [decisions.get(r.get("submission_decision_id"), {}) for r in attempts]
                values = trial_values(
                    {
                        **assignment,
                        "covered": any(not due or timestamp(r["started_at"]) <= due for r in attempts),
                        "policy_unknown": sum(r.get("allowed") is None for r in evidence),
                        "policy_violations": sum(r.get("allowed") is False for r in evidence),
                    },
                    events,
                    now=now,
                    goal=goal,
                    window_days=days,
                )
                field = "cluster" if study["family"] == "number_allocation" else "rep_id"
                add(study, dict(values), assignment.get(field) or "unknown")
            if study.get("strategy_ids"):
                prior = studies.get(study.get("evidence_comparison_id"))
                seed = (
                    strategy_rewards(
                        prior,
                        snapshot["assignments"],
                        snapshot["events"] + call_goal_events(snapshot["calls"], prior["goal"], now=now),
                        now=now,
                    )
                    if prior
                    else []
                )
                rewards = seed or strategy_rewards(study, snapshot["assignments"], events, now=now)
                if rewards:
                    values.update(reward_values(rewards[0]))
        else:
            feedback, row = snapshot["feedback"], snapshot["row"]
            if assignment and feedback:
                prefix = f"quality:{assignment['arm']}:"
                values[prefix + "reviewed_cards"] = 1
                for key in ("useful", "grounded", "timely", "distracting"):
                    values[prefix + key + "_count"] = int(isinstance(feedback.get(key), bool))
                    values[prefix + key + "_true"] = int(feedback.get(key) is True)
            if (
                study.get("strategy_ids")
                and row.get("strategy_exposure", {}).get("comparison_id") == identity
            ):
                values.update(
                    {
                        "exposure:" + k: v
                        for k, v in exposure_values(row, snapshot["observations"], now=now).items()
                    }
                )
        add(study, values)
        if snapshot["kind"] == "help" and snapshot["row"].get("rep_id"):
            add(study, values, rep_id=snapshot["row"]["rep_id"])
    return groups


def _health_groups(snapshot, now, namespace):
    from app.optimization.numbering import health_stratum, health_values

    row, policy = snapshot["row"], snapshot["config"]["health_policy"]
    if not row.get("number_id"):
        return []
    if row.get("inbound"):
        at = timestamp(row.get("occurred_at") or row.get("created_at"))
        values = {"callbacks": 1} if at and now - timedelta(days=policy["window_days"]) <= at <= now else {}
    else:
        known = outcome_at(row, now)
        if not known and row.get("end_reason") not in {"provider_failure", "route_failure"}:
            return []
        feature = snapshot["feature"] or {}
        observation = {
            **feature.get("features", {}),
            **row,
            **(known or {"outcome": None}),
            "feature_schema_version": feature.get(
                "feature_schema_version", feature.get("features", {}).get("feature_schema_version")
            ),
            "prediction_at": feature.get("as_of"),
            "expected_human": feature.get("predictions", {}).get("p_human"),
            "delivery_failed": row.get("end_reason") in {"provider_failure", "route_failure"},
        }
        values = health_values(observation, now=now, policy=policy)
    if not values:
        return []
    group = {
        "bucket_kind": "number_health",
        "bucket_day": None,
        "number_id": row["number_id"],
        "campaign_id": snapshot["campaign_id"],
        "definition_version": snapshot["definition"],
        "namespace": namespace,
    }
    result = [(group, values)]
    if values.get("comparable_samples"):
        result.append(
            (
                {**group, "bucket_kind": "number_health_stratum", "stratum": health_stratum(observation)},
                {"samples": 1},
            )
        )
    return result


def _detail_groups(snapshot, now, namespace):
    """Per-call statistics grouped independently of the workspace totals."""
    if snapshot["kind"] == "lead" and snapshot["row"]:
        config = snapshot["config"]
        data = training_rows(
            snapshot["calls"],
            snapshot["features"],
            snapshot["events"],
            now=now,
            goal=config["conversion_goal"],
            window_days=config["window_days"],
        )
        retry = retry_statistics(
            data,
            [{**snapshot["row"], "suppressed": snapshot["suppressed"]}],
            now=now,
            # Retain observed continuation at every known attempt index. The
            # planner applies the current permitted allowance, not this counter.
            max_attempts=max((r["features"].get("attempt_count", 0) for r in data), default=0) + 2,
        )
        groups = [
            (
                {
                    "bucket_kind": "retry",
                    "bucket_day": None,
                    "campaign_id": snapshot["campaign_id"],
                    "definition_version": snapshot["definition"],
                    "namespace": namespace,
                    "segment": r["segment"],
                    "attempt_index": r["attempt_index"],
                },
                {k: r[k] for k in ("needs_retry_count", "classified_count")},
            )
            for r in retry
        ]
        return groups + _trial_groups(snapshot, now, namespace) + _comparison_groups(snapshot, now, namespace)
    if snapshot["kind"] == "help" and snapshot["row"]:
        return _comparison_groups(snapshot, now, namespace)
    if snapshot["kind"] != "call" or not snapshot["row"]:
        return []
    groups = _health_groups(snapshot, now, namespace)
    config, row = snapshot["config"], snapshot["row"]
    if row.get("inbound"):
        return groups
    data = training_rows(
        [row],
        [snapshot["feature"]] if snapshot["feature"] else [],
        snapshot["events"],
        now=now,
        goal=config["conversion_goal"],
        window_days=config["window_days"],
        include_unclassified=True,
    )
    if not data:
        return groups
    observed = data[0]
    groups.append(
        (
            {
                "bucket_kind": "estimate",
                "bucket_day": None,
                "campaign_id": snapshot["campaign_id"],
                "definition_version": snapshot["definition"],
                "namespace": namespace,
                "features": {
                    k: observed.get(k) for k in ("segment", "local_hour", "local_weekday", "attempt_count")
                },
            },
            estimate_counts(data),
        )
    )
    if rewards := reward_counts(data if outcome_at(row, now) else []):
        groups.append(
            (
                {
                    "bucket_kind": "treatment",
                    "bucket_day": None,
                    "campaign_id": snapshot["campaign_id"],
                    "definition_version": snapshot["definition"],
                    "namespace": namespace,
                    "segment": observed.get("segment", "unknown"),
                },
                rewards,
            )
        )
    if not now - timedelta(days=28) <= timestamp(observed["at"]) <= now:
        return groups
    return groups + [
        (
            {
                "bucket_kind": "drift",
                "bucket_day": None,
                "campaign_id": snapshot["campaign_id"],
                "definition_version": snapshot["definition"],
                "namespace": namespace,
                "period": "recent" if timestamp(observed["at"]) >= now - timedelta(days=7) else "reference",
                "stratum": drift_stratum(observed),
            },
            drift_values(observed),
        )
    ]


_EMPTY_WHEN_ZERO = {
    "comparison",
    "number_health",
    "number_health_stratum",
    "call_cost",
    "estimate",
    "retry",
    "treatment",
}


def _calculate_unit(snapshot, now):
    """One record's vector and the next time its value changes with the clock alone."""
    kind, row, config = snapshot["kind"], snapshot["row"], snapshot["config"]
    refresh = []
    if kind == "lead" and row:
        values = lead_values(
            row,
            snapshot["calls"],
            snapshot["events"],
            suppressed=snapshot["suppressed"],
            config=config,
            now=now,
        )
        due = timestamp(row.get("coverage_due_at"))
        if due and due >= now:
            refresh.append(due + timedelta(microseconds=1))
        for assignment in snapshot["assignments"]:
            if at := timestamp(assignment.get("assigned_at")):
                boundary = at + timedelta(days=assignment.get("outcome_window_days", config["window_days"]))
                if boundary > now:
                    refresh.append(boundary)
    elif kind == "call" and row and not row.get("inbound"):
        values = call_values(row, config=config, now=now)
        values.update(training_values(row, snapshot["feature"], snapshot["events"], config=config, now=now))
        start = timestamp(
            row.get("submitted_at") or (row.get("started_at") if row.get("provider_id") else None)
        )
        at = timestamp(row.get("started_at"))
        boundaries = [start, at + timedelta(days=config["window_days"]) if at else None]
        if start:
            boundaries += [start + timedelta(days=config["window_days"])]
            boundaries += [start + timedelta(days=days, microseconds=1) for days in (1, 28)]
        if at:
            boundaries += [at + timedelta(days=days, microseconds=1) for days in (7, 28)]
        refresh.extend(t for t in boundaries if t and t > now)
    elif kind == "usage" and row:
        values = usage_values(row)
    elif kind == "help" and row:
        values = help_values(row, snapshot["feedback"], now=now)
    else:
        values = {}
    if kind == "call" and row:
        at = timestamp(row.get("occurred_at") or row.get("started_at") or row.get("created_at"))
        if at:
            window = timedelta(days=config["health_policy"]["window_days"], microseconds=1)
            refresh.extend(t for t in (at, at + window) if t > now)
    for evidence in [row, *snapshot["calls"], *snapshot["events"]]:
        for field in (
            "outcome_available_at",
            "occurred_at",
            "label_available_at",
            "observed_at",
            "retracted_at",
        ):
            at = timestamp(evidence.get(field))
            if at and at > now:
                refresh.append(at)
    return values, min(refresh) if refresh else None


class Totals:
    """Summed vectors by group, for one campaign's definition at one instant."""

    def __init__(self, campaign, config, definition, now):
        self.campaign, self.config, self.definition, self.now = campaign, config, definition, now
        self.groups, self.next_refresh = {}, None

    def group(self, **fields):
        return {
            "bucket_day": None,
            "campaign_id": self.campaign.id,
            "definition_version": self.definition,
            "namespace": NAMESPACE,
            **fields,
        }

    def add(self, group, values):
        key = json.dumps(group, sort_keys=True, separators=(",", ":"), default=str)
        entry = self.groups.setdefault(key, {"group": group, "values": {}})
        for name, value in values.items():
            entry["values"][name] = entry["values"].get(name, 0) + value

    def due(self, at):
        if at and (self.next_refresh is None or at < self.next_refresh):
            self.next_refresh = at

    def buckets(self, kind):
        """Non-empty groups of one kind, in a stable order."""
        result = []
        for _, entry in sorted(self.groups.items()):
            group, values = entry["group"], entry["values"]
            if group["bucket_kind"] != kind:
                continue
            if (
                (kind == "drift" and values.get("rows", 0) == 0)
                or (
                    kind == "trial" and values.get("control:leads", 0) + values.get("candidate:leads", 0) == 0
                )
                or (kind in _EMPTY_WHEN_ZERO and not any(values.values()))
            ):
                continue
            result.append({"group": group, "values": values})
        return result

    @property
    def total(self):
        return self.groups.get(_key(self.group(bucket_kind="total")), {}).get("values", {})

    def rep_total(self, rep_id):
        return self.groups.get(_key(self.group(bucket_kind="rep_total", rep_id=rep_id)), {}).get("values", {})


def _key(group):
    return json.dumps(group, sort_keys=True, separators=(",", ":"), default=str)


def _scoped(p, table, *conditions):
    model = MODELS[table]
    return select(model).where(
        model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"], *conditions
    )


def _docs(session, p, table, *conditions):
    return [doc(r) for r in session.scalars(_scoped(p, table, *conditions))]


def _chunks(values, size=PAGE):
    values = list(values)
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _record(totals, snapshot, *, details):
    now = totals.now
    snapshot = {
        "calls": [],
        "events": [],
        "feedback": None,
        "feature": None,
        "assignments": [],
        "features": [],
        "decisions": [],
        "studies": {},
        "observations": [],
        "suppressed": False,
        "campaign_id": totals.campaign.id,
        "config": totals.config,
        "definition": totals.definition,
        "comparisons": totals.comparisons,
        **snapshot,
    }
    try:
        values, due = _calculate_unit(snapshot, now)
        extra = _detail_groups(snapshot, now, NAMESPACE) if details else []
        extra += _call_cost_group(snapshot, values, NAMESPACE)
    except (ValueError, ArithmeticError):
        totals.add(totals.group(bucket_kind="total"), {"unit_errors": 1})
        return
    totals.due(due)
    if values:
        totals.add(totals.group(bucket_kind="total"), values)
        if snapshot["kind"] == "help" and snapshot["row"].get("rep_id"):
            totals.add(totals.group(bucket_kind="rep_total", rep_id=snapshot["row"]["rep_id"]), values)
    for group, group_values in extra:
        totals.add(group, group_values)


def compute(service, p, *, details=True):
    """Evaluate every record of the selected campaign. Memory stays bounded to one page."""
    from app.measurement import work_summary

    now = service.clock()
    with service.db.snapshot() as session:
        primary = service.selected_campaign(session, {k: v for k, v in p.items() if k != "campaign_id"})
        campaign = service.selected_campaign(session, p, p.get("campaign_id"))
        config = service.optimization_config(session, p, campaign=campaign)
        epoch = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
        totals = Totals(campaign, config, definition_version(config, epoch), now)
        totals.comparisons = [
            {k: v for k, v in item.items() if k != "population"}
            for item in campaign.data.get("controlled_comparisons", [])
        ]
        leads, calls_model = MODELS["leads"], MODELS["call_attempts"]
        lead_campaign = dict(
            session.execute(
                select(leads.id, leads.op_campaign_id).where(
                    leads.tenant_id == p["tenant_id"], leads.seller_id == p["seller_id"]
                )
            ).all()
        )
        call_meta = {
            row[0]: (row[1] or lead_campaign.get(row[2]), row[2])
            for row in session.execute(
                select(calls_model.id, calls_model.op_campaign_id, calls_model.op_lead_id).where(
                    calls_model.tenant_id == p["tenant_id"], calls_model.seller_id == p["seller_id"]
                )
            )
        }

        def resolved(row):
            campaign_id = row.get("campaign_id") or lead_campaign.get(row.get("lead_id"))
            if not campaign_id:
                campaign_id = call_meta.get(row.get("call_id") or row.get("object_id"), (None, None))[0]
            return (campaign_id or primary.id) == campaign.id

        studies = {epoch: campaign.data.get("optimization_trial") or {}}
        for model in session.scalars(_scoped(p, "model_versions")):
            if model.data.get("trial_epoch"):
                studies.setdefault(model.data["trial_epoch"], model.data.get("trial_study", {}))
        intervals = _docs(session, p, "work_intervals")
        work_calls = []
        lead_ids = sorted(i for i, c in lead_campaign.items() if c == campaign.id)
        for page in _chunks(lead_ids):
            people = _docs(session, p, "leads", leads.id.in_(page))
            calls = _docs(session, p, "call_attempts", calls_model.op_lead_id.in_(page))
            events = _docs(session, p, "conversion_events", MODELS["conversion_events"].op_lead_id.in_(page))
            assignments = _docs(
                session, p, "experiment_assignments", MODELS["experiment_assignments"].op_lead_id.in_(page)
            )
            features = {
                r["id"]: r
                for r in _docs(
                    session,
                    p,
                    "feature_snapshots",
                    MODELS["feature_snapshots"].id.in_(
                        {c.get("feature_snapshot_id") for c in calls} - {None}
                    ),
                )
            }
            decisions = {
                r["id"]: r
                for r in _docs(
                    session,
                    p,
                    "compliance_decisions",
                    MODELS["compliance_decisions"].id.in_(
                        {c.get("submission_decision_id") for c in calls} - {None}
                    ),
                )
            }
            work = {
                r["call_id"]: r
                for r in _docs(
                    session,
                    p,
                    "outcomes",
                    MODELS["outcomes"].external_key.in_(["attempt-work:" + c["id"] for c in calls]),
                )
                if r.get("kind") == "attempt_work"
            }
            denied = {
                r["phone"]
                for r in _docs(
                    session,
                    p,
                    "suppressions",
                    MODELS["suppressions"].external_key.in_({x.get("phone") for x in people}),
                )
                if r.get("active", True)
            }
            by_lead = {}
            for call in calls:
                by_lead.setdefault(call.get("lead_id"), []).append(call)
            for lead in people:
                own = by_lead.get(lead["id"], [])
                _record(
                    totals,
                    {
                        "kind": "lead",
                        "identity": lead["id"],
                        "row": lead,
                        "calls": own,
                        "events": [e for e in events if e.get("lead_id") == lead["id"]],
                        "assignments": [a for a in assignments if a.get("lead_id") == lead["id"]],
                        "features": [
                            features[c["feature_snapshot_id"]]
                            for c in own
                            if c.get("feature_snapshot_id") in features
                        ],
                        "decisions": [
                            decisions[c["submission_decision_id"]]
                            for c in own
                            if c.get("submission_decision_id") in decisions
                        ],
                        "studies": studies,
                        "suppressed": lead.get("phone") in denied,
                    },
                    details=details,
                )
            for call in calls:
                work_calls.append(call)
                if not resolved(call):
                    continue
                attempt = work.get(call["id"])
                row = dict(call)
                if attempt:
                    row.update(
                        {k: attempt[k] for k in ("rep_work_intervals", "rep_work_incomplete") if k in attempt}
                    )
                _record(
                    totals,
                    {
                        "kind": "call",
                        "identity": call["id"],
                        "row": row,
                        "feature": features.get(call.get("feature_snapshot_id")),
                        "events": [e for e in events if e.get("lead_id") == call.get("lead_id")],
                    },
                    details=details,
                )
            session.expunge_all()
        for task in _docs(session, p, "followup_tasks"):
            if task.get("inbound") and resolved(task):
                _record(
                    totals,
                    {"kind": "call", "identity": "callback:" + task["id"], "row": task},
                    details=details,
                )
        session.expunge_all()
        usage = MODELS["usage_ledger"]
        after = ""
        while True:
            page = [
                doc(r)
                for r in session.scalars(
                    _scoped(p, "usage_ledger", usage.id > after).order_by(usage.id).limit(PAGE)
                )
            ]
            if not page:
                break
            after = page[-1]["id"]
            for row in page:
                if resolved(row):
                    _record(totals, {"kind": "usage", "identity": row["id"], "row": row}, details=details)
            session.expunge_all()
        events_model, after = MODELS["coach_events"], ""
        while True:
            page = [
                doc(r)
                for r in session.scalars(
                    _scoped(p, "coach_events", events_model.id > after).order_by(events_model.id).limit(PAGE)
                )
            ]
            if not page:
                break
            after = page[-1]["id"]
            calls = {
                c["id"]: c
                for c in _docs(
                    session, p, "call_attempts", calls_model.id.in_({e.get("call_id") for e in page} - {None})
                )
            }
            leads_of = {c.get("lead_id") for c in calls.values()} - {None}
            assignments = _docs(
                session,
                p,
                "experiment_assignments",
                MODELS["experiment_assignments"].op_lead_id.in_(leads_of),
            )
            lineage = {}
            for record in session.scalars(
                _scoped(
                    p,
                    "outcomes",
                    MODELS["outcomes"].ctx_key.in_(list(calls)),
                    MODELS["outcomes"].ctx_status == "conversation_lineage",
                )
            ):
                value = record.data
                latest = lineage.setdefault(value.get("call_id"), {})
                if value["version"] >= latest.get(value["id"], {}).get("version", 0):
                    latest[value["id"]] = dict(value)
            rows = []
            for event in page:
                call = calls.get(event.get("call_id"))
                rep = event.get("rep_id") or (call.get("rep_id") if call else None)
                rows.append((event, call, rep))
            feedback = {
                r.external_key: doc(r)
                for r in session.scalars(
                    _scoped(
                        p,
                        "coaching_feedback",
                        MODELS["coaching_feedback"].external_key.in_(
                            [f"{e['id']}:{rep}" for e, _, rep in rows if rep]
                        ),
                    )
                )
            }
            for event, call, rep in rows:
                row = {**event, **({"rep_id": rep} if rep else {})}
                if not resolved({**row, "lead_id": call.get("lead_id") if call else None}):
                    continue
                _record(
                    totals,
                    {
                        "kind": "help",
                        "identity": event["id"],
                        "row": row,
                        "feedback": feedback.get(f"{event['id']}:{rep}") if rep else None,
                        "calls": [call] if call else [],
                        "assignments": [
                            a for a in assignments if call and a.get("lead_id") == call.get("lead_id")
                        ],
                        "observations": list(lineage.get(call["id"], {}).values()) if call else [],
                    },
                    details=details,
                )
            session.expunge_all()
    work = work_summary(intervals, work_calls, now=now)
    total = totals.group(bucket_kind="total")
    totals.add(
        total,
        {
            "rep_work_seconds": work["seconds"] or 0,
            "work_evidence_count": len(work["by_rep"]),
            "stale_intervals": work["stale_intervals"],
        },
    )
    for rep, seconds in work["by_rep"].items():
        totals.add(
            totals.group(bucket_kind="rep_total", rep_id=rep),
            {"rep_work_seconds": seconds, "work_evidence_count": 1},
        )
    for row in intervals:
        lease = timestamp(row.get("lease_until"))
        if not row.get("ended_at") and lease and lease > now:
            totals.due(lease)
    costs = {}
    for entry in totals.buckets("call_cost"):
        for name, value in observed_call_cost(entry["values"]).items():
            costs[name] = costs.get(name, 0) + value
    totals.add(total, costs)
    return totals


def read_coaching_metrics(service, p, totals):
    return coaching_from_counts(totals.rep_total(p["user_id"]) if p.get("role") == "rep" else totals.total)


def read_dataset_metrics(service, p, totals):
    values = totals.total
    keys = ("rows", "pending_rows", "matured_rows", "positive_conversions", "missing_snapshot_calls")
    return {
        **{key: values.get("dataset_" + key, 0) for key in keys},
        "snapshots": totals.campaign.data.get("dataset_summaries", [])[-10:],
    }


def read_estimate_statistics(service, p, totals):
    """Bounded conditional cells with exact pooled support; never partial local rates."""
    values = totals.total
    cells, retry, treatment = (totals.buckets(kind) for kind in ("estimate", "retry", "treatment"))
    return {
        "costs": {
            "cost_usd": values.get("observed_call_cost_microusd", 0) / 1_000_000,
            "cost_observations": values.get("observed_cost_calls", 0),
            "cost_unknown_observations": values.get("unknown_cost_calls", 0),
            "level": "pooled_complete_calls",
        },
        "treatment_counts": {
            row["group"]["segment"]: {
                **row["values"],
                **{k: v for k, v in values.items() if k.startswith("reward:")},
            }
            for row in treatment
        }
        if len(treatment) <= 1000
        else {},
        "treatments_complete": len(treatment) <= 1000,
        "total": {k.removeprefix("estimate:"): v for k, v in values.items() if k.startswith("estimate:")},
        "cells": [{"features": row["group"]["features"], "counts": row["values"]} for row in cells[:1000]],
        "cells_complete": len(cells) <= 1000,
        "retry_observations": [
            {
                **{k: row["group"][k] for k in ("segment", "attempt_index")},
                **row["values"],
                "matured": True,
                "available_at": totals.now.isoformat(),
            }
            for row in retry
        ]
        if len(retry) <= 1000
        else [],
        "retry_complete": len(retry) <= 1000,
    }


def read_drift_metrics(service, p, totals, *, limit=1000):
    buckets = totals.buckets("drift")
    if len(buckets) > limit:
        return {
            **drift_from_counts({}, {}),
            "complete": False,
            "action": "summary_capacity_exceeded",
            "drift_detected": None,
            "missingness_shift": None,
            "reference_rows": None,
            "recent_rows": None,
            "excluded_reference": None,
            "excluded_recent": None,
            "as_of": totals.now.isoformat(),
            "reason": "More comparable strata than the bounded report can inspect; no monitoring conclusion published",
            "inspected_limit": limit,
        }
    periods = {"reference": {}, "recent": {}}
    for bucket in buckets:
        group = bucket["group"]
        key = tuple(group["stratum"]) if group["stratum"] is not None else None
        periods[group["period"]][key] = bucket["values"]
    return {
        **drift_from_counts(periods["reference"], periods["recent"]),
        "complete": True,
        "as_of": totals.now.isoformat(),
        "windows": "Latest 7 days versus preceding 21 days",
    }


def read_number_health_metrics(service, p, totals):
    from app.optimization.numbering import health_from_counts

    with service.db.transaction(False) as session:
        policy = {
            **service.optimization_config(session, p, totals.campaign.id)["health_policy"],
            "window_days": totals.config["health_policy"]["window_days"],
        }
    strata = totals.buckets("number_health_stratum")
    distinct = {}
    for row in strata:
        identity = row["group"]["number_id"]
        distinct[identity] = distinct.get(identity, 0) + 1
    return {
        row["group"]["number_id"]: health_from_counts(
            row["values"],
            now=totals.now,
            policy=policy,
            comparison_strata=distinct.get(row["group"]["number_id"], 0) if len(strata) <= 1000 else None,
            next_expiry_at=totals.next_refresh.isoformat() if totals.next_refresh else None,
        )
        for row in totals.buckets("number_health")
    }


def read_comparison_metrics(service, p, totals):
    from app.optimization.experiments import comparison_from_counts

    studies = totals.campaign.data.get("controlled_comparisons", [])
    counts, strata, private = {}, {}, {}
    for bucket in totals.buckets("comparison"):
        meta = bucket["group"]
        if meta.get("rep_id"):
            if meta["rep_id"] == p["user_id"]:
                private[meta["comparison_id"]] = bucket["values"]
        elif meta["stratum"] is None:
            counts[meta["comparison_id"]] = bucket["values"]
        else:
            strata.setdefault(meta["comparison_id"], {})[meta["stratum"]] = bucket["values"]
    if p.get("role") == "rep":
        counts = {
            study["id"]: {
                **{
                    k: v
                    for k, v in counts.get(study["id"], {}).items()
                    if not k.startswith(("quality:", "exposure:"))
                },
                **private.get(study["id"], {}),
            }
            for study in studies
        }
    return [
        comparison_from_counts(
            study, counts.get(study["id"], {}), strata.get(study["id"], {}), now=totals.now
        )
        for study in studies
    ]


def read_trial_metrics(service, p, totals, *, epoch=None):
    campaign, config = totals.campaign, totals.config
    current = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
    buckets = totals.buckets("trial")
    epoch = epoch or current
    selected = [b for b in buckets if b["group"]["epoch"] == epoch]
    if not selected and epoch != current:
        from app.service import fail

        fail(404, "Controlled cohort not found")
    meta = (
        selected[0]["group"]
        if selected
        else {
            "goal": config["conversion_goal"],
            "window_days": config["window_days"],
            "study": campaign.data.get("optimization_trial") or {},
        }
    )
    values = selected[0]["values"] if selected else {}
    result = trial_from_counts(values, goal=meta["goal"], window_days=meta["window_days"])
    study = meta["study"]
    exposure = {
        key: values.get(key, 0)
        for key in (
            "candidate_tested_model_attempts",
            "candidate_other_model_attempts",
            "control_tested_model_attempts",
        )
    }
    result.update(
        epoch=epoch,
        trial_model_id=study.get("model_id"),
        study=study,
        model_exposure=exposure,
        cohort_history=sorted({b["group"]["epoch"] for b in buckets}),
    )
    result["sufficient_evidence"] &= (
        bool(study)
        and exposure["candidate_tested_model_attempts"] > 0
        and exposure["candidate_other_model_attempts"] == exposure["control_tested_model_attempts"] == 0
    )
    if study and totals.now < timestamp(study["ends_at"]) + timedelta(days=meta["window_days"]):
        result["sufficient_evidence"] = False
        result["analysis_status"] = "Enrollment and all predeclared outcome windows must close"
    return result


def read_metrics(service, p, totals):
    """Seller totals plus bounded rep totals; never source history or a training dataset."""
    from app.measurement import WORK_LEASE_SECONDS, WORK_NOTE, connect_health_counts

    config, values = totals.config, totals.total
    reps = [r for r in totals.buckets("rep_total") if r["values"].get("work_evidence_count")]
    counts = {
        key: values.get(key, 0)
        for key in (
            "original_cohort",
            "original_eligible_denominator",
            "eligible_now",
            "suppressed",
            "attempts",
            "human_pickups",
            "human_classified_attempts",
            "human_unknown_attempts",
            "right_party_conditioning_count",
            "right_party_successes",
            "right_party_unique",
            "held_meetings",
            "unique_conversions",
            "coverage_overdue",
            "pending_outcome_windows",
        )
    }
    work = {
        "seconds": values.get("rep_work_seconds", 0) if values.get("work_evidence_count") else None,
        "by_rep": {r["group"]["rep_id"]: r["values"]["rep_work_seconds"] for r in reps[:500]},
        "stale_intervals": values.get("stale_intervals", 0),
        "source": "declared_presence_heartbeat_and_verified_calls",
        "lease_seconds": WORK_LEASE_SECONDS,
        "note": WORK_NOTE,
        "rep_details_complete": len(reps) <= 500,
    }
    health = connect_health_counts(values)
    denominator = counts["original_eligible_denominator"]
    return {
        **counts,
        "definition_version": totals.definition,
        "projection_namespace": NAMESPACE,
        "next_refresh_at": totals.next_refresh.isoformat() if totals.next_refresh else None,
        "as_of": totals.now.isoformat(),
        "unique_reach_per_original_lead": counts["right_party_unique"] / denominator if denominator else 0,
        "held_meetings_per_original_lead": counts["held_meetings"] / denominator if denominator else 0,
        "conversion_per_original_eligible_lead": counts["unique_conversions"] / denominator
        if denominator
        else None,
        "rep_work_seconds": work["seconds"],
        "work_time": work,
        "conversions_per_rep_hour": counts["unique_conversions"] * 3600 / work["seconds"]
        if work["seconds"]
        else None,
        "outcome_goal": config["conversion_goal"],
        "outcome_window_days": config["window_days"],
        "coverage_shortfall": max(
            0, counts["coverage_overdue"] - int(config["rep_minutes"] * 60 / config["handling_seconds"])
        ),
        "coverage_capacity_assumption": "Declared rep minutes and handling time; number/window feasibility is shown in the allocation proposal.",
        "health": {"connect_rate": health},
        "warnings": [
            "Human pickup declined versus the prior observed period. Review list mix, calling times and number health."
        ]
        if health["status"] == "warning"
        else [],
        **{
            name + "_cost_usd": values.get(name + "_microusd", 0) / 1_000_000
            for name in ("reserved", "verified", "gross", "credited")
        },
        "observed_cost_usd": values.get("observed_call_cost_microusd", 0) / 1_000_000
        if values.get("observed_cost_calls")
        else None,
        "observed_cost_calls": values.get("observed_cost_calls", 0),
        "unknown_cost_calls": values.get("unknown_cost_calls", 0),
        "unit_errors": values.get("unit_errors", 0),
        "billing_verified": bool(values.get("usage_count")) and not values.get("unverified_usage_count"),
        "simulation": service.settings.simulation,
    }
