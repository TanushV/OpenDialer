import hashlib
import json
from datetime import timedelta

from sqlalchemy import select

from app.db import ActiveClaim, audit, doc, emit, get, put, uid, update
from app.optimization.learning import (
    GOALS, derive_labels, drift_report, freeze_features, future_slots, training_rows, trial_report,
)
from app.optimization.numbering import capacity_forecast, number_health, timestamp
from app.service import fail

DEFAULTS = {"conversion_goal": "meeting_held", "window_days": 14, "exploration": 0.1,
            "auto_train": True, "horizon_hours": 24, "rep_minutes": 480,
            "handling_seconds": 120, "daily_capacity_per_number": 40, "training_min_rows": 100}


class OptimizationWorkflows:
    def optimization_config(self, s, p, campaign_id=None):
        campaign = self.required(s, "campaigns", campaign_id, p) if campaign_id else self.scoped(s, "campaigns", p)[0]
        return {**DEFAULTS, **campaign.data.get("optimization", {})}

    def inventory_evidence(self, s, p):
        calls = [doc(c) for c in self.scoped(s, "call_attempts", p)]
        snapshots = {r.id: r.data for r in self.scoped(s, "feature_snapshots", p)}
        attempts = []
        for call in calls:
            if not call.get("outcome") and call.get("end_reason") not in {"provider_failure", "route_failure"}:
                continue
            at, ended = timestamp(call.get("started_at")), timestamp(call.get("ended_at"))
            predicted = snapshots.get(call.get("feature_snapshot_id"), {}).get("predictions", {})
            attempts.append({**call, "duration_seconds": max(0, (ended - at).total_seconds()) if at and ended else 0,
                             "delivery_failed": call.get("end_reason") in {"provider_failure", "route_failure"},
                             "expected_human": predicted.get("p_human")})
        claims = list(s.scalars(select(ActiveClaim).where(
            ActiveClaim.tenant_id == p["tenant_id"], ActiveClaim.seller_id == p["seller_id"])))
        leases = [lease.data for lease in self.scoped(s, "number_leases", p) if lease.data.get("active")]
        callbacks = [r.data for r in self.scoped(s, "followup_tasks", p) if r.data.get("inbound")]
        inventory = []
        for number in self.scoped(s, "number_inventory", p):
            health = number_health([a for a in attempts if a.get("number_id") == number.id],
                                   pooled_attempts=attempts, now=self.clock())
            health["callbacks"] = sum(c.get("number_id") == number.id for c in callbacks)
            used = sum(c.get("number_id") == number.id and timestamp(c["started_at"]).date() == self.clock().date()
                       for c in calls)
            inventory.append({**doc(number), "health": health, "used_today": used,
                              "reserved_attempts": sum(c.number_id == number.id and not c.call_id for c in claims),
                              "active_obligations": sum(c.number_id == number.id for c in claims)
                              + sum(lease["number_id"] == number.id for lease in leases)})
        return inventory

    def operational_dataset(self, s, p, config=None):
        config = config or self.optimization_config(s, p)
        calls = [doc(c) for c in self.scoped(s, "call_attempts", p)]
        snapshots = [doc(r) for r in self.scoped(s, "feature_snapshots", p)]
        events = [doc(r) for r in self.scoped(s, "conversion_events", p)]
        costs = {}
        for record in self.scoped(s, "usage_ledger", p):
            if record.data.get("call_id") and record.data.get("cost_usd") is not None and record.data.get("provider_record_id"):
                key = record.data["call_id"]
                costs[key] = costs.get(key, 0) + float(record.data["cost_usd"])
        by_snapshot = {r["id"]: r for r in snapshots}
        for call in calls:
            snapshot = by_snapshot.get(call.get("feature_snapshot_id"))
            if snapshot and call["id"] in costs:
                snapshot["observed_cost_usd"] = costs[call["id"]]
        data = training_rows(calls, snapshots, events, now=self.clock(), goal=config["conversion_goal"],
                             window_days=config["window_days"])
        return data

    def prediction_features(self, s, p, lead, number=None, calls=None):
        endpoint = get(s, "phone_endpoints", lead.data["endpoint_id"], p["tenant_id"])
        inventory = self.inventory_evidence(s, p)
        selected = next((n for n in inventory if number and n["id"] == number.id), None)
        health = selected["health"] if selected else {"score": None, "samples": 0}
        return freeze_features({**doc(lead), "created_at": lead.data.get("imported_at", lead.created_at.isoformat()),
                                "ownership_status": endpoint.data.get("ownership_status") if endpoint else None},
                               health=health, now=self.clock(),
                               calls=calls if calls is not None else [doc(c) for c in self.scoped(s, "call_attempts", p)])

    def forecast(self, s, p, *, campaign_id=None, capacity=None):
        config = self.optimization_config(s, p, campaign_id)
        campaign = self.required(s, "campaigns", campaign_id, p) if campaign_id else self.scoped(s, "campaigns", p)[0]
        policy = self.required(s, "policy_versions", campaign.data["policy_id"], p).data
        leads = [{**doc(lead), "suppressed": self.suppressed(s, p, lead.data["phone"])}
                 for lead in self.scoped(s, "leads", p) if lead.data["campaign_id"] == campaign.id]
        return capacity_forecast(leads, self.inventory_evidence(s, p), now=self.clock(),
                                 horizon_hours=config["horizon_hours"], rep_minutes=config["rep_minutes"],
                                 handling_seconds=config["handling_seconds"],
                                 daily_capacity_per_number=capacity or config["daily_capacity_per_number"], policy=policy)

    def optimization_report(self, p):
        with self.db.transaction(False) as s:
            config = self.optimization_config(s, p)
            data = self.operational_dataset(s, p, config)
            leads = self.scoped(s, "leads", p)
            calls = self.scoped(s, "call_attempts", p)
            known_calls = {c.id for c in calls if p["role"] != "rep" or c.data["rep_id"] == p["user_id"]}
            events = [doc(r) for r in self.scoped(s, "conversion_events", p)]
            original = sum(lead.data.get("originally_eligible", True) for lead in leads)
            converted = {e["lead_id"] for e in events if e["kind"] == config["conversion_goal"] and not e.get("retracted")}
            goal_label = "qualified" if config["conversion_goal"] == "qualified_conversation" else config["conversion_goal"]
            converted |= {c.data["lead_id"] for c in calls if derive_labels(c.data.get("outcome")).get(goal_label)}
            feedback = [r.data for r in self.scoped(s, "coaching_feedback", p) if r.data["call_id"] in known_calls]
            coach_events = [r.data for r in self.scoped(s, "coach_events", p) if r.data["call_id"] in known_calls]
            latency = sorted(r["latency"]["inference_ms"] for r in coach_events if r.get("latency"))
            delivery = sorted(r["delivery_ms"] for r in feedback if r.get("delivery_ms") is not None)
            assignments = [r.data for r in self.scoped(s, "experiment_assignments", p)]
            derived_events = events + [{"lead_id": c.data["lead_id"], "kind": config["conversion_goal"],
                                        "occurred_at": c.data.get("ended_at", c.data["started_at"])}
                                       for c in calls if derive_labels(c.data.get("outcome")).get(goal_label)]
            trial = trial_report(assignments, derived_events, now=self.clock(), goal=config["conversion_goal"],
                                 window_days=config["window_days"])
            decision_rows = self.scoped(s, "policy_decisions", p)
            decisions = [doc(r) for r in decision_rows if p["role"] != "rep" or r.data.get("rep_id") == p["user_id"]]
            plans = [doc(r) for r in self.scoped(s, "scheduling_plans", p)]
            count = len(data)
            midpoint = max(0, count - max(20, count // 3))
            model_drift = drift_report(data[:midpoint], data[midpoint:])
            futures = []
            campaign = self.scoped(s, "campaigns", p)[0]
            policy = self.required(s, "policy_versions", campaign.data["policy_id"], p).data
            for lead in leads[:500]:
                if lead.data.get("status") != "eligible" or self.suppressed(s, p, lead.data["phone"]):
                    continue
                candidates = future_slots(doc(lead), data, now=self.clock(), policy=policy)
                if candidates:
                    futures.append({**candidates[0], "name": lead.data["name"]})
            durations = sum(r.get("duration_seconds", 0) for r in data)
            return {
                "settings": config, "forecast": self.forecast(s, p),
                "numbers": self.inventory_evidence(s, p),
                "metrics": {"imported_leads": len(leads), "original_eligible_leads": original,
                            "suppressed_now": sum(self.suppressed(s, p, lead.data["phone"]) for lead in leads),
                            "attempts": len(calls), "human_answers": sum(derive_labels(c.data.get("outcome"))["human"] for c in calls),
                            "unique_right_party": len({c.data["lead_id"] for c in calls if derive_labels(c.data.get("outcome"))["right_party"]}),
                            "unique_conversions": len(converted),
                            "conversion_per_original_eligible_lead": len(converted) / original if original else None,
                            "conversions_per_rep_hour": len(converted) * 3600 / durations if durations else None,
                            "observed_cost_usd": sum(r.get("observed_cost_usd") or 0 for r in data),
                            "outcome_goal": config["conversion_goal"]},
                "dataset": {"rows": count, "pending_rows": sum(r["conversion"] is None for r in data),
                            "matured_rows": sum(r["window_closed"] for r in data),
                            "positive_conversions": sum(r["conversion"] == 1 for r in data),
                            "missing_snapshot_calls": sum(bool(c.data.get("outcome")) and not c.data.get("feature_snapshot_id") for c in calls),
                            "snapshots": [{"id": r.id, "rows": len(r.data.get("rows", [])), "as_of": r.data["as_of"],
                                           "goal": r.data["goal"]} for r in self.scoped(s, "learning_datasets", p)[-10:]]},
                "decisions": decisions[-30:], "future_slots": futures[:100], "plans": plans[-100:],
                "drift": model_drift, "trial": trial,
                "coaching": {"reviewed_cards": len(feedback),
                             **{key + "_fraction": sum(bool(r.get(key)) for r in feedback) / len(feedback) if feedback else None
                                for key in ("useful", "grounded", "timely", "distracting")},
                             "p95_inference_ms": latency[max(0, int(len(latency) * 0.95 + 0.999) - 1)] if latency else None,
                             "p95_delivery_ms": delivery[max(0, int(len(delivery) * 0.95 + 0.999) - 1)] if delivery else None,
                             "evidence_scope": "recorded provider and reviewer measurements; live STT target remains unverified"},
                "simulation": self.settings.simulation, "as_of": self.clock().isoformat(),
            }

    def save_operational_dataset(self, p):
        with self.db.transaction() as s:
            config = self.optimization_config(s, p)
            data = self.operational_dataset(s, p, config)
            # Bound training memory, retain the complete operational ledger as source of truth.
            data = data[-20000:]
            key = hashlib.sha256(json.dumps([config["conversion_goal"], config["window_days"], data], sort_keys=True).encode()).hexdigest()
            previous = next((r for r in self.scoped(s, "learning_datasets", p) if r.external_key == key), None)
            if previous:
                return doc(previous)
            result = put(s, "learning_datasets", p["tenant_id"], p["seller_id"],
                         {"goal": config["conversion_goal"], "window_days": config["window_days"],
                          "as_of": self.clock().isoformat(), "rows": data, "checksum": key,
                          "source": "immutable_operational_feature_snapshots", "max_training_rows": 20000},
                         key=key, now=self.clock())
            return doc(result)

    def train_operational(self, p):
        dataset = self.save_operational_dataset(p)
        data = dataset["rows"]
        with self.db.transaction(False) as s:
            minimum = self.optimization_config(s, p)["training_min_rows"]
        positives = sum(r["human"] for r in data)
        if len(data) < minimum or min(positives, len(data) - positives) < 10:
            return {"status": "insufficient_data", "rows": len(data), "required_rows": minimum,
                    "dataset_id": dataset["id"], "reason": "Independent labeled outcomes and both answer classes are required"}
        job = self.jobs.enqueue("ml", {"rows": data, "seller_id": p["seller_id"], "user_id": p["user_id"],
                                      "source_dataset_id": dataset["id"], "goal": dataset["goal"]},
                                "operational-train:" + dataset["checksum"], tenant_id=p["tenant_id"])
        return {"status": job["status"], "rows": len(data), "job_id": job["id"], "dataset_id": dataset["id"]}

    def refresh_optimization(self, p):
        dataset = self.save_operational_dataset(p)
        with self.db.transaction() as s:
            config = self.optimization_config(s, p)
            for number in self.inventory_evidence(s, p):
                existing = next((r for r in self.scoped(s, "number_health", p) if r.external_key == number["id"]), None)
                if existing:
                    update(existing, **number["health"], number_id=number["id"])
                else:
                    put(s, "number_health", p["tenant_id"], p["seller_id"],
                        {**number["health"], "number_id": number["id"]}, key=number["id"], now=self.clock())
            campaign = self.scoped(s, "campaigns", p)[0]
            policy = self.required(s, "policy_versions", campaign.data["policy_id"], p).data
            for lead in self.scoped(s, "leads", p):
                if lead.data.get("status") != "eligible" or self.suppressed(s, p, lead.data["phone"]):
                    continue
                key = lead.id + ":" + str(lead.data.get("attempt_count", 0)) + ":" + str(lead.data.get("callback_due_at"))
                existing = next((r for r in self.scoped(s, "scheduling_plans", p) if r.external_key == key), None)
                if existing:
                    continue
                options = future_slots(doc(lead), dataset["rows"], now=self.clock(), policy=policy)
                if not options:
                    continue
                selected = options[0]
                put(s, "scheduling_plans", p["tenant_id"], p["seller_id"],
                    {"lead_id": lead.id, "selected": selected, "alternatives": options,
                     "dataset_id": dataset["id"], "created_at": self.clock().isoformat()},
                    key=key, now=self.clock())
                # Fresh leads remain immediately callable; completed attempts get the learned retry slot.
                if lead.data.get("attempt_count") and not lead.data.get("callback_due_at"):
                    update(lead, next_attempt_at=selected["at"])
            emit(s, p["tenant_id"], "optimization.updated", campaign, self.clock())
        training = self.train_operational(p) if config["auto_train"] else {"status": "disabled"}
        return {"dataset_id": dataset["id"], "rows": len(dataset["rows"]), "training": training}
