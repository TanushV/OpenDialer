"""Stable optional experiments; assignments precede treatment and never change permissions."""

import hashlib
import math
from collections import defaultdict
from datetime import timedelta

from app.db import doc, put
from app.optimization.learning import derive_labels, trial_report
from app.optimization.numbering import timestamp


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


def comparison_report(study, assignments, calls, events, feedback, *, now, goal, window_days):
    events = events + [
        {"lead_id": call["lead_id"], "kind": goal, "occurred_at": call.get("ended_at") or call["started_at"]}
        for call in calls
        if derive_labels(call.get("outcome")).get("qualified" if goal == "qualified_conversation" else goal)
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
        balanced = all(
            abs(r["arms"]["control"]["leads"] - r["arms"]["candidate"]["leads"]) <= 1 for r in strata
        )
        result["sufficient_evidence"] &= balanced and all(
            r["arms"]["candidate"]["rate"] is not None
            and r["arms"]["control"]["rate"] is not None
            and r["arms"]["candidate"]["rate"] >= r["arms"]["control"]["rate"]
            for r in strata
        )
        result["inference_unit"] = "lead; randomized two-lead blocks within rep"
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
    result["evidence_scope"] = (
        "Reviewed controlled software protocol; absent outcomes and unassigned leads remain visible"
    )
    return result


class ComparisonWorkflows:
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

    def comparison_reports(self, s, p, campaign, calls, events, feedback):
        assignments = [r.data for r in self.scoped(s, "experiment_assignments", p)]
        decisions = {r.id: r.data for r in self.scoped(s, "compliance_decisions", p)}
        observed_calls = [
            {
                **doc(c),
                "submission_allowed": decisions.get(c.data.get("submission_decision_id"), {}).get("allowed"),
            }
            for c in calls
        ]
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
            )
            for study in campaign.data.get("controlled_comparisons", [])
        ]
