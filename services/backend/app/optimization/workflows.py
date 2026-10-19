import hashlib
import hmac
import json
import math
from datetime import timedelta

from sqlalchemy import func, or_, select

from app.db import MODELS, ActiveClaim, audit, doc, emit, get, put, update
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
from app.scheduling.models import EstimatePolicy, prediction_key, profile_current, saved_prediction

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
    "staffed_windows": [],
    "additional_ready_after_hours": 0,
    "staffing_timezone": "UTC",
    "staffing_start_hour": 9,
    "staffing_end_hour": 20,
    "staffing_reps": 1,
    "staffing_weekdays": list(range(7)),
    "account_channel_limit": None,
    "minimum_expansion_age_hours": 24,
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

    def model_source_current(self, s, p, model, config):
        """Check the evaluated definition once per transaction, without ML imports."""
        value = model.data
        if value.get("goal", config["conversion_goal"]) != config["conversion_goal"]:
            return False
        if value.get("feature_schema_version", 1) != 2:
            return value.get("window_days", config["window_days"]) == config["window_days"]
        baseline = {k: config[k] for k in ("estimate_policy", "handling_seconds")}
        if value.get("baseline") != baseline or value.get("window_days") != config["window_days"]:
            return False
        source = get(s, "learning_datasets", value.get("source_dataset_id"), p["tenant_id"])
        return bool(
            source
            and source.seller_id == p["seller_id"]
            and source.data.get("baseline") == baseline
            and source.data.get("goal") == config["conversion_goal"]
            and source.data.get("window_days") == config["window_days"]
        )

    def serving_model(self, s, p, config, lead_id=None):
        from app.optimization.learning import stable_control

        if lead_id and stable_control(lead_id):
            return None
        # This pointer cache lives only for this read transaction. A rollback,
        # promotion or goal edit is visible to the next decision immediately.
        cache = s.info.setdefault("serving_context", {})
        scope = (
            p["tenant_id"],
            p["seller_id"],
            config["conversion_goal"],
            config["window_days"],
            config["handling_seconds"],
            EstimatePolicy.model_validate(config["estimate_policy"]).version,
        )
        if scope not in cache:
            model = MODELS["model_versions"]
            prefetched = s.info.get("row_cache", {}).get(("model_versions", p["tenant_id"], p["seller_id"]))
            models = (
                prefetched
                if prefetched is not None
                else list(
                    s.scalars(
                        select(model).where(
                            model.tenant_id == p["tenant_id"],
                            model.seller_id == p["seller_id"],
                            (model.ctx_key == "active")
                            | ((model.ctx_key == "trial") & (model.ctx_expires_at > self.clock())),
                            or_(model.ctx_status == config["conversion_goal"], model.ctx_status.is_(None)),
                        )
                    )
                )
            )
            models = [m for m in models if self.model_source_current(s, p, m, config)]
            cache[scope] = {"models": models}
            if any(m.data.get("trial_active") for m in models):
                campaign = self.scoped(s, "campaigns", p)[0]
                cache[scope]["epoch"] = (
                    campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
                )
        context = cache[scope]
        models = [
            m
            for m in context["models"]
            if m.data.get("goal", config["conversion_goal"]) == config["conversion_goal"]
        ]
        trials = [
            m
            for m in models
            if m.data.get("trial_active")
            and m.data.get("trial_epoch") == context.get("epoch")
            and timestamp(m.data.get("trial_ends_at"))
            and timestamp(m.data["trial_ends_at"]) > self.clock()
        ]
        if trials and lead_id is None:
            return trials[0]
        if trials:
            if "assignments" not in context:
                prefetched = s.info.get("row_cache", {}).get(
                    ("experiment_assignments", p["tenant_id"], p["seller_id"])
                )
                context["assignments"] = {r.external_key: r for r in prefetched or []}
                context["assignments_prefetched"] = prefetched is not None
            key = context["epoch"] + ":" + lead_id
            assignments = context["assignments"]
            if key not in assignments and not context["assignments_prefetched"]:
                assignments[key] = self._by_key(s, p, "experiment_assignments", key)
            enrollment = assignments.get(key)
            if (
                enrollment
                and enrollment.data.get("lead_id") == lead_id
                and enrollment.data.get("epoch") == context["epoch"]
                and enrollment.data.get("family", "optimization") == "optimization"
                and enrollment.data.get("arm") == "candidate"
            ):
                trial = next((m for m in trials if m.id == enrollment.data.get("trial_model_id")), None)
                if trial:
                    return trial
        return next((m for m in models if m.data.get("active")), None)

    def slot_predictor(self, s, p, lead, config):
        model = self.serving_model(s, p, config, lead.id)
        if model is None:
            return None
        features = self.prediction_features(s, p, lead, calls=[], fast=True)
        artifact, cached = dict(model.data), {}
        directory = self.settings.data_dir / "models"

        def predict(hour, weekday):
            if not cached:
                from app.ml.training import predict_funnel_batch

                keys = [(day, slot) for day in range(7) for slot in range(24)]
                values = predict_funnel_batch(
                    artifact,
                    [{**features, "local_hour": slot, "local_weekday": day} for day, slot in keys],
                    directory,
                )
                cached.update(zip(keys, values, strict=True))
            return cached[weekday, hour]

        return predict

    def reconcile_goal_status(self, s, p, lead):
        from app.optimization.goals import goal_state

        config = self.optimization_config(s, p, lead.data["campaign_id"])
        change = goal_state(
            lead.data,
            config["conversion_goal"],
            [doc(c) for c in self.lead_records(s, p, "call_attempts", lead.id)],
            [doc(e) for e in self.lead_records(s, p, "conversion_events", lead.id)],
            now=self.clock(),
            suppressed=self.suppressed(s, p, lead.data["phone"]),
        )
        if any(lead.data.get(key) != value for key, value in change.items()):
            update(lead, **change)

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
        from app.optimization.api import HealthPolicy

        campaign = (
            self.required(s, "campaigns", campaign_id, p)
            if campaign_id
            else self.scoped(s, "campaigns", p)[0]
        )
        config = {**DEFAULTS, **campaign.data.get("optimization", {})}
        config["health_policy"] = HealthPolicy.model_validate(config.get("health_policy", {})).model_dump()
        config["estimate_policy"] = EstimatePolicy.model_validate(
            config.get("estimate_policy", {})
        ).model_dump(mode="json")
        return config

    def inventory_evidence(self, s, p, fast=False):
        if fast:
            return self.inventory_capacity(s, p)
        config = self.optimization_config(s, p)
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
            snapshot = snapshots.get(call.get("feature_snapshot_id"), {})
            predicted = snapshot.get("predictions", {})
            attempts.append(
                {
                    **snapshot.get("features", {}),
                    **call,
                    "feature_schema_version": snapshot.get(
                        "feature_schema_version", snapshot.get("features", {}).get("feature_schema_version")
                    ),
                    "prediction_at": snapshot.get("as_of"),
                    "duration_seconds": max(0, (ended - at).total_seconds()) if at and ended else 0,
                    "connected_seconds": call.get("connected_seconds"),
                    "delivery_failed": call.get("end_reason") in {"provider_failure", "route_failure"},
                    "expected_human": predicted.get("p_human"),
                }
            )
        claims = list(
            s.scalars(
                select(ActiveClaim).where(
                    ActiveClaim.tenant_id == p["tenant_id"],
                    ActiveClaim.seller_id == p["seller_id"],
                    or_(ActiveClaim.call_id.is_not(None), ActiveClaim.expires_at > self.clock()),
                )
            )
        )
        leases = [lease.data for lease in self.scoped(s, "number_leases", p) if lease.data.get("active")]
        callbacks = [r.data for r in self.scoped(s, "followup_tasks", p) if r.data.get("inbound")]
        inventory = []
        for number in self.scoped(s, "number_inventory", p):
            readiness = self.number_readiness_in(s, p, number)
            health = number_health(
                [a for a in attempts if a.get("number_id") == number.id],
                pooled_attempts=attempts,
                now=self.clock(),
                external=number.data.get("external_reputation"),
                policy=config["health_policy"],
                configuration_errors=readiness["configuration_errors"],
            )
            health["callbacks"] = sum(c.get("number_id") == number.id for c in callbacks)
            used = sum(
                c.get("number_id") == number.id and timestamp(c["started_at"]).date() == self.clock().date()
                for c in calls
            )
            inventory.append(
                {
                    **doc(number),
                    **readiness,
                    "health": health,
                    "used_today": used,
                    "reserved_attempts": sum(c.number_id == number.id and not c.call_id for c in claims),
                    "active_obligations": sum(c.number_id == number.id for c in claims)
                    + sum(lease["number_id"] == number.id for lease in leases),
                }
            )
        return inventory

    def inventory_capacity(self, s, p):
        """Current capacity is authoritative; health reads the last derived observation."""
        claims = list(
            s.scalars(
                select(ActiveClaim).where(
                    ActiveClaim.tenant_id == p["tenant_id"],
                    ActiveClaim.seller_id == p["seller_id"],
                    or_(ActiveClaim.call_id.is_not(None), ActiveClaim.expires_at > self.clock()),
                )
            )
        )
        s.info["active_claims"] = claims
        attempt = MODELS["call_attempts"]
        today = self.clock().replace(hour=0, minute=0, second=0, microsecond=0)
        used = dict(
            s.execute(
                select(attempt.op_number_id, func.count())
                .where(
                    attempt.tenant_id == p["tenant_id"],
                    attempt.seller_id == p["seller_id"],
                    attempt.op_at >= today,
                    attempt.op_at <= self.clock(),
                )
                .group_by(attempt.op_number_id)
            ).all()
        )
        health = {r.data["number_id"]: dict(r.data) for r in self.scoped(s, "number_health", p)}
        config = self.optimization_config(s, p)
        leases = [r.data for r in self.scoped(s, "number_leases", p) if r.data.get("active")]
        return [
            {
                **doc(n),
                **self.number_readiness_in(s, p, n),
                "health": number_health(
                    health.get(n.id) or [],
                    pooled_attempts=[],
                    now=self.clock(),
                    external=n.data.get("external_reputation"),
                    policy=config["health_policy"],
                    configuration_errors=self.number_readiness_in(s, p, n)["configuration_errors"],
                ),
                "used_today": used.get(n.id, 0),
                "reserved_attempts": sum(c.number_id == n.id and not c.call_id for c in claims),
                "active_obligations": sum(c.number_id == n.id for c in claims)
                + sum(x["number_id"] == n.id for x in leases),
            }
            for n in self.scoped(s, "number_inventory", p)
        ]

    def selection_profile(self, s, p, data, config):
        from app.optimization.learning import funnel_predictions, learn_distribution, retry_statistics

        campaign = self.scoped(s, "campaigns", p)[0]
        data = [r for r in data if r.get("campaign_id") == campaign.id]
        policy = self.required(s, "policy_versions", campaign.data["policy_id"], p)
        leads = [doc(r) for r in self.scoped(s, "leads", p) if r.data["campaign_id"] == campaign.id]
        cells = sorted({(r.get("segment", "unknown"), r.get("attempt_count", 0)) for r in leads})
        segments = sorted({segment for segment, _ in cells})
        estimate_options = {
            "handling_seconds": config["handling_seconds"],
            "estimate_policy": config["estimate_policy"],
        }
        actions = [r.id for r in self.scoped(s, "treatment_variants", p) if r.data.get("approved")] or [
            "approved-default"
        ]
        return {
            "as_of": self.clock().isoformat(),
            "goal": config["conversion_goal"],
            "samples": len(data),
            "window_days": config["window_days"],
            "estimate_policy_version": EstimatePolicy.model_validate(config["estimate_policy"]).version,
            "work_assumption_seconds": config["handling_seconds"],
            "retry_observations": retry_statistics(
                data,
                leads,
                now=self.clock(),
                max_attempts=policy.data.get("max_attempts", 4),
            ),
            "pooled": funnel_predictions(data, {}, **estimate_options),
            "predictions": {
                prediction_key(features): funnel_predictions(data, features, **estimate_options)
                for segment, attempt in cells
                for day in (0, 5)
                for hour in range(24)
                for features in [
                    {"segment": segment, "local_hour": hour, "local_weekday": day, "attempt_count": attempt}
                ]
            },
            "treatments": {
                str(segment): learn_distribution(
                    actions, data, segment=segment, exploration=config["exploration"]
                )
                for segment in segments
            },
        }

    def operational_dataset(self, s, p, config=None):
        config = config or self.optimization_config(s, p)
        calls = [doc(c) for c in self.scoped(s, "call_attempts", p)]
        snapshots = [doc(r) for r in self.scoped(s, "feature_snapshots", p)]
        events = [doc(r) for r in self.scoped(s, "conversion_events", p)]
        from app.coaching.context import lineage_key
        from app.optimization.conversations import active_observations
        from app.postcall.analysis import learning_observations

        notes = {r.data["call_id"]: r for r in self.scoped(s, "notes", p)}
        memories = {
            r.data["call_id"]: r.data
            for r in self.scoped(s, "conversation_states", p)
            if r.data.get("schema_version") == 2
        }
        # This explicit historical dataset is replaced by projections in E09.
        # Fetch its metadata once, never one query for each previous call.
        lineage, work = {}, {}
        model = MODELS["outcomes"]
        for row in s.scalars(
            select(model).where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.ctx_status.in_(("conversation_lineage", "attempt_work")),
            )
        ):
            if row.data["kind"] == "attempt_work":
                work[row.data["call_id"]] = row.data
            else:
                lineage.setdefault(row.data["call_id"], {})[
                    lineage_key(row.data["id"], row.data["version"])
                ] = row.data
        lead_campaigns = {r.id: r.data["campaign_id"] for r in self.scoped(s, "leads", p)}
        for call in calls:
            call["campaign_id"] = lead_campaigns.get(call["lead_id"])
            if call["id"] in work:
                call.update(
                    {key: work[call["id"]].get(key) for key in ("rep_work_intervals", "rep_work_incomplete")}
                )
            note = notes.get(call["id"])
            memory = memories.get(call["id"])
            observed = (
                learning_observations(memory["observations"], lineage.get(call["id"]))
                if memory
                else (note.data.get("observations", []) if note else [])
            )
            call["observations"] = active_observations(observed, self.clock())
        from decimal import Decimal

        costs, incomplete, verified = {}, set(), set()
        for record in self.scoped(s, "usage_ledger", p):
            value = record.data
            key = value.get("call_id") or value.get("object_id")
            if not key:
                continue
            if value.get("status") == "released":
                continue
            if value.get("status") != "settled" or value.get("cost_usd") is None:
                incomplete.add(key)
                continue
            if value.get("kind") == "call_authorization" and value.get("billing_verified"):
                verified.add(key)
            costs[key] = (
                costs.get(key, Decimal(0))
                + Decimal(str(value["cost_usd"]))
                - Decimal(str(value.get("credited_usd", 0)))
            )
        for call in calls:
            complete = call["id"] in verified and call["id"] not in incomplete
            call["cost_status"] = "settled" if complete else "uncertain"
            call["observed_cost_usd"] = float(costs.get(call["id"], 0)) if complete else None
        data = training_rows(
            calls,
            snapshots,
            events,
            now=self.clock(),
            goal=config["conversion_goal"],
            window_days=config["window_days"],
        )
        return data

    def prediction_features(self, s, p, lead, number=None, calls=None, *, fast=False):
        endpoint = get(s, "phone_endpoints", lead.data["endpoint_id"], p["tenant_id"])
        inventory = self.inventory_evidence(s, p, fast=fast) if number else []
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

    def forecast_input(self, s, p, *, campaign_id=None, capacity=None):
        config = self.optimization_config(s, p, campaign_id)
        campaign = (
            self.required(s, "campaigns", campaign_id, p)
            if campaign_id
            else self.scoped(s, "campaigns", p)[0]
        )
        policy_row = self.required(s, "policy_versions", campaign.data["policy_id"], p)
        profile = campaign.data.get("selection_profile", {})
        if not profile_current(profile, config):
            profile = {}
        policy = {
            **policy_row.data,
            **{k: v for k, v in config.items() if k.startswith("staffing_")},
            "handling_seconds": config["handling_seconds"],
            "estimate_policy": config["estimate_policy"],
            "version": f"{policy_row.id}:{policy_row.version}",
            "goal_version": campaign.data.get("optimization_epoch", config["conversion_goal"]),
            "estimate_version": profile.get("version") or profile.get("as_of") or "pooled-v1",
        }
        if config.get("account_channel_limit") is not None:
            policy["account_channel_limit"] = config["account_channel_limit"]
        claims = list(
            s.scalars(
                select(ActiveClaim).where(
                    ActiveClaim.tenant_id == p["tenant_id"],
                    ActiveClaim.seller_id == p["seller_id"],
                    or_(ActiveClaim.call_id.is_not(None), ActiveClaim.expires_at > self.clock()),
                )
            )
        )
        claimed_leads = {claim.lead_id for claim in claims}
        # An uncertain live leg cannot be declared free just because its lease expired.
        end = self.clock() + timedelta(hours=config["horizon_hours"])
        busy = {
            claim.rep_id: (end if claim.call_id else timestamp(claim.expires_at)).isoformat()
            for claim in claims
        }
        windows = config["staffed_windows"]
        roster = {rep for window in windows for rep in window.get("rep_ids", [])}
        users = MODELS["users"]
        available = {
            u.id
            for u in s.scalars(
                select(users).where(
                    users.tenant_id == p["tenant_id"],
                    users.seller_id == p["seller_id"],
                    users.id.in_(roster),
                )
            )
            if not u.data.get("disabled") and u.data.get("role") in {"owner", "admin", "rep"}
        }
        windows = [
            {**w, "rep_ids": [rep for rep in w["rep_ids"] if rep in available]} if "rep_ids" in w else w
            for w in windows
        ]
        leads, cursor = [], None
        model = MODELS["leads"]
        while True:
            page = list(
                s.scalars(
                    select(model)
                    .where(
                        model.tenant_id == p["tenant_id"],
                        model.seller_id == p["seller_id"],
                        model.op_campaign_id == campaign.id,
                        *([model.id > cursor] if cursor else []),
                    )
                    .order_by(model.id)
                    .limit(500)
                )
            )
            if not page:
                break
            ids = [row.id for row in page]
            keys = [
                hmac.new(self.db.identity_key, row.data["phone"].encode(), hashlib.sha256).hexdigest()
                for row in page
            ]
            bindings, denied = {}, set()
            for kind, identities in (("lead_number_bindings", ids), ("suppressions", keys)):
                related = MODELS[kind]
                for row in s.scalars(
                    select(related).where(
                        related.tenant_id == p["tenant_id"],
                        related.seller_id == p["seller_id"],
                        related.external_key.in_(identities),
                    )
                ):
                    if kind == "lead_number_bindings":
                        bindings[row.data["lead_id"]] = row.data["number_id"]
                    elif row.data.get("active", True):
                        denied.add(row.data["phone"])
            leads.extend(
                {
                    **doc(row),
                    "suppressed": row.data["phone"] in denied,
                    "bound_number_id": bindings.get(row.id),
                    "active_obligation": row.id in claimed_leads,
                }
                for row in page
            )
            cursor = page[-1].id
            if len(page) < 500:
                break
        list_staffing_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "list": [(row["id"], row.get("endpoint_id"), row.get("region")) for row in leads],
                    "staff": {
                        k: v
                        for k, v in config.items()
                        if k.startswith("staff") or k in {"rep_minutes", "horizon_hours"}
                    },
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        pooled = saved_prediction(profile, {}, config)
        estimate_cache = {}
        regions = {"general", *(lead.get("region") or "general" for lead in leads)}
        for lead in leads:
            cell = (str(lead.get("segment", "unknown")), lead.get("attempt_count", 0))
            if cell not in estimate_cache:
                estimate_cache[cell] = {
                    f"{day}:{hour}:{region}": math.prod(
                        prediction[k]
                        for k in ("p_human", "p_right_party_given_human", "p_conversion_given_right_party")
                    )
                    for day in range(7)
                    for hour in range(24)
                    for prediction in [
                        saved_prediction(
                            profile,
                            {
                                "segment": cell[0],
                                "attempt_count": cell[1],
                                "local_hour": hour,
                                "local_weekday": day,
                            },
                            config,
                        )
                    ]
                    for region in regions
                }
            lead.update(
                predicted_work_seconds=pooled["expected_rep_seconds"],
                outcome_estimates=pooled,
                slot_estimates=estimate_cache[cell],
            )
        inventory = self.inventory_evidence(s, p, fast=True)
        present = {n["phone"] for n in inventory}
        order_model = MODELS["command_outbox"]
        orders = s.scalars(
            select(order_model).where(
                order_model.tenant_id == p["tenant_id"],
                order_model.seller_id == p["seller_id"],
                order_model.external_key.like("purchase:%"),
            )
        )
        for order in orders:
            if order.data.get("kind") != "purchase" or order.data.get("status") in {"failed", "cancelled"}:
                continue
            failed = set(order.data.get("failed_numbers", []))
            for phone in order.data.get("numbers", []):
                if phone in present or phone in failed:
                    continue
                inventory.append(
                    {
                        "id": order.id + ":" + phone,
                        "phone": phone,
                        "region": phone[2:5],
                        "lifecycle": "purchased",
                        "daily_limit": 0,
                        "order_status": order.data.get("status"),
                    }
                )
                present.add(phone)
        return {
            "leads": leads,
            "inventory": inventory,
            "now": self.clock(),
            "horizon_hours": config["horizon_hours"],
            "rep_minutes": config["rep_minutes"],
            "handling_seconds": config["handling_seconds"],
            "daily_capacity_per_number": capacity or config["daily_capacity_per_number"],
            "policy": policy,
            "staffed_windows": windows,
            "additional_ready_after_hours": config["additional_ready_after_hours"],
            "rep_busy_until": busy,
            "retry_observations": profile.get("retry_observations", []),
        }, {
            "unavailable_staff_count": len(roster - available),
            "occupied_rep_count": len(busy),
            "list_staffing_fingerprint": list_staffing_fingerprint,
            "minimum_expansion_age_hours": config["minimum_expansion_age_hours"],
        }

    def forecast(self, s, p, *, campaign_id=None, capacity=None):
        inputs, metadata = self.forecast_input(s, p, campaign_id=campaign_id, capacity=capacity)
        return {**capacity_forecast(**inputs), **metadata}

    def build_forecast(self, p, campaign_id, capacity=None):
        """Snapshot/read, pure calculation, then a short source-checked publication."""
        from app.service import fail

        key = f"planning:{campaign_id}:{capacity or 'configured'}"
        with self.db.transaction(False) as s:
            current, _, _, _ = self.report_scope(s, p)
            if current["role"] != "admin":
                fail(403, "Administrator access is required to plan inventory")
            inputs, metadata = self.forecast_input(s, current, campaign_id=campaign_id, capacity=capacity)
            generation = self.report_revision(s, current)
        result = {**capacity_forecast(**inputs), **metadata}
        with self.db.transaction() as s:
            user, _, _, _ = self.report_scope(s, p)
            if user["role"] != "admin" or self.report_revision(s, user, lock=True) != generation:
                return {"status": "superseded"}
            prior = self._by_key(s, p, "report_projections", key)
            previous = prior.data.get("report", {}) if prior else {}
            old_review = previous.get("expansion_review", {})
            shortfall = result.get("number_shortfall")
            first = (
                timestamp(old_review.get("first_shortfall_at"))
                if old_review.get("status") in {"observing", "eligible"}
                else None
            ) or inputs["now"]
            changed = bool(
                previous and previous.get("list_staffing_fingerprint") != result["list_staffing_fingerprint"]
            )
            matured = (inputs["now"] - first).total_seconds() >= 3600 * metadata[
                "minimum_expansion_age_hours"
            ]
            result["expansion_review"] = {
                "status": "unknown"
                if shortfall is None
                else "sufficient"
                if shortfall == 0
                else "eligible"
                if changed or matured
                else "observing",
                "reason": "incomplete_plan"
                if shortfall is None
                else "existing_capacity_sufficient"
                if shortfall == 0
                else "list_or_staffing_changed"
                if changed
                else "persistent_shortfall"
                if matured
                else "awaiting_second_observation",
                "first_shortfall_at": first.isoformat() if shortfall else None,
                "observed_at": inputs["now"].isoformat(),
                "next_review_at": (
                    first + timedelta(hours=metadata["minimum_expansion_age_hours"])
                ).isoformat()
                if shortfall
                else None,
                "purchase_allowed": False,
            }
            value = {
                "report": result,
                "source_revision": generation,
                "as_of": result["as_of"],
                "expires_at": (inputs["now"] + timedelta(seconds=60)).isoformat(),
                "campaign_id": campaign_id,
            }
            row = (
                update(prior, **value)
                if prior
                else put(
                    s, "report_projections", p["tenant_id"], p["seller_id"], value, key=key, now=self.clock()
                )
            )
            emit(s, p["tenant_id"], "planning.changed", row, self.clock())
        return {
            "status": "ready",
            "source_revision": generation,
            "input_fingerprint": result["input_fingerprint"],
        }

    def optimization_report(self, p, epoch=None):
        from app.optimization.projections import read_coaching_metrics, read_dataset_metrics

        with self.db.transaction(False) as s:
            s.info["row_cache"] = {}
            config = self.optimization_config(s, p)
            data = self.operational_dataset(s, p, config)
            leads = self.scoped(s, "leads", p)
            calls = self.scoped(s, "call_attempts", p)
            submitted_by_lead = {}
            for call in calls:
                if call.data.get("provider_id") or call.data.get("submitted_at"):
                    submitted_by_lead.setdefault(call.data["lead_id"], []).append(call)
            known_calls = {c.id for c in calls if p["role"] != "rep" or c.data["rep_id"] == p["user_id"]}
            events = [doc(r) for r in self.scoped(s, "conversion_events", p)]
            feedback = [
                r.data for r in self.scoped(s, "coaching_feedback", p) if r.data["call_id"] in known_calls
            ]
            coach_events = [
                doc(r) for r in self.scoped(s, "coach_events", p) if r.data["call_id"] in known_calls
            ]
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
                    for c in submitted_by_lead.get(record.data["lead_id"], [])
                    if timestamp(c.data["started_at"]) >= assigned
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
                if derive_labels(c.data).get(
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
            plans = sorted(
                self.scoped(s, "scheduling_plans", p),
                key=lambda r: (
                    r.external_key == "current:" + r.data["lead_id"],
                    r.created_at,
                    r.id,
                ),
            )
            now = self.clock()
            model_drift = {
                **drift_report(
                    [
                        r
                        for r in data
                        if now - timedelta(days=28) <= timestamp(r["at"]) < now - timedelta(days=7)
                    ],
                    [r for r in data if now - timedelta(days=7) <= timestamp(r["at"]) <= now],
                ),
                "as_of": now.isoformat(),
                "windows": "Latest 7 days versus preceding 21 days",
            }
            latest_plans = {}
            for record in plans:
                plan = record.data
                latest_plans[plan["lead_id"]] = plan
            futures = [
                {**latest_plans[lead.id]["selected"], "name": lead.data["name"]}
                for lead in leads
                if lead.id in latest_plans
                and latest_plans[lead.id].get("selected")
                and lead.data.get("status") == "eligible"
                and not lead.data.get("outreach_hold")
                and not lead.data.get("goal_completed")
                and latest_plans[lead.id].get("source_lead_version", lead.version) == lead.version
                and (
                    timestamp(latest_plans[lead.id].get("expires_at")) or self.clock() + timedelta(seconds=1)
                )
                > self.clock()
            ]
            measured = self.metrics(p)
            return {
                "settings": config,
                "comparisons": self.comparison_reports(
                    s, p, campaign, calls, derived_events, feedback, coach_events
                ),
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
                    **measured,
                    "imported_leads": measured["original_cohort"],
                    "original_eligible_leads": measured["original_eligible_denominator"],
                    "suppressed_now": measured["suppressed"],
                    "human_answers": measured["human_pickups"],
                    "unique_right_party": measured["right_party_unique"],
                    "observed_cost_usd": sum(r.get("observed_cost_usd") or 0 for r in data),
                },
                "dataset": read_dataset_metrics(self, p),
                "decisions": decisions[-30:],
                "future_slots": futures[:100],
                "plans": [doc(row) for row in plans[-100:]],
                "drift": model_drift,
                "trial": trial,
                "coaching": read_coaching_metrics(self, p),
                "simulation": self.settings.simulation,
                "as_of": self.clock().isoformat(),
            }

    def save_operational_dataset(self, p):
        with self.db.transaction() as s:
            config = self.optimization_config(s, p)
            data = self.operational_dataset(s, p, config)
            campaign = self.scoped(s, "campaigns", p)[0]
            revision = campaign.data.get("conversation_revision", 0)
            baseline = {k: config[k] for k in ("estimate_policy", "handling_seconds")}
            # Bound training memory, retain the complete operational ledger as source of truth.
            data = data[-20000:]
            key = hashlib.sha256(
                json.dumps(
                    [config["conversion_goal"], config["window_days"], data, revision, baseline],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            previous = self._by_key(s, p, "learning_datasets", key)
            result = previous or put(
                s,
                "learning_datasets",
                p["tenant_id"],
                p["seller_id"],
                {
                    "goal": config["conversion_goal"],
                    "conversation_revision": revision,
                    "window_days": config["window_days"],
                    "baseline": baseline,
                    "as_of": self.clock().isoformat(),
                    "rows": data,
                    "checksum": key,
                    "source": "immutable_operational_feature_snapshots",
                    "max_training_rows": 20000,
                },
                key=key,
                now=self.clock(),
            )
            summary = {
                "id": result.id,
                "rows": len(result.data.get("rows", [])),
                "as_of": result.data["as_of"],
                "goal": result.data["goal"],
            }
            manifest = campaign.data.get("dataset_summaries", [])
            revised = sorted(
                [r for r in manifest if r["id"] != result.id] + [summary],
                key=lambda r: (r["as_of"], r["id"]),
            )[-10:]
            if manifest != revised:
                update(campaign, dataset_summaries=revised)
            return doc(result)

    def train_operational(self, p):
        dataset = self.save_operational_dataset(p)
        data = dataset["rows"]
        with self.db.transaction(False) as s:
            minimum = self.optimization_config(s, p)["training_min_rows"]
        known = [r["human"] for r in data if r.get("human") in (0, 1)]
        positives = sum(known)
        if len(known) < minimum or min(positives, len(known) - positives) < 10:
            return {
                "status": "insufficient_data",
                "rows": len(data),
                "classified_rows": len(known),
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
                "baseline": dataset["baseline"],
                "window_days": dataset["window_days"],
            },
            "operational-train:" + dataset["checksum"],
            tenant_id=p["tenant_id"],
        )
        return {"status": job["status"], "rows": len(data), "job_id": job["id"], "dataset_id": dataset["id"]}

    def refresh_schedule_plans(self, p, campaign_id, dataset_id=None):
        """Publish the shared whole-list schedule in source-fenced pages of 100."""
        from zoneinfo import ZoneInfo

        from app.service import fail

        with self.db.transaction(False) as s:
            user, _, _, _ = self.report_scope(s, p)
            if user["role"] != "admin":
                fail(403, "Administrator access required to refresh retry plans")
            inputs, _ = self.forecast_input(s, p, campaign_id=campaign_id)
            generation = self.report_revision(s, p)
            config = self.optimization_config(s, p, campaign_id)
            campaign = self.required(s, "campaigns", campaign_id, p)
            profile = campaign.data.get("selection_profile", {})
            if not profile_current(profile, config):
                profile = {}
            estimates = {**profile.get("predictions", {}), "pooled": saved_prediction(profile, {}, config)}
        forecast = capacity_forecast(**inputs, schedule_row_limit=None)
        selected = {row["lead_id"]: row for row in forecast["existing_schedule"]}
        saved, superseded, unchanged = 0, 0, 0
        common = {k: v for k, v in inputs.items() if k not in {"now", "leads"}}
        common["time_bucket"] = int(inputs["now"].timestamp()) // 600
        common_hash = hashlib.sha256(json.dumps(common, sort_keys=True, default=str).encode()).hexdigest()

        def fingerprint(lead):
            return hashlib.sha256(
                json.dumps([common_hash, lead], sort_keys=True, default=str).encode()
            ).hexdigest()

        for offset in range(0, len(inputs["leads"]), 100):
            page = inputs["leads"][offset : offset + 100]
            ids = [row["id"] for row in page]
            with self.db.transaction(False) as s:
                model = MODELS["leads"]
                records = list(
                    s.scalars(
                        select(model).where(
                            model.tenant_id == p["tenant_id"],
                            model.seller_id == p["seller_id"],
                            model.id.in_(ids),
                        )
                    )
                )
                self._queue_context(s, p, campaign_id, records)
                predictors = {
                    row.id: self.slot_predictor(s, p, row, config) for row in records if row.id in selected
                }
                plans = MODELS["scheduling_plans"]
                prior = {
                    row.data["lead_id"]: dict(row.data)
                    for row in s.scalars(
                        select(plans).where(
                            plans.tenant_id == p["tenant_id"],
                            plans.seller_id == p["seller_id"],
                            plans.external_key.in_(["current:" + identity for identity in ids]),
                        )
                    )
                }
            computed = []
            for lead in page:
                digest = fingerprint(lead)
                old = prior.get(lead["id"], {})
                if (
                    old.get("input_fingerprint") == digest
                    and (timestamp(old.get("expires_at")) or inputs["now"]) > inputs["now"]
                ):
                    unchanged += 1
                    continue
                choice = selected.get(lead["id"])
                alternatives = (
                    future_slots(
                        lead,
                        [],
                        now=inputs["now"],
                        policy=inputs["policy"],
                        horizon_days=inputs["horizon_hours"] / 24,
                        predictor=predictors.get(lead["id"]),
                        estimates=estimates,
                        staffed_windows=inputs["staffed_windows"],
                        rep_minutes=inputs["rep_minutes"],
                        inventory=inputs["inventory"],
                        rep_busy_until=inputs["rep_busy_until"],
                    )
                    if choice
                    else []
                )
                if choice:
                    local = timestamp(choice["at"]).astimezone(ZoneInfo(lead["timezone"]))
                    prediction = next(
                        (r["predictions"] for r in alternatives if r["at"] == choice["at"]),
                        estimates.get(
                            prediction_key(
                                {
                                    "segment": lead.get("segment", "unknown"),
                                    "local_hour": local.hour,
                                    "local_weekday": local.weekday(),
                                    "attempt_count": lead.get("attempt_count", 0),
                                }
                            )
                        )
                        or estimates["pooled"],
                    )
                    choice = {
                        **choice,
                        "local_hour": local.hour,
                        "predictions": prediction,
                        "reason": "requested_callback"
                        if choice["callback"]
                        else "shared_capacity_and_expected_conversion",
                        "constraint_scope": "whole_list_schedule",
                    }
                computed.append((lead, choice, alternatives))
            if not computed:
                continue
            with self.db.transaction() as s:
                user, _, _, _ = self.report_scope(s, p)
                if user["role"] != "admin":
                    fail(403, "Planning owner no longer has administrator access")
                if self.report_revision(s, p, lock=True) != generation:
                    superseded += len(inputs["leads"]) - offset
                    self.jobs.enqueue_in(
                        s,
                        "ml",
                        {
                            "kind": "retry_plan_refresh",
                            "seller_id": p["seller_id"],
                            "user_id": p["user_id"],
                            "campaign_id": campaign_id,
                        },
                        f"retry-plans:{campaign_id}:{self.report_revision(s, p)}",
                        tenant_id=p["tenant_id"],
                    )
                    break
                model, plans = MODELS["leads"], MODELS["scheduling_plans"]
                current = {
                    row.id: row
                    for row in s.scalars(
                        select(model).where(
                            model.tenant_id == p["tenant_id"],
                            model.seller_id == p["seller_id"],
                            model.id.in_(ids),
                        )
                    )
                }
                existing = {
                    row.data["lead_id"]: row
                    for row in s.scalars(
                        select(plans).where(
                            plans.tenant_id == p["tenant_id"],
                            plans.seller_id == p["seller_id"],
                            plans.external_key.in_(["current:" + identity for identity in ids]),
                        )
                    )
                }
                for old, choice, alternatives in computed:
                    lead = current.get(old["id"])
                    if not lead or lead.version != old["version"]:
                        superseded += 1
                        continue
                    if (
                        choice
                        and lead.data.get("attempt_count")
                        and not lead.data.get("callback_due_at")
                        and lead.data.get("next_attempt_at") != choice["at"]
                    ):
                        update(lead, next_attempt_at=choice["at"])
                        old = {**old, "next_attempt_at": choice["at"], "version": lead.version}
                    value = {
                        "lead_id": lead.id,
                        "selected": choice,
                        "alternatives": alternatives,
                        "status": "ready" if choice else "unavailable",
                        "dataset_id": dataset_id,
                        "input_fingerprint": fingerprint(old),
                        "source_lead_version": lead.version,
                        "created_at": inputs["now"].isoformat(),
                        "expires_at": (inputs["now"] + timedelta(minutes=10)).isoformat(),
                        "schedule_fingerprint": forecast["input_fingerprint"],
                        "planning_limited": forecast["planning_limited"],
                    }
                    if lead.id in existing:
                        update(existing[lead.id], **value)
                    else:
                        put(
                            s,
                            "scheduling_plans",
                            p["tenant_id"],
                            p["seller_id"],
                            value,
                            key="current:" + lead.id,
                            now=inputs["now"],
                        )
                    saved += 1
                s.flush()
                generation = self.report_revision(s, p)
        return {
            "saved": saved,
            "superseded": superseded,
            "unchanged": unchanged,
            "omitted_schedule_rows": forecast["schedule_rows_omitted"],
        }

    def refresh_optimization(self, p):
        from app.optimization.conversations import conversation_associations

        with self.db.transaction() as s:
            s.info["row_cache"] = {}
            self.ensure_experiment_cohort(s, p)
        dataset = self.save_operational_dataset(p)
        with self.db.transaction(False) as s:
            campaign = self.scoped(s, "campaigns", p)[0]
            campaign_id = campaign.id
            vocabulary = dict(campaign.data.get("conversation_vocabulary", {"version": 1, "aliases": []}))
        conversation_report = {
            **conversation_associations(
                [r for r in dataset["rows"] if r.get("campaign_id") == campaign_id],
                as_of=self.clock(),
                vocabulary=vocabulary,
            ),
            "goal": dataset["goal"],
            "window_days": dataset["window_days"],
        }
        with self.db.transaction() as s:
            s.info["row_cache"] = {}
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
                if number["health"]["status"] in {"blocked", "investigating"} and number["lifecycle"] in {
                    "healthy",
                    "limited_real_use",
                }:
                    record = self.required(s, "number_inventory", number["id"], p)
                    update(
                        record,
                        lifecycle="investigating",
                        inbound_retained=True,
                        health_review_reason=number["health"]["reason"],
                    )
                    audit(
                        s,
                        p["tenant_id"],
                        p["seller_id"],
                        p["user_id"],
                        "number.delivery_hold",
                        record.id,
                        self.clock(),
                        {
                            "samples": number["health"]["samples"],
                            "policy_version": number["health"]["policy_version"],
                            "reason_codes": number["health"]["reason_codes"],
                        },
                    )
            campaign = self.scoped(s, "campaigns", p)[0]
            update(campaign, selection_profile=self.selection_profile(s, p, dataset["rows"], config))
            if (
                dataset.get("conversation_revision") == campaign.data.get("conversation_revision", 0)
                and dataset["goal"] == config["conversion_goal"]
                and dataset["window_days"] == config["window_days"]
                and vocabulary["version"]
                == campaign.data.get("conversation_vocabulary", {"version": 1})["version"]
            ):
                update(campaign, conversation_report=conversation_report)
            emit(s, p["tenant_id"], "optimization.updated", campaign, self.clock())
        planning = self.refresh_schedule_plans(p, campaign_id, dataset["id"])
        training = self.train_operational(p) if config["auto_train"] else {"status": "disabled"}
        self.rebuild_optimization_report(p)
        return {
            "dataset_id": dataset["id"],
            "rows": len(dataset["rows"]),
            "training": training,
            "planning": planning,
        }
