import hashlib
import hmac
import json
import math
from datetime import timedelta

from sqlalchemy import func, or_, select

from app.db import MODELS, ActiveClaim, audit, doc, emit, get, put, update
from app.optimization.experiments import ComparisonWorkflows
from app.optimization.learning import freeze_features, training_rows
from app.optimization.numbering import HealthPolicy, capacity_forecast, number_health, timestamp
from app.scheduling.models import (
    OPTIMIZATION_DEFAULTS,
    EstimatePolicy,
    prediction_key,
    profile_current,
    saved_prediction,
)

STAGES = ("p_human", "p_right_party_given_human", "p_conversion_given_right_party")

DEFAULTS = OPTIMIZATION_DEFAULTS


def next_attempts(leads, schedule, estimate, *, limit=100):
    """Earliest planned attempt per lead, at most limit leads; compact estimate, no alternatives."""
    from zoneinfo import ZoneInfo

    by_id, seen, rows, cache = {lead["id"]: lead for lead in leads}, set(), [], {}
    ordered = sorted(
        (r for r in schedule if not r.get("contingent")),
        key=lambda r: (r["at"], r["priority_class"], r["lead_id"]),
    )
    for row in ordered:
        if row["lead_id"] in seen:
            continue
        seen.add(row["lead_id"])
        if len(rows) == limit:
            continue
        lead = by_id[row["lead_id"]]
        local = timestamp(row["at"]).astimezone(ZoneInfo(lead["timezone"]))
        features = {
            "segment": str(lead.get("segment", "unknown")),
            "attempt_count": lead.get("attempt_count", 0),
            "local_hour": local.hour,
            "local_weekday": local.weekday(),
        }
        key = prediction_key(features)
        if key not in cache:
            value = estimate(features)
            cache[key] = {
                **{k: value[k] for k in (*STAGES, "expected_rep_seconds", "observations")},
                "estimate_level": value["estimate_level"],
            }
        rows.append(
            {
                "lead_id": row["lead_id"],
                "lead_version": lead.get("version"),
                "at": row["at"],
                "end_at": row["end_at"],
                "local_hour": local.hour,
                "rep_id": row["rep_id"],
                "number_id": row["number_id"],
                "priority_class": row["priority_class"],
                "callback": row["callback"],
                "reason": "requested_callback"
                if row["callback"]
                else "shared_capacity_and_expected_conversion",
                "predicted_conversions": row["predicted_conversions"],
                "estimate": cache[key],
            }
        )
    return rows, len(seen) - len(rows)


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
        from app.optimization.learning import cohort_arm, cohort_fraction

        campaign = (
            self.required(s, "campaigns", campaign_id, p) if campaign_id else self.selected_campaign(s, p)
        )
        epoch = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
        existing = {r.external_key for r in self.scoped(s, "experiment_assignments", p)}
        study = campaign.data.get("optimization_trial") or {}
        if study and timestamp(study["ends_at"]) < self.clock():
            return epoch
        candidates = self.scoped(s, "leads", p)
        fraction = cohort_fraction(campaign.data, self.optimization_config(s, p, campaign.id), self.clock())
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
                    "arm": (arm := cohort_arm(lead.id, fraction)),
                    "propensity": fraction if arm == "control" else 1 - fraction,
                    "allocation": {"control": fraction, "candidate": 1 - fraction},
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
        from app.optimization.learning import PRODUCTION_HOLDOUT_FRACTION, stable_control

        # The persistent production holdout only; trial arms come from recorded enrollment.
        holdout = config.get("production_holdout_fraction", PRODUCTION_HOLDOUT_FRACTION)
        if lead_id and stable_control(lead_id, holdout):
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

    def optimization_config(self, s, p, campaign_id=None, *, campaign=None):
        campaign = campaign or (
            self.required(s, "campaigns", campaign_id, p) if campaign_id else self.selected_campaign(s, p)
        )
        config = {**DEFAULTS, **campaign.data.get("optimization", {})}
        config["health_policy"] = HealthPolicy.model_validate(config.get("health_policy", {})).model_dump()
        config["estimate_policy"] = EstimatePolicy.model_validate(
            config.get("estimate_policy", {})
        ).model_dump(mode="json")
        # The latest configuration read in this transaction also supplies per-number health checks.
        s.info[("health_policy", p["tenant_id"], p["seller_id"])] = config["health_policy"]
        return config

    def health_policy_in(self, s, p):
        cached = s.info.get(("health_policy", p["tenant_id"], p["seller_id"]))
        return cached if cached is not None else self.optimization_config(s, p)["health_policy"]

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
        campaign = self.selected_campaign(s, p)
        config = self.optimization_config(s, p, campaign=campaign)
        leases = [r.data for r in self.scoped(s, "number_leases", p) if r.data.get("active")]
        ends = self.obligation_ends(s, p, claims, self.predicted_work_seconds(s, p, config, campaign))
        delivery = {
            identity: (value.get("delivery_samples", 0), value.get("delivery_failures", 0))
            for identity, value in health.items()
            if isinstance(value, dict) and value.get("assessment_schema") == 1
        }
        totals = [sum(v[i] for v in delivery.values()) for i in (0, 1)]
        result = []
        for n in self.scoped(s, "number_inventory", p):
            readiness = self.number_readiness_in(s, p, n)
            own = delivery.get(n.id, (0, 0))
            finishes = [ends[c.id] for c in claims if c.number_id == n.id] + [
                timestamp(x.get("expires_at")) or self.clock() for x in leases if x["number_id"] == n.id
            ]
            result.append(
                {
                    **doc(n),
                    **readiness,
                    "health": number_health(
                        health.get(n.id) or [],
                        pooled_attempts=[],
                        now=self.clock(),
                        external=n.data.get("external_reputation"),
                        policy=config["health_policy"],
                        configuration_errors=readiness["configuration_errors"],
                        delivery_reference={"samples": totals[0] - own[0], "failures": totals[1] - own[1]},
                    ),
                    "used_today": used.get(n.id, 0),
                    "reserved_attempts": sum(c.number_id == n.id and not c.call_id for c in claims),
                    "active_obligations": len(finishes),
                    "busy_until": max(finishes).isoformat() if finishes else None,
                }
            )
        return result

    def predicted_work_seconds(self, s, p, config, campaign=None):
        campaign = campaign or self.selected_campaign(s, p)
        profile = campaign.data.get("selection_profile", {})
        return saved_prediction(profile if profile_current(profile, config) else {}, {}, config)[
            "expected_rep_seconds"
        ]

    def obligation_ends(self, s, p, claims, work_seconds):
        """Predicted end of each obligation: live work from its start, a reservation at its lease.

        An overrunning call is treated as ending now and is re-forecast on the next refresh;
        no obligation is assumed to last for the whole horizon.
        """
        model, now = MODELS["call_attempts"], self.clock()
        identities = [c.call_id for c in claims if c.call_id]
        started = {
            row.id: timestamp(row.data.get("submitted_at") or row.data.get("started_at"))
            for row in (
                s.scalars(
                    select(model).where(
                        model.tenant_id == p["tenant_id"],
                        model.seller_id == p["seller_id"],
                        model.id.in_(identities),
                    )
                )
                if identities
                else []
            )
        }
        return {
            claim.id: max(now, (started.get(claim.call_id) or now) + timedelta(seconds=work_seconds))
            if claim.call_id
            else timestamp(claim.expires_at)
            for claim in claims
        }

    def selection_profile(self, s, p, statistics, config):
        # Treatment counts stay as evidence; no commit-time treatment policy is derived without
        # a registered comparison (REMEDIATION 9.4).
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
        from collections import Counter

        from app.compliance.operating import screening_page

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
        pooled = saved_prediction(profile, {}, config)
        # Live work occupies its rep for the predicted remaining work from call start.
        ends = self.obligation_ends(s, p, claims, pooled["expected_rep_seconds"])
        busy = {claim.rep_id: ends[claim.id].isoformat() for claim in claims}
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
        screening_counts, screening_reasons = Counter(), Counter()
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
            screening = screening_page(self, s, p, [row.data["phone"] for row in page], denied)
            for row in page:
                decision = screening[row.data["phone"]]
                screening_counts["cleared_count" if decision["decision"] == "clear" else "blocked_count"] += 1
                if decision["decision"] != "clear":
                    screening_reasons.update(decision["reason_codes"])
            leads.extend(
                {
                    **doc(row),
                    "suppressed": row.data["phone"] in denied,
                    "bound_number_id": bindings.get(row.id),
                    "active_obligation": row.id in claimed_leads,
                    "screening": screening[row.data["phone"]],
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
        # Slot values depend on local time only; no location effect is assumed without support.
        estimate_cache, by_key = {}, {}
        for lead in leads:
            cell = (str(lead.get("segment", "unknown")), lead.get("attempt_count", 0))
            if cell not in estimate_cache:
                values = {}
                for day in range(7):
                    for hour in range(24):
                        features = {
                            "segment": cell[0],
                            "attempt_count": cell[1],
                            "local_hour": hour,
                            "local_weekday": day,
                        }
                        key = prediction_key(features)
                        if key not in by_key:
                            prediction = saved_prediction(profile, features, config)
                            by_key[key] = math.prod(prediction[k] for k in STAGES)
                        values[f"{day}:{hour}"] = by_key[key]
                estimate_cache[cell] = values
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
            "screening": {
                "cleared_count": screening_counts["cleared_count"],
                "blocked_count": screening_counts["blocked_count"],
                "blocked_reasons": dict(screening_reasons),
                "as_of": self.clock().isoformat(),
            },
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
            profile = self.required(s, "campaigns", campaign_id, current).data.get("selection_profile", {})
            profile = profile if profile_current(profile, config) else {}
        result = {**capacity_forecast(**inputs, schedule_row_limit=None), **metadata}
        # One bounded per-campaign summary is the per-lead view; nothing here gates a call.
        result["next_attempts"], result["next_attempts_omitted"] = next_attempts(
            inputs["leads"], result["existing_schedule"], lambda f: saved_prediction(profile, f, config)
        )
        result["next_attempts_expire_at"] = (inputs["now"] + timedelta(minutes=10)).isoformat()
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
        campaign = self.selected_campaign(s, p)
        saved = self._by_key(s, p, "report_projections", f"planning:{campaign.id}:configured")
        report = saved.data.get("report", {}) if saved else {}
        planned = report.get("next_attempts", [])[:100]
        expires = timestamp(report.get("next_attempts_expire_at"))
        model = MODELS["leads"]
        leads = (
            {
                r.id: r
                for r in s.scalars(
                    select(model).where(
                        model.tenant_id == p["tenant_id"],
                        model.seller_id == p["seller_id"],
                        model.id.in_([row["lead_id"] for row in planned]),
                    )
                )
            }
            if planned and expires and expires > self.clock()
            else {}
        )
        futures = [
            {**row, "name": lead.data["name"]}
            for row in planned
            if (lead := leads.get(row["lead_id"]))
            and lead.data.get("status") == "eligible"
            and not lead.data.get("outreach_hold")
            and not lead.data.get("goal_completed")
            and row.get("lead_version") == lead.version
        ]
        return {
            "decisions": decisions,
            "plans": planned,
            "future_slots": futures,
            "detail_coverage": {
                "decisions_inspected": min(len(recent), 30),
                "decisions_limited": len(recent) > 30,
                "plans_inspected": len(planned),
                "plans_limited": bool(report.get("next_attempts_omitted")),
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
        from app.ml.training import training_support

        known = [r["human"] for r in data if r.get("human") in (0, 1)]
        positives = sum(known)
        reason = (
            "Independent labeled outcomes and both answer classes are required"
            if len(known) < minimum or min(positives, len(known) - positives) < 10
            else training_support(data)
        )
        if reason:
            # No bounded child is started when no stage could meet the review policy.
            return {
                "status": "insufficient_data",
                "rows": len(data),
                "classified_rows": len(known),
                "required_rows": minimum,
                "dataset_id": dataset["id"],
                "reason": reason,
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
        """Retired: forecasts publish one bounded summary; queued legacy jobs complete without writes."""
        return {"status": "ready", "saved": 0, "superseded": 0, "unchanged": 0, "retired": True}

    def refresh_observed_health(self, p, *, lease=None):
        from app.service import fail

        with self.db.transaction(False) as s:
            user, _, _, _ = self.report_scope(s, p)
            if user["role"] != "admin":
                fail(403, "Administrator access required for health refresh")
            numbers = self.inventory_evidence(s, p)
            generation = self.report_revision(s, p)
        updated = 0
        for offset in range(0, len(numbers), 100):
            with self.db.transaction() as s:
                if self.report_revision(s, p, lock=True) != generation:
                    return {"status": "superseded", "updated": updated}
                self.jobs.require_owned_in(s, lease)
                user, _, _, _ = self.report_scope(s, p)
                if user["role"] != "admin":
                    fail(403, "Health owner no longer has administrator access")
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

    def refresh_optimization(self, p, *, lease=None):
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
            self.jobs.require_owned_in(s, lease)
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
        self.rebuild_optimization_report(p, lease=lease)
        return {
            "status": "ready",
            "rows": profile["samples"],
            "jobs": [{"id": j["id"], "kind": j["payload"]["kind"]} for j in jobs],
        }
