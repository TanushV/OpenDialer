import hashlib
import json
from datetime import timedelta

from sqlalchemy import select

from app.db import ActiveClaim, audit, doc, emit, get, put, update
from app.optimization.experiments import ComparisonWorkflows
from app.optimization.learning import (
    derive_labels,
    drift_report,
    freeze_features,
    future_slots,
    training_rows,
    trial_report,
)
from app.optimization.numbering import capacity_forecast, number_health, timestamp

DEFAULTS = {
    "conversion_goal": "meeting_held",
    "window_days": 14,
    "exploration": 0.1,
    "auto_train": True,
    "horizon_hours": 24,
    "rep_minutes": 480,
    "handling_seconds": 120,
    "daily_capacity_per_number": 40,
    "training_min_rows": 100,
}


class OptimizationWorkflows(ComparisonWorkflows):
    def ensure_experiment_cohort(self, s, p, campaign_id=None):
        from app.optimization.learning import stable_control

        campaign = (
            self.required(s, "campaigns", campaign_id, p)
            if campaign_id
            else self.scoped(s, "campaigns", p)[0]
        )
        epoch = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
        existing = {r.external_key for r in self.scoped(s, "experiment_assignments", p)}
        study = campaign.data.get("optimization_trial") or {}
        if study and timestamp(study["ends_at"]) < self.clock():
            return epoch
        candidates = self.scoped(s, "leads", p)
        if study:
            candidates = sorted(
                candidates, key=lambda lead: hashlib.sha256((epoch + lead.id).encode()).hexdigest()
            )[: study["max_leads"]]
        for lead in candidates:
            key = epoch + ":" + lead.id
            if (
                lead.data.get("campaign_id") != campaign.id
                or not lead.data.get("originally_eligible", True)
                or key in existing
            ):
                continue
            put(
                s,
                "experiment_assignments",
                p["tenant_id"],
                p["seller_id"],
                {
                    "lead_id": lead.id,
                    "arm": "control" if stable_control(lead.id) else "candidate",
                    "assigned_at": self.clock().isoformat(),
                    "epoch": epoch,
                    "originally_eligible": True,
                    "coverage_due_at": lead.data.get("coverage_due_at"),
                    "policy_version": "conversion-coverage-v2",
                    "family": "optimization",
                    "trial_model_id": study.get("model_id"),
                    "goal": self.optimization_config(s, p)["conversion_goal"],
                    "outcome_window_days": self.optimization_config(s, p)["window_days"],
                    "assignment_source": "enrollment_before_optimization; no historical treatment imputed",
                },
                key=key,
                now=self.clock(),
            )
        return epoch

    def serving_model(self, s, p, config, lead_id=None):
        from app.optimization.learning import stable_control

        if lead_id and stable_control(lead_id):
            return None
        models = self.scoped(s, "model_versions", p)
        campaign = self.scoped(s, "campaigns", p)[0]
        epoch = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
        enrolled = {
            r.data["lead_id"]
            for r in self.scoped(s, "experiment_assignments", p)
            if r.data.get("epoch") == epoch and r.data.get("family", "optimization") == "optimization"
        }
        eligible = [
            model
            for model in models
            if model.data.get("goal", config["conversion_goal"]) == config["conversion_goal"]
            and (
                model.data.get("active")
                or model.data.get("trial_active")
                and timestamp(model.data.get("trial_ends_at"))
                and timestamp(model.data["trial_ends_at"]) > self.clock()
                and (lead_id is None or lead_id in enrolled)
            )
        ]
        return next((m for m in eligible if m.data.get("trial_active")), eligible[0] if eligible else None)

    def slot_predictor(self, s, p, lead, config):
        from app.ml.training import predict_funnel

        model = self.serving_model(s, p, config, lead.id)
        if model is None:
            return None
        features = self.prediction_features(s, p, lead)

        def predict(hour, weekday):
            return predict_funnel(
                model.data,
                {**features, "local_hour": hour, "local_weekday": weekday},
                self.settings.data_dir / "models",
            )

        return predict

    def reconcile_goal_status(self, s, p, lead):
        config = self.optimization_config(s, p, lead.data["campaign_id"])
        goal = config["conversion_goal"]
        label = "qualified" if goal == "qualified_conversation" else goal
        calls = [c for c in self.scoped(s, "call_attempts", p) if c.data["lead_id"] == lead.id]
        achieved = any(derive_labels(c.data.get("outcome")).get(label) for c in calls)
        achieved |= any(
            r.data["lead_id"] == lead.id and r.data["kind"] == goal and not r.data.get("retracted")
            for r in self.scoped(s, "conversion_events", p)
        )
        if achieved:
            update(
                lead,
                conversion_goal_achieved=goal,
                status="converted" if lead.data.get("status") == "eligible" else lead.data.get("status"),
            )
        elif lead.data.get("conversion_goal_achieved") == goal:
            update(
                lead,
                conversion_goal_achieved=None,
                status="needs_review"
                if self.suppressed(s, p, lead.data["phone"])
                else "eligible"
                if lead.data.get("status") == "converted"
                else lead.data.get("status"),
            )

    def schedule_optimization(self, tenant_id=None):
        from app.db import rows

        slot = int(self.clock().timestamp()) // 600
        with self.db.transaction(False) as s:
            users = [
                doc(user) | {"tenant_id": user.tenant_id, "seller_id": user.seller_id}
                for user in rows(s, "users", tenant_id)
                if user.data.get("role") in {"owner", "admin"} and not user.data.get("disabled")
            ]
        seen = set()
        for user in users:
            identity = (user["tenant_id"], user["seller_id"])
            if identity in seen:
                continue
            seen.add(identity)
            self.jobs.enqueue(
                "ml",
                {"kind": "optimization_refresh", "seller_id": user["seller_id"], "user_id": user["id"]},
                f"optimization-refresh:{user['seller_id']}:{slot}",
                tenant_id=user["tenant_id"],
            )

    def optimization_config(self, s, p, campaign_id=None):
        campaign = (
            self.required(s, "campaigns", campaign_id, p)
            if campaign_id
            else self.scoped(s, "campaigns", p)[0]
        )
        return {**DEFAULTS, **campaign.data.get("optimization", {})}

    def inventory_evidence(self, s, p):
        calls = [doc(c) for c in self.scoped(s, "call_attempts", p)]
        snapshots = {r.id: r.data for r in self.scoped(s, "feature_snapshots", p)}
        attempts = []
        for call in calls:
            if not call.get("outcome") and call.get("end_reason") not in {
                "provider_failure",
                "route_failure",
            }:
                continue
            at, ended = timestamp(call.get("started_at")), timestamp(call.get("ended_at"))
            predicted = snapshots.get(call.get("feature_snapshot_id"), {}).get("predictions", {})
            attempts.append(
                {
                    **call,
                    "duration_seconds": max(0, (ended - at).total_seconds()) if at and ended else 0,
                    "delivery_failed": call.get("end_reason") in {"provider_failure", "route_failure"},
                    "expected_human": predicted.get("p_human"),
                }
            )
        claims = list(
            s.scalars(
                select(ActiveClaim).where(
                    ActiveClaim.tenant_id == p["tenant_id"], ActiveClaim.seller_id == p["seller_id"]
                )
            )
        )
        leases = [lease.data for lease in self.scoped(s, "number_leases", p) if lease.data.get("active")]
        callbacks = [r.data for r in self.scoped(s, "followup_tasks", p) if r.data.get("inbound")]
        inventory = []
        for number in self.scoped(s, "number_inventory", p):
            health = number_health(
                [a for a in attempts if a.get("number_id") == number.id],
                pooled_attempts=attempts,
                now=self.clock(),
            )
            health["callbacks"] = sum(c.get("number_id") == number.id for c in callbacks)
            used = sum(
                c.get("number_id") == number.id and timestamp(c["started_at"]).date() == self.clock().date()
                for c in calls
            )
            inventory.append(
                {
                    **doc(number),
                    "health": health,
                    "used_today": used,
                    "reserved_attempts": sum(c.number_id == number.id and not c.call_id for c in claims),
                    "active_obligations": sum(c.number_id == number.id for c in claims)
                    + sum(lease["number_id"] == number.id for lease in leases),
                }
            )
        return inventory

    def operational_dataset(self, s, p, config=None):
        config = config or self.optimization_config(s, p)
        calls = [doc(c) for c in self.scoped(s, "call_attempts", p)]
        snapshots = [doc(r) for r in self.scoped(s, "feature_snapshots", p)]
        events = [doc(r) for r in self.scoped(s, "conversion_events", p)]
        costs = {}
        for record in self.scoped(s, "usage_ledger", p):
            if (
                record.data.get("call_id")
                and record.data.get("cost_usd") is not None
                and record.data.get("provider_record_id")
            ):
                key = record.data["call_id"]
                costs[key] = costs.get(key, 0) + float(record.data["cost_usd"])
        by_snapshot = {r["id"]: r for r in snapshots}
        for call in calls:
            snapshot = by_snapshot.get(call.get("feature_snapshot_id"))
            if snapshot and call["id"] in costs:
                snapshot["observed_cost_usd"] = costs[call["id"]]
        data = training_rows(
            calls,
            snapshots,
            events,
            now=self.clock(),
            goal=config["conversion_goal"],
            window_days=config["window_days"],
        )
        return data

    def prediction_features(self, s, p, lead, number=None, calls=None):
        endpoint = get(s, "phone_endpoints", lead.data["endpoint_id"], p["tenant_id"])
        inventory = self.inventory_evidence(s, p)
        selected = next((n for n in inventory if number and n["id"] == number.id), None)
        health = selected["health"] if selected else {"score": None, "samples": 0}
        return freeze_features(
            {
                **doc(lead),
                "created_at": lead.data.get("imported_at", lead.created_at.isoformat()),
                "ownership_status": endpoint.data.get("ownership_status") if endpoint else None,
            },
            health=health,
            now=self.clock(),
            calls=calls if calls is not None else [doc(c) for c in self.scoped(s, "call_attempts", p)],
        )

    def forecast(self, s, p, *, campaign_id=None, capacity=None):
        config = self.optimization_config(s, p, campaign_id)
        campaign = (
            self.required(s, "campaigns", campaign_id, p)
            if campaign_id
            else self.scoped(s, "campaigns", p)[0]
        )
        policy = self.required(s, "policy_versions", campaign.data["policy_id"], p).data
        leads = [
            {**doc(lead), "suppressed": self.suppressed(s, p, lead.data["phone"])}
            for lead in self.scoped(s, "leads", p)
            if lead.data["campaign_id"] == campaign.id
        ]
        return capacity_forecast(
            leads,
            self.inventory_evidence(s, p),
            now=self.clock(),
            horizon_hours=config["horizon_hours"],
            rep_minutes=config["rep_minutes"],
            handling_seconds=config["handling_seconds"],
            daily_capacity_per_number=capacity or config["daily_capacity_per_number"],
            policy=policy,
        )

    def optimization_report(self, p, epoch=None):
        with self.db.transaction(False) as s:
            config = self.optimization_config(s, p)
            data = self.operational_dataset(s, p, config)
            leads = self.scoped(s, "leads", p)
            calls = self.scoped(s, "call_attempts", p)
            known_calls = {c.id for c in calls if p["role"] != "rep" or c.data["rep_id"] == p["user_id"]}
            events = [doc(r) for r in self.scoped(s, "conversion_events", p)]
            original = sum(lead.data.get("originally_eligible", True) for lead in leads)
            converted = {
                e["lead_id"]
                for e in events
                if e["kind"] == config["conversion_goal"] and not e.get("retracted")
            }
            goal_label = (
                "qualified"
                if config["conversion_goal"] == "qualified_conversation"
                else config["conversion_goal"]
            )
            converted |= {
                c.data["lead_id"] for c in calls if derive_labels(c.data.get("outcome")).get(goal_label)
            }
            feedback = [
                r.data for r in self.scoped(s, "coaching_feedback", p) if r.data["call_id"] in known_calls
            ]
            coach_events = [
                r.data for r in self.scoped(s, "coach_events", p) if r.data["call_id"] in known_calls
            ]
            latency = sorted(r["latency"]["inference_ms"] for r in coach_events if r.get("latency"))
            delivery = sorted(r["delivery_ms"] for r in feedback if r.get("delivery_ms") is not None)
            campaign = self.scoped(s, "campaigns", p)[0]
            current_epoch = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
            epoch = epoch or current_epoch
            historical = [
                r
                for r in self.scoped(s, "experiment_assignments", p)
                if r.data.get("epoch") == epoch and r.data.get("family", "optimization") == "optimization"
            ]
            if epoch != current_epoch and not historical:
                from app.service import fail

                fail(404, "Controlled cohort not found")
            trial_goal = (
                historical[0].data.get("goal", config["conversion_goal"])
                if historical
                else config["conversion_goal"]
            )
            trial_days = (
                historical[0].data.get("outcome_window_days", 14) if historical else config["window_days"]
            )
            decisions_by_id = {r.id: r.data for r in self.scoped(s, "compliance_decisions", p)}
            assignments = []
            for record in self.scoped(s, "experiment_assignments", p):
                if (
                    record.data.get("epoch") != epoch
                    or record.data.get("family", "optimization") != "optimization"
                ):
                    continue
                assigned = timestamp(record.data["assigned_at"])
                attempts = [
                    c
                    for c in calls
                    if c.data["lead_id"] == record.data["lead_id"]
                    and timestamp(c.data["started_at"]) >= assigned
                    and (c.data.get("provider_id") or c.data.get("submitted_at"))
                ]
                due = timestamp(record.data.get("coverage_due_at"))
                evidence = [decisions_by_id.get(c.data.get("submission_decision_id")) for c in attempts]
                assignments.append(
                    {
                        **record.data,
                        "covered": any(not due or timestamp(c.data["started_at"]) <= due for c in attempts),
                        "policy_unknown": sum(e is None for e in evidence),
                        "policy_violations": sum(
                            e is not None and not e.get("allowed", False) for e in evidence
                        ),
                    }
                )
            derived_events = events + [
                {
                    "lead_id": c.data["lead_id"],
                    "kind": trial_goal,
                    "occurred_at": c.data.get("ended_at", c.data["started_at"]),
                }
                for c in calls
                if derive_labels(c.data.get("outcome")).get(
                    "qualified" if trial_goal == "qualified_conversation" else trial_goal
                )
            ]
            trial = trial_report(
                assignments,
                derived_events,
                now=self.clock(),
                goal=trial_goal,
                window_days=trial_days,
            )
            study = campaign.data.get("optimization_trial") or {}
            if epoch != current_epoch:
                study = next(
                    (
                        r.data.get("trial_study", {})
                        for r in self.scoped(s, "model_versions", p)
                        if r.data.get("trial_epoch") == epoch
                    ),
                    {},
                )
            trial.update(epoch=epoch, trial_model_id=study.get("model_id"), study=study)
            exposure = {
                "candidate_tested_model_attempts": 0,
                "candidate_other_model_attempts": 0,
                "control_tested_model_attempts": 0,
            }
            if study:
                snapshots = {r.id: r.data for r in self.scoped(s, "feature_snapshots", p)}
                by_lead = {a["lead_id"]: a for a in assignments}
                for call in calls:
                    assigned = by_lead.get(call.data["lead_id"])
                    if not assigned or not call.data.get("submitted_at"):
                        continue
                    started = timestamp(call.data["started_at"])
                    if not timestamp(assigned["assigned_at"]) <= started <= timestamp(study["ends_at"]):
                        continue
                    actual = (
                        snapshots.get(call.data.get("feature_snapshot_id"), {})
                        .get("predictions", {})
                        .get("model_version")
                    )
                    tested = actual == study["model_id"]
                    key = (
                        ("candidate_tested_model_attempts" if tested else "candidate_other_model_attempts")
                        if assigned["arm"] == "candidate"
                        else "control_tested_model_attempts"
                        if tested
                        else None
                    )
                    if key:
                        exposure[key] += 1
                trial["sufficient_evidence"] &= (
                    exposure["candidate_tested_model_attempts"] > 0
                    and exposure["candidate_other_model_attempts"] == 0
                    and exposure["control_tested_model_attempts"] == 0
                )
            else:
                trial["sufficient_evidence"] = False
            trial["model_exposure"] = exposure
            if study and self.clock() < timestamp(study["ends_at"]) + timedelta(days=trial_days):
                trial["sufficient_evidence"] = False
                trial["analysis_status"] = "Enrollment and all predeclared outcome windows must close"
            decision_rows = self.scoped(s, "policy_decisions", p)
            decisions = [
                doc(r) for r in decision_rows if p["role"] != "rep" or r.data.get("rep_id") == p["user_id"]
            ]
            plans = [doc(r) for r in self.scoped(s, "scheduling_plans", p)]
            count = len(data)
            midpoint = max(0, count - max(20, count // 3))
            model_drift = drift_report(data[:midpoint], data[midpoint:])
            futures = []
            policy = self.required(s, "policy_versions", campaign.data["policy_id"], p).data
            for lead in leads[:500]:
                if lead.data.get("status") != "eligible" or self.suppressed(s, p, lead.data["phone"]):
                    continue
                candidates = future_slots(
                    doc(lead),
                    data,
                    now=self.clock(),
                    policy=policy,
                    predictor=self.slot_predictor(s, p, lead, config),
                )
                if candidates:
                    futures.append({**candidates[0], "name": lead.data["name"]})
            durations = sum(r.get("duration_seconds", 0) for r in data)
            return {
                "settings": config,
                "comparisons": self.comparison_reports(s, p, campaign, calls, derived_events, feedback),
                "cohort_history": sorted(
                    {
                        r.data["epoch"]
                        for r in self.scoped(s, "experiment_assignments", p)
                        if r.data.get("epoch") and r.data.get("family", "optimization") == "optimization"
                    }
                ),
                "forecast": self.forecast(s, p),
                "numbers": self.inventory_evidence(s, p),
                "metrics": {
                    "imported_leads": len(leads),
                    "original_eligible_leads": original,
                    "suppressed_now": sum(self.suppressed(s, p, lead.data["phone"]) for lead in leads),
                    "attempts": len(calls),
                    "human_answers": sum(derive_labels(c.data.get("outcome"))["human"] for c in calls),
                    "unique_right_party": len(
                        {
                            c.data["lead_id"]
                            for c in calls
                            if derive_labels(c.data.get("outcome"))["right_party"]
                        }
                    ),
                    "unique_conversions": len(converted),
                    "conversion_per_original_eligible_lead": len(converted) / original if original else None,
                    "conversions_per_rep_hour": len(converted) * 3600 / durations if durations else None,
                    "observed_cost_usd": sum(r.get("observed_cost_usd") or 0 for r in data),
                    "outcome_goal": config["conversion_goal"],
                },
                "dataset": {
                    "rows": count,
                    "pending_rows": sum(r["conversion"] is None for r in data),
                    "matured_rows": sum(r["window_closed"] for r in data),
                    "positive_conversions": sum(r["conversion"] == 1 for r in data),
                    "missing_snapshot_calls": sum(
                        bool(c.data.get("outcome")) and not c.data.get("feature_snapshot_id") for c in calls
                    ),
                    "snapshots": [
                        {
                            "id": r.id,
                            "rows": len(r.data.get("rows", [])),
                            "as_of": r.data["as_of"],
                            "goal": r.data["goal"],
                        }
                        for r in self.scoped(s, "learning_datasets", p)[-10:]
                    ],
                },
                "decisions": decisions[-30:],
                "future_slots": futures[:100],
                "plans": plans[-100:],
                "drift": model_drift,
                "trial": trial,
                "coaching": {
                    **{
                        "p95_" + key: sorted(
                            r["latency"][key]
                            for r in coach_events
                            if r.get("latency", {}).get(key) is not None
                        )[
                            max(
                                0,
                                int(
                                    sum(r.get("latency", {}).get(key) is not None for r in coach_events)
                                    * 0.95
                                    + 0.999
                                )
                                - 1,
                            )
                        ]
                        if any(r.get("latency", {}).get(key) is not None for r in coach_events)
                        else None
                        for key in ("state_ms", "retrieval_ms", "stt_ms")
                    },
                    "reviewed_cards": sum(
                        any(r.get(key) is not None for key in ("useful", "grounded", "timely", "distracting"))
                        for r in feedback
                    ),
                    **{
                        "p95_" + key: sorted(r[key] for r in feedback if r.get(key) is not None)[
                            max(0, int(sum(r.get(key) is not None for r in feedback) * 0.95 + 0.999) - 1)
                        ]
                        if any(r.get(key) is not None for r in feedback)
                        else None
                        for key in ("render_ms", "event_to_render_ms")
                    },
                    **{
                        key + "_fraction": sum(r.get(key) is True for r in feedback)
                        / sum(r.get(key) is not None for r in feedback)
                        if any(r.get(key) is not None for r in feedback)
                        else None
                        for key in ("useful", "grounded", "timely", "distracting")
                    },
                    "p95_inference_ms": latency[max(0, int(len(latency) * 0.95 + 0.999) - 1)]
                    if latency
                    else None,
                    "p95_delivery_ms": delivery[max(0, int(len(delivery) * 0.95 + 0.999) - 1)]
                    if delivery
                    else None,
                    "evidence_scope": "recorded provider and reviewer measurements; live STT target remains unverified",
                },
                "simulation": self.settings.simulation,
                "as_of": self.clock().isoformat(),
            }

    def save_operational_dataset(self, p):
        with self.db.transaction() as s:
            config = self.optimization_config(s, p)
            data = self.operational_dataset(s, p, config)
            # Bound training memory, retain the complete operational ledger as source of truth.
            data = data[-20000:]
            key = hashlib.sha256(
                json.dumps([config["conversion_goal"], config["window_days"], data], sort_keys=True).encode()
            ).hexdigest()
            previous = next(
                (r for r in self.scoped(s, "learning_datasets", p) if r.external_key == key), None
            )
            if previous:
                return doc(previous)
            result = put(
                s,
                "learning_datasets",
                p["tenant_id"],
                p["seller_id"],
                {
                    "goal": config["conversion_goal"],
                    "window_days": config["window_days"],
                    "as_of": self.clock().isoformat(),
                    "rows": data,
                    "checksum": key,
                    "source": "immutable_operational_feature_snapshots",
                    "max_training_rows": 20000,
                },
                key=key,
                now=self.clock(),
            )
            return doc(result)

    def train_operational(self, p):
        dataset = self.save_operational_dataset(p)
        data = dataset["rows"]
        with self.db.transaction(False) as s:
            minimum = self.optimization_config(s, p)["training_min_rows"]
        positives = sum(r["human"] for r in data)
        if len(data) < minimum or min(positives, len(data) - positives) < 10:
            return {
                "status": "insufficient_data",
                "rows": len(data),
                "required_rows": minimum,
                "dataset_id": dataset["id"],
                "reason": "Independent labeled outcomes and both answer classes are required",
            }
        job = self.jobs.enqueue(
            "ml",
            {
                "rows": data,
                "seller_id": p["seller_id"],
                "user_id": p["user_id"],
                "source_dataset_id": dataset["id"],
                "goal": dataset["goal"],
            },
            "operational-train:" + dataset["checksum"],
            tenant_id=p["tenant_id"],
        )
        return {"status": job["status"], "rows": len(data), "job_id": job["id"], "dataset_id": dataset["id"]}

    def refresh_optimization(self, p):
        with self.db.transaction() as s:
            self.ensure_experiment_cohort(s, p)
        dataset = self.save_operational_dataset(p)
        with self.db.transaction() as s:
            config = self.optimization_config(s, p)
            for number in self.inventory_evidence(s, p):
                existing = next(
                    (r for r in self.scoped(s, "number_health", p) if r.external_key == number["id"]), None
                )
                if existing:
                    update(existing, **number["health"], number_id=number["id"])
                else:
                    put(
                        s,
                        "number_health",
                        p["tenant_id"],
                        p["seller_id"],
                        {**number["health"], "number_id": number["id"]},
                        key=number["id"],
                        now=self.clock(),
                    )
                if (
                    number["health"]["samples"] >= 20
                    and number["health"].get("delivery_success_rate", 1) < 0.7
                    and number["lifecycle"] in {"healthy", "limited_real_use"}
                ):
                    record = self.required(s, "number_inventory", number["id"], p)
                    update(
                        record,
                        lifecycle="investigating",
                        inbound_retained=True,
                        health_review_reason="Repeated observed delivery failures; review required",
                    )
                    audit(
                        s,
                        p["tenant_id"],
                        p["seller_id"],
                        p["user_id"],
                        "number.delivery_hold",
                        record.id,
                        self.clock(),
                        {"samples": number["health"]["samples"]},
                    )
            campaign = self.scoped(s, "campaigns", p)[0]
            policy = self.required(s, "policy_versions", campaign.data["policy_id"], p).data
            for lead in self.scoped(s, "leads", p):
                if lead.data.get("status") != "eligible" or self.suppressed(s, p, lead.data["phone"]):
                    continue
                key = (
                    lead.id
                    + ":"
                    + str(lead.data.get("attempt_count", 0))
                    + ":"
                    + str(lead.data.get("callback_due_at"))
                )
                existing = next(
                    (r for r in self.scoped(s, "scheduling_plans", p) if r.external_key == key), None
                )
                if existing:
                    continue
                options = future_slots(
                    doc(lead),
                    dataset["rows"],
                    now=self.clock(),
                    policy=policy,
                    predictor=self.slot_predictor(s, p, lead, config),
                )
                if not options:
                    continue
                selected = options[0]
                put(
                    s,
                    "scheduling_plans",
                    p["tenant_id"],
                    p["seller_id"],
                    {
                        "lead_id": lead.id,
                        "selected": selected,
                        "alternatives": options,
                        "dataset_id": dataset["id"],
                        "created_at": self.clock().isoformat(),
                    },
                    key=key,
                    now=self.clock(),
                )
                # Fresh leads remain immediately callable; completed attempts get the learned retry slot.
                if lead.data.get("attempt_count") and not lead.data.get("callback_due_at"):
                    update(lead, next_attempt_at=selected["at"])
            emit(s, p["tenant_id"], "optimization.updated", campaign, self.clock())
        training = self.train_operational(p) if config["auto_train"] else {"status": "disabled"}
        return {"dataset_id": dataset["id"], "rows": len(dataset["rows"]), "training": training}
