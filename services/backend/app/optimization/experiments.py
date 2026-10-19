"""Stable optional experiments; assignments precede treatment and never change permissions."""

import hashlib
import math
from collections import defaultdict
from datetime import timedelta

from app.db import doc, put
from app.optimization.learning import (
    conversion_at,
    derive_labels,
    learn_distribution,
    outcome_at,
    trial_report,
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
    draw = int(hashlib.sha256(f"{study['id']}:{rep_id}:{lead_id}".encode()).hexdigest()[:16], 16) / 2**64
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


def assign_arm(family, epoch, lead_id, stratum, assignments):
    if family == "number_allocation":
        unit = stratum
    else:
        prior = [a for a in assignments if a["family"] == family and a.get("rep_id") == stratum]
        block, position = divmod(len(prior), 2)
        unit = stratum + ":" + str(block)
        if position:
            return "candidate" if prior[-1]["arm"] == "control" else "control"
    return (
        "candidate"
        if int(hashlib.sha256((epoch + ":" + unit).encode()).hexdigest()[:8], 16) % 2
        else "control"
    )


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
    events = events + call_goal_events(calls, goal, now=now)
    if study.get("strategy_ids"):
        events = [
            known
            for event in events
            if event.get("id")
            and (known := conversion_at(event, now))
            and known["timing_known"]
            and not known["retracted"]
        ]
    selected = [a for a in assignments if a.get("comparison_id") == study["id"]]
    by_lead = defaultdict(list)
    for call in calls:
        by_lead[call["lead_id"]].append(call)
    enriched = []
    for item in selected:
        attempts = [
            c
            for c in by_lead[item["lead_id"]]
            if timestamp(c["started_at"]) >= timestamp(item["assigned_at"]) and c.get("submitted_at")
        ]
        due = timestamp(item.get("coverage_due_at"))
        enriched.append(
            {
                **item,
                "covered": any(not due or timestamp(c["started_at"]) <= due for c in attempts),
                "policy_unknown": sum(c.get("submission_allowed") is None for c in attempts),
                "policy_violations": sum(c.get("submission_allowed") is False for c in attempts),
            }
        )
    result = trial_report(enriched, events, now=now, goal=goal, window_days=window_days)
    result.update(
        **study,
        original_eligible_leads=len(study["population"]),
        assigned_leads=len(selected),
        unassigned_leads=len(study["population"]) - len(selected),
        rep_strata=[],
        cluster_count=0,
    )
    result.pop("population", None)
    grouped = defaultdict(list)
    field = "cluster" if study["family"] == "number_allocation" else "rep_id"
    for item in enriched:
        grouped[item.get(field, "unknown")].append(item)
    strata = [
        {"id": key, **trial_report(values, events, now=now, goal=goal, window_days=window_days)}
        for key, values in grouped.items()
    ]
    if field == "cluster":
        result["cluster_count"] = len(grouped)
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
    arm_by_call = {c["id"]: a["arm"] for a in selected for c in by_lead[a["lead_id"]]}
    result["coaching_quality"] = {}
    for arm in ("control", "candidate"):
        reviews = [r for r in feedback if arm_by_call.get(r["call_id"]) == arm]
        result["coaching_quality"][arm] = {"reviewed_cards": len(reviews)}
        for key in ("useful", "grounded", "timely", "distracting"):
            known = [r[key] for r in reviews if r.get(key) is not None]
            result["coaching_quality"][arm][key + "_fraction"] = sum(known) / len(known) if known else None
    if now < timestamp(study["ends_at"]) + timedelta(days=window_days):
        result["sufficient_evidence"] = False
    if study.get("strategy_ids"):
        rewards = strategy_rewards(study, selected, events, now=now)
        # Earlier approved evidence and current intent-to-treat rewards share one
        # lead unit; retries/re-enrolment cannot manufacture extra support.
        unique = {}
        for reward in [*seed_rewards, *rewards]:
            unique.setdefault(reward["lead_id"], reward)
        result["strategy_policy_snapshot"] = learn_distribution(
            study["strategy_ids"], list(unique.values()), exploration=0.1
        )
        cards = [r for r in exposures if r.get("strategy_exposure", {}).get("comparison_id") == study["id"]]
        shown = [r["strategy_exposure"] for r in cards]
        by_call = defaultdict(list)
        for item in observations:
            by_call[item.get("call_id")].append(item)
        result["strategy_exposure"] = {
            "generated": len(shown),
            "displayed": sum(bool(r.get("displayed_at")) for r in shown),
            "reported_used": sum(r.get("used_by_rep") is True for r in shown),
            "reported_not_used": sum(r.get("used_by_rep") is False for r in shown),
            "use_unknown": sum(r.get("used_by_rep") is None for r in shown),
            "observed_in_speech": sum(
                bool(r["strategy_exposure"].get("displayed_at"))
                and bool(observed_strategy_uses(by_call[r["call_id"]], r["id"]))
                for r in cards
            ),
            "scope": "Assignment, authenticated display, reported use and reviewed matching speech are separate; none proves adoption or conversion",
        }
        result["allocation_policy"] = study["strategy_policy"]
        if study["strategy_policy"] == "adaptive":
            # This descriptive adaptive policy report is not a new fixed-arm
            # confirmation experiment or authorization for another promotion.
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
                report = (
                    next(
                        (r for r in saved.data["report"].get("comparisons", []) if r["id"] == study["id"]),
                        None,
                    )
                    if fresh
                    else None
                )
                if report:
                    choice = strategy_choice(study, lead.id, rep_id, report["strategy_policy_snapshot"])
                else:
                    choice = {
                        "strategy_id": "control",
                        "strategy_probabilities": {a: float(a == "control") for a in study["strategy_ids"]},
                        "propensity": 1.0,
                        "arm": "control",
                        "policy_reason": "fresh_reward_projection_pending",
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
        assignments = [
            r
            for r in self.scoped(s, "experiment_assignments", p)
            if r.data.get("comparison_id") == study["id"]
        ]
        existing = next((r for r in assignments if r.external_key == key), None)
        if existing:
            return doc(existing)
        if family == "coaching" and not rep_id:
            return None
        stratum = lead.data.get("region") or "general" if family == "number_allocation" else rep_id
        prior = [r.data for r in assignments]
        arm = assign_arm(family, study["id"], lead.id, stratum, prior)
        rep_count = sum(r.get("rep_id") == rep_id for r in prior)
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
                "propensity": 1 if family == "coaching" and rep_count % 2 else 0.5,
                "originally_eligible": True,
                "goal": study["goal"],
                "outcome_window_days": study["window_days"],
            },
            key=key,
            now=self.clock(),
        )
        return doc(result)

    def comparison_reports(self, s, p, campaign, calls, events, feedback, exposures=()):
        from sqlalchemy import select

        from app.db import MODELS

        assignments = [r.data for r in self.scoped(s, "experiment_assignments", p)]
        decisions = {r.id: r.data for r in self.scoped(s, "compliance_decisions", p)}
        observed_calls = [
            {
                **doc(c),
                "submission_allowed": decisions.get(c.data.get("submission_decision_id"), {}).get("allowed"),
            }
            for c in calls
        ]
        studies = campaign.data.get("controlled_comparisons", [])
        # Content-free lineage survives context trimming and raw expiry. Revisions
        # are folded by identity so a correction removes the prior speech match.
        latest = {}
        call_ids = {r["call_id"] for r in exposures if r.get("strategy_exposure", {}).get("comparison_id")}
        if call_ids:
            model = MODELS["outcomes"]
            for row in s.scalars(
                select(model).where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.ctx_status == "conversation_lineage",
                    model.ctx_key.in_(call_ids),
                )
            ):
                value = row.data
                key = (value["call_id"], value["id"])
                if value["version"] >= latest.get(key, {}).get("version", 0):
                    latest[key] = value
        return [
            comparison_report(
                study,
                assignments,
                observed_calls,
                events,
                feedback,
                now=self.clock(),
                goal=study["goal"],
                window_days=study["window_days"],
                exposures=exposures,
                observations=list(latest.values()),
                seed_rewards=[
                    r
                    for prior in studies
                    if prior["id"] == study.get("evidence_comparison_id")
                    for r in strategy_rewards(
                        prior,
                        assignments,
                        events + call_goal_events(observed_calls, prior["goal"], now=self.clock()),
                        now=self.clock(),
                    )
                ],
            )
            for study in studies
        ]
