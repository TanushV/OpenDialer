import hashlib
import hmac
import json
import math
from datetime import timedelta

from sqlalchemy import func, or_, select

from app.db import MODELS, ActiveClaim, audit, doc, emit, get, put, update
from app.optimization.experiments import ComparisonWorkflows
from app.optimization.learning import (
    freeze_features,
    future_slots,
    training_rows,
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
    def selected_campaign(self, s, p, identity=None, *, lock=False):
        model = MODELS["campaigns"]
        query = select(model).where(model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"])
        if identity := identity or p.get("campaign_id"):
            query = query.where(model.id == identity)
        query = query.order_by(model.created_at, model.id).limit(1)
        row = s.scalar(query.with_for_update() if lock else query)
        if not row:
            from app.service import fail

            fail(404, "Campaign not found")
        return row

    def ensure_experiment_cohort(self, s, p, campaign_id=None):
        from app.optimization.learning import stable_control

        campaign = (
            self.required(s, "campaigns", campaign_id, p) if campaign_id else self.selected_campaign(s, p)
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
                campaign = self.selected_campaign(s, p)
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
            with self.db.transaction(False) as session:
                model = MODELS["campaigns"]
                campaigns = list(
                    session.scalars(
                        select(model.id).where(
                            model.tenant_id == user["tenant_id"],
                            model.seller_id == user["seller_id"],
                        )
                    )
                )
            for campaign_id in campaigns:
                self.jobs.enqueue(
                    "ml",
                    {
                        "kind": "optimization_refresh",
                        "seller_id": user["seller_id"],
                        "user_id": user["id"],
                        "campaign_id": campaign_id,
                    },
                    f"optimization-refresh:{user['seller_id']}:{campaign_id}:{slot}",
                    tenant_id=user["tenant_id"],
                )

    def optimization_config(self, s, p, campaign_id=None):
        from app.optimization.api import HealthPolicy

        campaign = (
            self.required(s, "campaigns", campaign_id, p) if campaign_id else self.selected_campaign(s, p)
        )
        config = {**DEFAULTS, **campaign.data.get("optimization", {})}
        config["health_policy"] = HealthPolicy.model_validate(config.get("health_policy", {})).model_dump()
        config["estimate_policy"] = EstimatePolicy.model_validate(
            config.get("estimate_policy", {})
        ).model_dump(mode="json")
        return config

    def inventory_evidence(self, s, p, fast=False):
        from app.optimization.projections import read_number_health_metrics

        return self.inventory_capacity(s, p, health=None if fast else read_number_health_metrics(self, p))

    def inventory_capacity(self, s, p, *, health=None):
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
        if health is None:
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

    def selection_profile(self, s, p, statistics, config):
        from app.optimization.learning import distribution_from_counts

        campaign = self.selected_campaign(s, p)
        actions = [
            r.id
            for r in self.scoped(s, "treatment_variants", p)
            if r.data.get("approved") and r.data.get("campaign_id") == campaign.id
        ] or ["approved-default"]
        return {
            "as_of": self.clock().isoformat(),
            "goal": config["conversion_goal"],
            "samples": statistics["total"].get("rows", 0),
            "window_days": config["window_days"],
            "estimate_policy_version": EstimatePolicy.model_validate(config["estimate_policy"]).version,
            "work_assumption_seconds": config["handling_seconds"],
            "statistics": {
                k: v for k, v in statistics.items() if k not in {"treatment_counts", "retry_observations"}
            },
            "retry_observations": statistics["retry_observations"],
            "treatments": {
                segment: distribution_from_counts(actions, counts, exploration=config["exploration"])
                for segment, counts in statistics["treatment_counts"].items()
            },
        }

    def operational_dataset(self, s, p, config=None, *, limit=20000):
        """Explicit bounded snapshot. Filter identities before decrypting related content."""
        from decimal import Decimal

        from app.coaching.context import lineage_key
        from app.optimization.conversations import active_observations
        from app.postcall.analysis import learning_observations

        if not 1 <= limit <= 20000:
            raise ValueError("Training snapshot bound must be between1 and20000 attempts")
        campaign = self.selected_campaign(s, p)
        config = config or self.optimization_config(s, p, campaign.id)
        model, leads = MODELS["call_attempts"], MODELS["leads"]
        selected = list(
            s.scalars(
                select(model.id)
                .join(
                    leads,
                    (model.op_lead_id == leads.id)
                    & (model.tenant_id == leads.tenant_id)
                    & (model.seller_id == leads.seller_id),
                )
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    leads.op_campaign_id == campaign.id,
                    model.op_at <= self.clock(),
                )
                .order_by(model.op_at.desc(), model.id.desc())
                .limit(limit)
            )
        )
        data, encoded_bytes = [], 0
        for offset in range(0, len(selected), 500):
            ids = selected[offset : offset + 500]

            def related(kind, field, identities):
                table = MODELS[kind]
                return (
                    list(
                        s.scalars(
                            select(table).where(
                                table.tenant_id == p["tenant_id"],
                                table.seller_id == p["seller_id"],
                                getattr(table, field).in_(identities),
                            )
                        )
                    )
                    if identities
                    else []
                )

            calls = [doc(r) for r in related("call_attempts", "id", ids)]
            snapshots = [
                doc(r)
                for r in related("feature_snapshots", "id", [c.get("feature_snapshot_id") for c in calls])
            ]
            events = [
                doc(r)
                for r in related("conversion_events", "op_lead_id", list({c["lead_id"] for c in calls}))
            ]
            notes = {r.external_key: r.data for r in related("notes", "external_key", ids)}
            memories = {r.external_key: r.data for r in related("conversation_states", "external_key", ids)}
            lineage, work = {}, {}
            for row in related("outcomes", "ctx_key", ids):
                if row.data.get("kind") == "attempt_work":
                    work[row.data["call_id"]] = row.data
                elif row.data.get("kind") == "conversation_lineage":
                    lineage.setdefault(row.data["call_id"], {})[
                        lineage_key(row.data["id"], row.data["version"])
                    ] = row.data
            costs, incomplete, verified = {}, set(), set()
            for record in related("usage_ledger", "op_call_id", ids):
                value = record.data
                key = value.get("call_id") or value.get("object_id")
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
                call["campaign_id"] = campaign.id
                if call["id"] in work:
                    call.update(
                        {
                            key: work[call["id"]].get(key)
                            for key in ("rep_work_intervals", "rep_work_incomplete")
                        }
                    )
                memory = memories.get(call["id"], {})
                observed = (
                    learning_observations(memory["observations"], lineage.get(call["id"]))
                    if memory.get("schema_version") == 2
                    else notes.get(call["id"], {}).get("observations", [])
                )
                call["observations"] = active_observations(observed, self.clock())
                complete = call["id"] in verified and call["id"] not in incomplete
                call["cost_status"] = "settled" if complete else "uncertain"
                call["observed_cost_usd"] = float(costs.get(call["id"], 0)) if complete else None
            page = training_rows(
                calls,
                snapshots,
                events,
                now=self.clock(),
                goal=config["conversion_goal"],
                window_days=config["window_days"],
            )
            encoded_bytes += len(json.dumps(page, allow_nan=False).encode())
            if encoded_bytes > 16 * 1024 * 1024:
                raise ValueError("Training snapshot exceeds16MiB; narrow the selected cohort")
            data.extend(page)
        return sorted(data, key=lambda r: (r["at"], r["call_id"]))

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

    def forecast_input(self, s, p, *, campaign_id=None, capacity=None, lead_ids=None):
        if lead_ids is not None and len(lead_ids) > 100:
            raise ValueError("At most100 changed leads per planning page")
        config = self.optimization_config(s, p, campaign_id)
        campaign = (
            self.required(s, "campaigns", campaign_id, p) if campaign_id else self.selected_campaign(s, p)
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
                        *([model.id.in_(lead_ids)] if lead_ids is not None else []),
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

    def build_forecast(self, p, campaign_id, capacity=None, *, lease=None):
        """Snapshot/read, pure calculation, then a short source-checked publication."""
        from app.service import fail

        key = f"planning:{campaign_id}:{capacity or 'configured'}"
        with self.db.transaction(False) as s:
            current, _, _, _ = self.report_scope(s, p)
            if current["role"] != "admin":
                fail(403, "Administrator access is required to plan inventory")
            generation = self.report_revision(s, current)
            inputs, metadata = self.forecast_input(s, current, campaign_id=campaign_id, capacity=capacity)
            config = self.optimization_config(s, current, campaign_id)
            campaign = self.required(s, "campaigns", campaign_id, current)
            profile = campaign.data.get("selection_profile", {})
            if not profile_current(profile, config):
                profile = {}
        result = {**capacity_forecast(**inputs, schedule_row_limit=None), **metadata}
        if capacity is None:
            plans = self._publish_schedule_plans(
                p, campaign_id, inputs, result, config, profile, generation, lease=lease
            )
            if plans["status"] != "ready":
                return {"status": "superseded"}
            generation = plans["source_revision"]
        # Retain bounded display detail; per-lead plans above preserve all scheduled work.
        omitted = max(0, len(result["expanded_schedule"]) - 1000)
        for field in (
            "existing_schedule",
            "expanded_schedule",
            "hypothetical_added_schedule",
            "conditional_pending_schedule",
        ):
            result[field] = result[field][:1000]
        result["schedule_rows_omitted"] = omitted
        result["omitted_rows"]["schedule"] = omitted
        with self.db.transaction() as s:
            self.report_revision(s, p, lock=True)
            self.jobs.require_owned_in(s, lease)
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

    def report_details(self, s, p):
        """Bounded saved decisions/plans; no horizon computation or historical dataset."""
        reservations = MODELS["reservations"]
        query = select(reservations).where(
            reservations.tenant_id == p["tenant_id"], reservations.seller_id == p["seller_id"]
        )
        if p["role"] == "rep":
            query = query.where(reservations.op_owner_id == p["user_id"])
        recent = list(s.scalars(query.order_by(reservations.op_at.desc(), reservations.id.desc()).limit(31)))
        ids = [
            r.data["policy_decision_id"] for r in reversed(recent[:30]) if r.data.get("policy_decision_id")
        ]
        model = MODELS["policy_decisions"]
        records = (
            {
                r.id: doc(r)
                for r in s.scalars(
                    select(model).where(
                        model.tenant_id == p["tenant_id"],
                        model.seller_id == p["seller_id"],
                        model.id.in_(ids),
                    )
                )
            }
            if ids
            else {}
        )
        decisions = [
            records[i]
            for i in ids
            if i in records and (p["role"] != "rep" or records[i].get("rep_id") == p["user_id"])
        ]
        model, leads = MODELS["scheduling_plans"], MODELS["leads"]
        planned = list(
            s.execute(
                select(model, leads)
                .join(
                    leads,
                    (model.op_lead_id == leads.id)
                    & (model.tenant_id == leads.tenant_id)
                    & (model.seller_id == leads.seller_id),
                )
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.external_key == "current:" + leads.id,
                )
                .order_by(model.op_at.desc(), model.id.desc())
                .limit(101)
            )
        )
        futures = []
        for record, lead in planned[:100]:
            plan = record.data
            if (
                plan.get("selected")
                and lead.data.get("status") == "eligible"
                and not lead.data.get("outreach_hold")
                and not lead.data.get("goal_completed")
                and plan.get("source_lead_version") == lead.version
                and (timestamp(plan.get("expires_at")) or self.clock()) > self.clock()
            ):
                futures.append({**plan["selected"], "name": lead.data["name"]})
        return {
            "decisions": decisions,
            "plans": [doc(r) for r, _ in planned[:100]],
            "future_slots": futures,
            "detail_coverage": {
                "decisions_inspected": min(len(recent), 30),
                "decisions_limited": len(recent) > 30,
                "plans_inspected": min(len(planned), 100),
                "plans_limited": len(planned) > 100,
            },
        }

    def saved_forecast(self, s, p):
        campaign = self.selected_campaign(s, p)
        row = self._by_key(s, p, "report_projections", f"planning:{campaign.id}:configured")
        saved = row.data if row else {}
        current = self.report_revision(s, p)
        fresh = (
            saved.get("source_revision") == current
            and (timestamp(saved.get("expires_at")) or self.clock()) > self.clock()
        )
        return {
            "regions": [],
            "capacity_shortfall": None,
            "staffing_shortfall": None,
            "number_shortfall": None,
            **saved.get("report", {}),
            "projection": {
                "status": "ready" if fresh else "stale" if saved else "pending",
                "as_of": saved.get("as_of"),
                "source_revision": saved.get("source_revision"),
                "current_revision": current,
            },
        }

    def optimization_report(self, p, epoch=None):
        from app.optimization.projections import (
            read_coaching_metrics,
            read_comparison_metrics,
            read_dataset_metrics,
            read_drift_metrics,
            read_trial_metrics,
        )

        with self.db.transaction(False) as s:
            s.info["row_cache"] = {}
            trial = read_trial_metrics(self, p, epoch=epoch)
            cohort_history = trial.pop("cohort_history")
            measured = self.metrics(p)
            return {
                "settings": self.optimization_config(s, p),
                "comparisons": read_comparison_metrics(self, p),
                "cohort_history": cohort_history,
                "forecast": self.saved_forecast(s, p),
                "numbers": self.inventory_evidence(s, p),
                "metrics": {
                    **measured,
                    "imported_leads": measured["original_cohort"],
                    "original_eligible_leads": measured["original_eligible_denominator"],
                    "suppressed_now": measured["suppressed"],
                    "human_answers": measured["human_pickups"],
                    "unique_right_party": measured["right_party_unique"],
                },
                "dataset": read_dataset_metrics(self, p),
                **self.report_details(s, p),
                "drift": read_drift_metrics(self, p),
                "trial": trial,
                "coaching": read_coaching_metrics(self, p),
                "simulation": self.settings.simulation,
                "as_of": self.clock().isoformat(),
            }

    def save_operational_dataset(self, p, *, lease=None):
        with self.db.transaction(False) as s:
            user, _, _, _ = self.report_scope(s, p)
            if user["role"] != "admin":
                from app.service import fail

                fail(403, "Administrator access required for a learning snapshot")
            generation = self.report_revision(s, p)
            config = self.optimization_config(s, p)
            campaign = self.selected_campaign(s, p)
            revision = campaign.data.get("conversation_revision", 0)
            campaign_id = campaign.id
            baseline = {k: config[k] for k in ("estimate_policy", "handling_seconds")}
            data = self.operational_dataset(s, p, config)
            key = hashlib.sha256(
                json.dumps(
                    [config["conversion_goal"], config["window_days"], data, revision, baseline],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        with self.db.transaction() as s:
            self.report_revision(s, p, lock=True)
            self.jobs.require_owned_in(s, lease)
            campaign = self.required(s, "campaigns", campaign_id, p)
            user, _, _, _ = self.report_scope(s, p)
            if user["role"] != "admin":
                from app.service import fail

                fail(403, "Learning owner no longer has administrator access")
            if self.report_revision(s, p, lock=True) != generation:
                raise RuntimeError("Learning sources changed during snapshot; retry current work")
            previous = self._by_key(s, p, "learning_datasets", key)
            result = previous or put(
                s,
                "learning_datasets",
                p["tenant_id"],
                p["seller_id"],
                {
                    "campaign_id": campaign_id,
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

    def train_operational(self, p, dataset=None):
        dataset = dataset if dataset is not None else self.save_operational_dataset(p)
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

    def refresh_schedule_plans(
        self, p, campaign_id, dataset_id=None, *, lead_ids=None, internal=False, lease=None
    ):
        """Use the common allocator; changed pages retain other saved allocations."""
        from app.optimization.capacity import plan_changed
        from app.service import fail

        with self.db.transaction(False) as s:
            if not internal:
                user, _, _, _ = self.report_scope(s, p)
                if user["role"] != "admin":
                    fail(403, "Administrator access required to refresh retry plans")
            generation = self.report_revision(s, p)
            inputs, _ = self.forecast_input(s, p, campaign_id=campaign_id, lead_ids=lead_ids)
            config = self.optimization_config(s, p, campaign_id)
            campaign = self.required(s, "campaigns", campaign_id, p)
            profile = campaign.data.get("selection_profile", {})
            if not profile_current(profile, config):
                profile = {}
            fixed = []
            if lead_ids is not None:
                plans, leads = MODELS["scheduling_plans"], MODELS["leads"]
                saved = list(
                    s.execute(
                        select(plans, leads.version)
                        .join(
                            leads,
                            (leads.id == plans.op_lead_id)
                            & (leads.tenant_id == plans.tenant_id)
                            & (leads.seller_id == plans.seller_id),
                        )
                        .where(
                            plans.tenant_id == p["tenant_id"],
                            plans.seller_id == p["seller_id"],
                            plans.op_status == "ready",
                            plans.op_lead_id.not_in(lead_ids),
                        )
                        .limit(10001)
                    )
                )
                if len(saved) > 10000:
                    return {
                        "status": "limited",
                        "saved": 0,
                        "superseded": 0,
                        "unchanged": 0,
                        "reason": "saved_allocation_limit",
                        "omitted_schedule_rows": None,
                    }
                for record, version in saved:
                    value = record.data
                    if (
                        value.get("source_lead_version") == version
                        and (timestamp(value.get("expires_at")) or inputs["now"]) > inputs["now"]
                        and value.get("selected")
                    ):
                        fixed.append(value["selected"])
        forecast = (
            plan_changed(
                **{
                    k: v
                    for k, v in inputs.items()
                    if k
                    not in {
                        "daily_capacity_per_number",
                        "additional_ready_after_hours",
                        "retry_observations",
                    }
                },
                fixed_schedule=fixed,
            )
            if lead_ids is not None
            else capacity_forecast(**inputs, schedule_row_limit=None)
        )
        return self._publish_schedule_plans(
            p,
            campaign_id,
            inputs,
            forecast,
            config,
            profile,
            generation,
            dataset_id=dataset_id,
            internal=internal,
            lead_ids=lead_ids,
            lease=lease,
        )

    def _publish_schedule_plans(
        self,
        p,
        campaign_id,
        inputs,
        forecast,
        config,
        profile,
        generation,
        *,
        dataset_id=None,
        internal=False,
        lead_ids=None,
        lease=None,
    ):
        from zoneinfo import ZoneInfo

        from app.service import fail

        estimates = {**profile.get("predictions", {}), "pooled": saved_prediction(profile, {}, config)}
        selected = {row["lead_id"]: row for row in forecast["existing_schedule"]}
        saved, superseded, unchanged = 0, 0, 0
        common = {k: v for k, v in inputs.items() if k not in {"now", "leads"}}
        common["time_bucket"] = int(inputs["now"].timestamp()) // 600
        common_hash = hashlib.sha256(json.dumps(common, sort_keys=True, default=str).encode()).hexdigest()

        def fingerprint(lead):
            # A different lead's priority may move or displace this allocation
            # without changing this lead's inputs. Fingerprint the pure choice,
            # before adding display predictions or publication metadata.
            return hashlib.sha256(
                json.dumps(
                    [common_hash, lead, selected.get(lead["id"])], sort_keys=True, default=str
                ).encode()
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
                # Derive only the cells needed by this bounded page; keep the
                # same canonical sparse estimates used by live queue selection.
                for day in (0, 5):
                    for hour in range(24):
                        features = {
                            "segment": lead.get("segment", "unknown"),
                            "attempt_count": lead.get("attempt_count", 0),
                            "local_hour": hour,
                            "local_weekday": day,
                        }
                        key = prediction_key(features)
                        if key not in estimates:
                            estimates[key] = saved_prediction(profile, features, config)
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
                        "constraint_scope": forecast.get("constraint_scope", "whole_list_schedule"),
                    }
                computed.append((lead, choice, alternatives))
            if not computed:
                continue
            with self.db.transaction() as s:
                self.report_revision(s, p, lock=True)
                self.jobs.require_owned_in(s, lease)
                if not internal:
                    user, _, _, _ = self.report_scope(s, p)
                    if user["role"] != "admin":
                        fail(403, "Planning owner no longer has administrator access")
                if self.report_revision(s, p, lock=True) != generation:
                    superseded += len(inputs["leads"]) - offset
                    if lead_ids is None:
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
                        "campaign_id": campaign_id,
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
                        "priority_rebalance_pending": forecast.get("rebalance_required", False),
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
        rebalance_job = None
        if not superseded and forecast.get("rebalance_required"):
            with self.db.transaction() as s:
                if self.report_revision(s, p, lock=True) != generation:
                    return {"status": "superseded"}
                self.jobs.require_owned_in(s, lease)
                rebalance_job = self.jobs.enqueue_in(
                    s,
                    "ml",
                    {
                        "kind": "retry_plan_refresh",
                        "seller_id": p["seller_id"],
                        "user_id": p.get("user_id"),
                        "campaign_id": campaign_id,
                        "internal": True,
                        "input_fingerprint": forecast["input_fingerprint"],
                    },
                    f"planning-rebalance:{campaign_id}",
                    tenant_id=p["tenant_id"],
                    priority=16,
                    latest=True,
                )
        return {
            "status": "superseded" if superseded else "ready",
            "source_revision": generation,
            "saved": saved,
            "superseded": superseded,
            "unchanged": unchanged,
            "omitted_schedule_rows": forecast["schedule_rows_omitted"],
            "rebalance_job_id": rebalance_job["id"] if rebalance_job else None,
        }

    def refresh_observed_health(self, p, *, lease=None):
        with self.db.transaction(False) as s:
            numbers = self.inventory_evidence(s, p)
            generation = self.report_revision(s, p)
        updated = 0
        for offset in range(0, len(numbers), 100):
            with self.db.transaction() as s:
                if self.report_revision(s, p, lock=True) != generation:
                    return {"status": "superseded", "updated": updated}
                self.jobs.require_owned_in(s, lease)
                for number in numbers[offset : offset + 100]:
                    existing = self._by_key(s, p, "number_health", number["id"])
                    value = {**number["health"], "number_id": number["id"]}
                    if not existing:
                        put(
                            s,
                            "number_health",
                            p["tenant_id"],
                            p["seller_id"],
                            value,
                            key=number["id"],
                            now=self.clock(),
                        )
                        updated += 1
                    elif existing.data != value:
                        update(existing, **value)
                        updated += 1
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
                s.flush()
                generation = self.report_revision(s, p)
        return {"status": "ready", "updated": updated}

    def refresh_learning(self, p, *, lease=None):
        """Explicit CPU-lane work; one bounded dataset for associations and optional training."""
        from app.optimization.conversations import conversation_associations

        dataset = self.save_operational_dataset(p, lease=lease)
        with self.db.transaction(False) as s:
            user, _, _, _ = self.report_scope(s, p)
            if user["role"] != "admin":
                from app.service import fail

                fail(403, "Administrator access required for learning refresh")
            campaign = self.selected_campaign(s, p)
            campaign_id = campaign.id
            vocabulary = dict(campaign.data.get("conversation_vocabulary", {"version": 1, "aliases": []}))
        report = {
            **conversation_associations(
                [r for r in dataset["rows"] if r.get("campaign_id") == campaign_id],
                as_of=self.clock(),
                vocabulary=vocabulary,
            ),
            "goal": dataset["goal"],
            "window_days": dataset["window_days"],
        }
        with self.db.transaction() as s:
            self.report_revision(s, p, lock=True)
            self.jobs.require_owned_in(s, lease)
            owner, _, _, _ = self.report_scope(s, p)
            if owner["role"] != "admin":
                from app.service import fail

                fail(403, "Learning owner no longer has administrator access")
            campaign = self.required(s, "campaigns", campaign_id, p)
            config = self.optimization_config(s, p, campaign_id)
            if not (
                dataset.get("conversation_revision") == campaign.data.get("conversation_revision", 0)
                and dataset["goal"] == config["conversion_goal"]
                and dataset["window_days"] == config["window_days"]
                and vocabulary["version"]
                == campaign.data.get("conversation_vocabulary", {"version": 1})["version"]
            ):
                return {"status": "superseded", "dataset_id": dataset["id"]}
            update(campaign, conversation_report=report)
            emit(s, p["tenant_id"], "optimization.updated", campaign, self.clock())
        training = self.train_operational(p, dataset) if config["auto_train"] else {"status": "disabled"}
        return {
            "status": "ready",
            "dataset_id": dataset["id"],
            "rows": len(dataset["rows"]),
            "training": training,
        }

    def refresh_optimization(self, p):
        from app.optimization.projections import read_estimate_statistics

        if not self.prepare_metric_read(p):
            return {"status": "pending"}
        with self.db.transaction(False) as s:
            user, _, _, _ = self.report_scope(s, p)
            if user["role"] != "admin":
                from app.service import fail

                fail(403, "Administrator access required for optimization refresh")
            generation = self.report_revision(s, p)
            campaign = self.selected_campaign(s, p)
            campaign_id = campaign.id
            config = self.optimization_config(s, p, campaign_id)
            statistics = read_estimate_statistics(self, p)
            profile = self.selection_profile(s, p, statistics, config)
            stable_statistics = {
                **statistics,
                "retry_observations": [
                    {k: v for k, v in r.items() if k != "available_at"}
                    for r in statistics["retry_observations"]
                ],
            }
            fingerprint = hashlib.sha256(
                json.dumps(
                    [
                        stable_statistics,
                        config,
                        campaign.data.get("conversation_revision", 0),
                        campaign.data.get("conversation_vocabulary", {"version": 1}),
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        with self.db.transaction() as s:
            user, _, _, _ = self.report_scope(s, p)
            if user["role"] != "admin":
                from app.service import fail

                fail(403, "Optimization owner no longer has administrator access")
            if self.report_revision(s, p, lock=True) != generation:
                return {"status": "superseded"}
            campaign = self.required(s, "campaigns", campaign_id, p)
            update(campaign, selection_profile=profile)
            jobs = []
            for kind in ("health_refresh", "forecast_refresh", "learning_refresh"):
                key = (
                    "operational-train:snapshot:"
                    if kind == "learning_refresh"
                    else "optimization-work:" + kind + ":"
                ) + campaign_id
                jobs.append(
                    self.jobs.enqueue_in(
                        s,
                        "ml",
                        {
                            "kind": kind,
                            "seller_id": p["seller_id"],
                            "user_id": p["user_id"],
                            "campaign_id": campaign_id,
                            "input_fingerprint": fingerprint
                            if kind == "learning_refresh"
                            else str(generation),
                        },
                        key,
                        tenant_id=p["tenant_id"],
                        priority=-10
                        if kind == "learning_refresh"
                        else 25
                        if kind == "health_refresh"
                        else 15,
                        latest=True,
                    )
                )
            emit(s, p["tenant_id"], "optimization.updated", campaign, self.clock())
        self.rebuild_optimization_report(p)
        return {
            "status": "ready",
            "rows": profile["samples"],
            "jobs": [{"id": j["id"], "kind": j["payload"]["kind"]} for j in jobs],
        }
