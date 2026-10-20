"""Stable optional experiments; assignments precede treatment and never change permissions."""

import math
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, select

from app.db import MODELS, doc, put
from app.optimization.learning import (
    conversion_at,
    derive_labels,
    distribution_from_counts,
    hashed_draw,
    outcome_at,
    reward_counts,
    trial_from_counts,
)
from app.optimization.numbering import timestamp


def strategy_rewards(study, assignments, events, *, now):
    """Intent-to-treat outcomes, never helpfulness or inferred sentence adoption."""
    by_lead = defaultdict(list)
    for event in events:
        known = conversion_at(event, now) if event.get("id") else None
        if known and known["timing_known"] and not known["retracted"] and known.get("kind") == study["goal"]:
            by_lead[known.get("lead_id")].append(timestamp(known["occurred_at"]))
    result, seen = [], set()
    for item in sorted(assignments, key=lambda a: (a["assigned_at"], a.get("id", ""))):
        if item.get("comparison_id") != study["id"] or item["lead_id"] in seen:
            continue
        seen.add(item["lead_id"])
        start = timestamp(item["assigned_at"])
        end = start + timedelta(days=study["window_days"])
        if start > now:
            continue
        result.append(
            {
                "assignment_id": item.get("id"),
                "lead_id": item["lead_id"],
                "action": item.get("strategy_id", "control"),
                "propensity": item.get("propensity"),
                "segment": item.get("segment"),
                "window_status": "matured" if end <= now else "pending",
                "conversion": int(any(start <= t <= end for t in by_lead[item["lead_id"]]))
                if end <= now
                else None,
            }
        )
    return result


def strategy_choice(study, lead_id, rep_id, policy=None):
    """Stable finite assignment in a rep stratum; probability belongs to the approach."""
    actions = study["strategy_ids"]
    if study["strategy_policy"] == "adaptive":
        probabilities = (policy or study["initial_strategy_policy"])["probabilities"]
    else:
        q = study["candidate_probability"]
        probabilities = {"control": 1 - q, actions[1]: q}
    if (
        set(probabilities) != set(actions)
        or any(
            not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 < v <= 1
            for v in probabilities.values()
        )
        or abs(sum(probabilities.values()) - 1) > 1e-9
    ):
        raise ValueError("A strategy policy must cover the exact reviewed finite set")
    draw = hashed_draw(f"{study['id']}:{rep_id}:{lead_id}")
    cumulative, chosen = 0.0, actions[-1]
    for action in actions:
        cumulative += probabilities[action]
        if draw < cumulative:
            chosen = action
            break
    return {
        "strategy_id": chosen,
        "strategy_probabilities": probabilities,
        "propensity": probabilities[chosen],
        "arm": "control" if chosen == "control" else "candidate",
    }


def observed_strategy_uses(observations, event_id):
    """Reviewed matching behavior is distinct from a rep saying they adopted advice."""
    return [
        {
            "id": i["id"],
            "version": i["version"],
            "strategy_id": i["strategy_id"],
            "review_receipt_id": i.get("review_receipt_id"),
        }
        for i in observations
        if i.get("observation_kind", i.get("kind")) == "approach"
        and i.get("observed_guidance_id") == event_id
        and i.get("strategy_id")
        and not i.get("invalidated_at")
        and i.get("state") not in {"stale", "retracted"}
        and i.get("review_status") == "reviewed"
        and (
            i.get("epistemic_status") == "human_confirmed"
            or i.get("epistemic_status") == "model_reviewed"
            and i.get("review_receipt_id")
        )
    ]


def assign_arm(family, epoch, lead_id, stratum, assignments=(), *, prior_count=None):
    position = 0
    if family == "number_allocation":
        unit = stratum
    else:
        prior = [a for a in assignments if a["family"] == family and a.get("rep_id") == stratum]
        block, position = divmod(len(prior) if prior_count is None else prior_count, 2)
        unit = stratum + ":" + str(block)
        if position and prior_count is None:
            return "candidate" if prior[-1]["arm"] == "control" else "control"
    first = "candidate" if hashed_draw(epoch + ":" + unit) < 0.5 else "control"
    # Both positions derive the same block coin; timestamp ties cannot select
    # the wrong preceding row. Existing saved assignments are returned unchanged.
    return ("control" if first == "candidate" else "candidate") if position else first


def call_goal_events(calls, goal, *, now):
    """One as-of disposition source for both trial totals and adaptive seeds."""
    result = []
    for call in calls:
        known = outcome_at(call, now)
        at = timestamp(call.get("ended_at") or call.get("started_at"))
        if (
            known
            and known["timing_known"]
            and at
            and at <= now
            and derive_labels(known).get("qualified" if goal == "qualified_conversation" else goal)
        ):
            result.append(
                {
                    "id": "call:" + call["id"],
                    "lead_id": call["lead_id"],
                    "kind": goal,
                    "occurred_at": at.isoformat(),
                    "label_available_at": known["outcome_available_at"],
                }
            )
    return result


def reward_values(reward):
    return reward_counts([reward])


def exposure_values(event, observations, *, now):
    exposure = event.get("strategy_exposure", {})
    shown = timestamp(exposure.get("displayed_at"))
    displayed = bool(shown and shown <= now)
    used = exposure.get("used_by_rep") if displayed else None
    return {
        "generated": 1,
        "displayed": int(displayed),
        "reported_used": int(used is True),
        "reported_not_used": int(used is False),
        "use_unknown": int(used is None),
        "observed_in_speech": int(
            displayed
            and bool(
                observed_strategy_uses(
                    [r for r in observations if r.get("call_id") == event["call_id"]], event["id"]
                )
            )
        ),
    }


def comparison_from_counts(study, values, grouped, *, now):
    """One inference policy over unique-lead, stratum, exposure and quality totals."""
    goal, window_days = study["goal"], study["window_days"]
    result = trial_from_counts(values, goal=goal, window_days=window_days)
    assigned = sum(result["arms"][a]["leads"] for a in ("control", "candidate"))
    result.update(
        **study,
        original_eligible_leads=len(study["population"]),
        assigned_leads=assigned,
        unassigned_leads=len(study["population"]) - assigned,
        rep_strata=[],
        cluster_count=0,
    )
    result.pop("population", None)
    field = "cluster" if study["family"] == "number_allocation" else "rep_id"
    strata = [
        {"id": key, **trial_from_counts(counts, goal=goal, window_days=window_days)}
        for key, counts in sorted(grouped.items(), key=lambda item: str(item[0]))
    ]
    if field == "cluster":
        result["cluster_count"] = len(strata)
        summaries = {}
        for arm in ("control", "candidate"):
            rates = [r["arms"][arm]["rate"] for r in strata if r["arms"][arm]["rate"] is not None]
            mean = sum(rates) / len(rates) if rates else None
            se = (
                math.sqrt(sum((r - mean) ** 2 for r in rates) / (len(rates) - 1) / len(rates))
                if len(rates) > 1
                else None
            )
            summaries[arm] = {
                "clusters": len(rates),
                "mean_cluster_conversion": mean,
                "confidence_low": max(0, mean - 1.96 * se) if se is not None else None,
                "confidence_high": min(1, mean + 1.96 * se) if se is not None else None,
            }
        result["cluster_summary"] = summaries
        a, b = summaries["control"], summaries["candidate"]
        result["sufficient_evidence"] &= min(a["clusters"], b["clusters"]) >= 20 and (
            b["confidence_low"] is not None
            and a["confidence_high"] is not None
            and b["confidence_low"] > a["confidence_high"]
        )
        result["inference_unit"] = "region cluster; lead intervals are descriptive only"
    else:
        result["rep_strata"] = strata
        # Do not let a pooled result hide a loss within a rep or an unbalanced protocol.
        balanced = bool(study.get("strategy_ids")) or all(
            abs(r["arms"]["control"]["leads"] - r["arms"]["candidate"]["leads"]) <= 1 for r in strata
        )
        result["sufficient_evidence"] &= balanced and all(
            r["arms"]["candidate"]["rate"] is not None
            and r["arms"]["control"]["rate"] is not None
            and r["arms"]["candidate"]["rate"] >= r["arms"]["control"]["rate"]
            for r in strata
        )
        result["inference_unit"] = (
            "lead; stable assignment within rep strata"
            if study.get("strategy_ids")
            else "lead; randomized two-lead blocks within rep"
        )
    result["coaching_quality"] = {}
    for arm in ("control", "candidate"):
        prefix = f"quality:{arm}:"
        quality = {"reviewed_cards": values.get(prefix + "reviewed_cards", 0)}
        for key in ("useful", "grounded", "timely", "distracting"):
            n = values.get(prefix + key + "_count", 0)
            quality[key + "_fraction"] = values.get(prefix + key + "_true", 0) / n if n else None
        result["coaching_quality"][arm] = quality
    if now < timestamp(study["ends_at"]) + timedelta(days=window_days):
        result["sufficient_evidence"] = False
    if study.get("strategy_ids"):
        result["strategy_policy_snapshot"] = distribution_from_counts(study["strategy_ids"], values)
        result["strategy_exposure"] = {
            **{
                key: values.get("exposure:" + key, 0)
                for key in (
                    "generated",
                    "displayed",
                    "reported_used",
                    "reported_not_used",
                    "use_unknown",
                    "observed_in_speech",
                )
            },
            "scope": "Assignment, authenticated display, reported use and reviewed matching speech are separate; none proves adoption or conversion",
        }
        result["allocation_policy"] = study["strategy_policy"]
        if study["strategy_policy"] == "adaptive":
            result["sufficient_evidence"] = False
            result["inference_unit"] += "; adaptive rewards, not fixed-arm causal evidence"
    result["evidence_scope"] = (
        "Reviewed controlled software protocol; absent outcomes and unassigned leads remain visible"
    )
    return result


class ComparisonWorkflows:
    def strategy_exposures_in(self, s, p, state):
        from sqlalchemy import select

        from app.db import MODELS

        ids = state.data.get("displayed_coach_event_ids", [])[-8:] if state else []
        if not ids:
            return []
        catalog = {r["id"]: r for r in self.strategy_catalog(s, p)}
        model = MODELS["coach_events"]
        rows = s.scalars(
            select(model).where(
                model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"], model.id.in_(ids)
            )
        )
        return [
            {
                "id": r.id,
                **{
                    k: r.data["strategy_exposure"].get(k)
                    for k in ("strategy_id", "source_version", "displayed_at")
                },
                "scope": "Displayed approach identity; speech matches do not prove adoption or effect",
                "approach": catalog[r.data["strategy_exposure"]["strategy_id"]]["text"]
                if r.data["strategy_exposure"].get("strategy_id") != "control"
                and catalog.get(r.data["strategy_exposure"].get("strategy_id"), {}).get("source_version")
                == r.data["strategy_exposure"].get("source_version")
                else None,
            }
            for r in rows
            if r.data.get("strategy_exposure", {}).get("displayed_at")
        ]

    def strategy_catalog(self, s, p):
        from app.coaching.library import examples

        return [
            {"id": "control", "source_version": 1, "text": "Contextual help without a prescribed approach"},
            *[
                {"id": item["id"], "source_version": item["version"], "text": item["text"]}
                for item in examples(self, s, p)
            ],
        ]

    def strategy_context(self, s, p, campaign, lead, assignment):
        """Current treatment authorization; the historical assignment is never overwritten."""
        default = {
            "strategy_id": "control",
            "probabilities": {"control": 1.0},
            "comparison_id": None,
            "assignment_id": None,
            "source_version": 1,
            "reason": "ordinary_contextual_help",
            "approach": None,
        }
        if not assignment or not assignment.get("strategy_id"):
            return default
        default.update(comparison_id=assignment["comparison_id"], assignment_id=assignment.get("id"))
        study = next(
            (
                r
                for r in campaign.data.get("controlled_comparisons", [])
                if r["id"] == assignment["comparison_id"]
            ),
            None,
        )
        config = self.optimization_config(s, p, campaign.id)
        if (
            not study
            or study.get("stopped_at")
            or timestamp(study["ends_at"]) <= self.clock()
            or study["goal"] != config["conversion_goal"]
            or study["window_days"] != config["window_days"]
        ):
            return {**default, "reason": "protocol_inactive"}
        catalog = {item["id"]: item for item in self.strategy_catalog(s, p)}
        if any(
            catalog.get(item["id"], {}).get("source_version") != item["source_version"]
            for item in study["strategies"]
        ):
            return {**default, "reason": "source_changed"}
        if lead.data.get("requested_contact") or any(
            timestamp(lead.data.get(key)) and timestamp(lead.data[key]) <= self.clock()
            for key in ("callback_due_at", "coverage_due_at")
        ):
            return {**default, "reason": "protected_work"}
        strategy = catalog[assignment["strategy_id"]]
        return {
            **default,
            "strategy_id": strategy["id"],
            "source_version": strategy["source_version"],
            "probabilities": assignment["strategy_probabilities"],
            "reason": "reviewed_finite_strategy",
            "approach": strategy["text"] if strategy["id"] != "control" else None,
        }

    def comparison_assignment(self, s, p, campaign, lead, family, *, rep_id=None, persist=True):
        studies = campaign.data.get("controlled_comparisons", [])
        study = next(
            (
                r
                for r in reversed(studies)
                if r["family"] == family
                and not r.get("stopped_at")
                and timestamp(r["ends_at"]) > self.clock()
            ),
            None,
        )
        if not study or lead.id not in study["population"]:
            return None
        key = study["id"] + ":" + lead.id
        existing = self._by_key(s, p, "experiment_assignments", key)
        if existing:
            return doc(existing)
        if study.get("strategy_ids"):
            if not rep_id:
                return None
            choice = strategy_choice(study, lead.id, rep_id)
            if study["strategy_policy"] == "adaptive":
                saved = self._by_key(s, p, "report_projections", study["report_key"])
                fresh = bool(
                    saved
                    and saved.data["source_revision"] == self.report_revision(s, p)
                    and timestamp(saved.data["expires_at"]) > self.clock()
                )
                report = next(
                    (
                        r
                        for r in (saved.data["report"].get("comparisons", []) if saved else [])
                        if r["id"] == study["id"] and r.get("strategy_policy_snapshot")
                    ),
                    None,
                )
                # Stale rewards hold the last policy actually in force (else the approved initial
                # one) with its true mixture probabilities; never a deterministic control arm.
                choice = {
                    **strategy_choice(
                        study,
                        lead.id,
                        rep_id,
                        report["strategy_policy_snapshot"] if report else study["initial_strategy_policy"],
                    ),
                    "policy_reason": "fresh_reward_projection"
                    if report and fresh
                    else "held_last_policy"
                    if report
                    else "approved_initial_policy",
                }
            values = {
                "family": family,
                "comparison_id": study["id"],
                "lead_id": lead.id,
                "rep_id": rep_id,
                "segment": lead.data.get("segment"),
                **choice,
                "assigned_at": self.clock().isoformat(),
                "coverage_due_at": lead.data.get("coverage_due_at"),
                "originally_eligible": True,
                "goal": study["goal"],
                "outcome_window_days": study["window_days"],
            }
            return (
                doc(
                    put(
                        s,
                        "experiment_assignments",
                        p["tenant_id"],
                        p["seller_id"],
                        values,
                        key=key,
                        now=self.clock(),
                    )
                )
                if persist
                else values
            )
        if family == "coaching" and not rep_id:
            return None
        stratum = lead.data.get("region") or "general" if family == "number_allocation" else rep_id
        rep_count = 0
        if family == "coaching":
            model = MODELS["experiment_assignments"]
            rep_count = s.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.external_key.startswith(study["id"] + ":", autoescape=True),
                    model.op_owner_id == rep_id,
                )
            )
        arm = assign_arm(family, study["id"], lead.id, stratum, prior_count=rep_count)
        if not persist:
            return {"comparison_id": study["id"], "arm": arm, "family": family}
        result = put(
            s,
            "experiment_assignments",
            p["tenant_id"],
            p["seller_id"],
            {
                "family": family,
                "comparison_id": study["id"],
                "lead_id": lead.id,
                "arm": arm,
                "assigned_at": self.clock().isoformat(),
                "cluster": stratum if family == "number_allocation" else None,
                "rep_id": rep_id,
                "coverage_due_at": lead.data.get("coverage_due_at"),
                # Both block positions are marginally randomized at one half; the second is only
                # conditionally determined, which the block position records.
                "propensity": 0.5,
                "block_position": rep_count % 2 if family == "coaching" else None,
                "originally_eligible": True,
                "goal": study["goal"],
                "outcome_window_days": study["window_days"],
            },
            key=key,
            now=self.clock(),
        )
        return doc(result)
