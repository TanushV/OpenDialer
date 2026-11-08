import hashlib
import json
from collections import defaultdict
from datetime import UTC, timedelta

from sqlalchemy import func, or_, select

from app.optimization.experiments import ComparisonWorkflows
from app.optimization.geography import planning_zones
from app.optimization.learning import training_rows
from app.optimization.numbering import (
    HealthPolicy,
    adaptive_daily_limit,
    number_health,
    timestamp,
    warmup_allowance,
)
from app.platform.db import MODELS, ActiveClaim, Job, audit, doc, emit, get, put, update
from app.platform.jobs import jobdoc
from app.scheduling.models import (
    FEATURE_VERSIONS,
    OPTIMIZATION_DEFAULTS,
    EstimatePolicy,
    prediction_key,
    profile_current,
    saved_prediction,
)

# A finished attempt is resolved evidence for warmup growth.
RESOLVED_STATES = {"ENDED", "FINALIZED", "BUSY", "NO_ANSWER", "TEMPORARY_FAILURE", "INVALID_ENDPOINT"}
STAGES = ("p_human", "p_right_party_given_human", "p_conversion_given_right_party")

DEFAULTS = OPTIMIZATION_DEFAULTS
# Under the bounded training child's 16 MiB input, leaving room for its envelope.
TRAINING_SNAPSHOT_MAX_BYTES = 15 * 1024 * 1024


class TrainingRows(list):
    """As-of training rows; ``truncation`` records any bound that omitted evidence."""

    truncation = None


# R8.7: train at most once a day, and only when new labeled calls exist since the last training.
TRAINING_INTERVAL = timedelta(hours=20)


def training_due(last, labeled, now):
    at = timestamp(last.get("at")) if last.get("at") else None
    return labeled > last.get("labeled", -1) and (at is None or now - at >= TRAINING_INTERVAL)


def dataset_checksum(value):
    """Content address of a learning snapshot; the trainer re-derives it before fitting."""
    return hashlib.sha256(
        json.dumps(
            [
                value.get("goal"),
                value.get("window_days"),
                value.get("rows"),
                value.get("conversation_revision", 0),
                value.get("baseline"),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


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
        # The allocator scores scheduled slots in the first of the same plausible zones.
        local = timestamp(row["at"]).astimezone(ZoneInfo(planning_zones(lead)[0]))
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
        if value.get("feature_schema_version", 1) not in FEATURE_VERSIONS:
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

    def finalized_calls(self, s, tenant_id, seller_id):
        """How many of this seller's calls have a saved outcome: the refresh trigger (R2.10)."""
        model = MODELS["call_attempts"]
        return s.scalar(
            select(func.count())
            .select_from(model)
            .where(
                model.tenant_id == tenant_id,
                model.seller_id == seller_id,
                model.op_status == "FINALIZED",
            )
        )

    def schedule_optimization(self, tenant_id=None):
        """Queue a refresh only after a call is finalized, at most once a minute (section 8, R2.10)."""
        from app.platform.db import rows

        slot = int(self.clock().timestamp()) // 60
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
                campaigns = [
                    row.id
                    for row in session.scalars(
                        select(model).where(
                            model.tenant_id == user["tenant_id"],
                            model.seller_id == user["seller_id"],
                        )
                    )
                    if row.data.get("optimization_refreshed_finalized")
                    != self.finalized_calls(session, user["tenant_id"], user["seller_id"])
                ]
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

    def learning_owner(self, s, p):
        """The caller, with an admin role unless either the token or the user is a rep."""
        from app.service import fail

        user = self.required(s, "users", p["user_id"], p)
        if user.data.get("disabled"):
            fail(403, "Account is disabled")
        return {**p, "role": "rep" if "rep" in (p["role"], user.data["role"]) else "admin"}

    def request_optimization_refresh(self, p):
        """One refresh job per campaign; asking again requeues it once it has finished."""
        with self.db.transaction() as s:
            campaign = self.selected_campaign(s, p)
            key = f"optimization-refresh:{p['seller_id']}:{campaign.id}:requested"
            payload = {"kind": "optimization_refresh", "seller_id": p["seller_id"], "user_id": p["user_id"]}
            job = s.scalar(select(Job).where(Job.tenant_id == p["tenant_id"], Job.key == key))
            if not job:
                return self._refresh_status(
                    self.jobs.enqueue_in(s, "ml", payload, key, tenant_id=p["tenant_id"], priority=10)
                )
            if job.status not in {"queued", "running"}:
                job.payload, job.status, job.error, job.attempts = payload, "queued", None, 0
                job.lease_owner = job.lease_expires_at = None
                job.available_at = self.clock()
            return self._refresh_status(jobdoc(job))

    @staticmethod
    def _refresh_status(job):
        return {"status": job["status"], "job_id": job["id"]}

    def refresh_optimization_job(self, p, *, lease=None):
        from app.service import fail

        with self.db.transaction(False) as s:
            if self.learning_owner(s, p)["role"] != "admin":
                fail(403, "Administrator access is required to refresh optimization")
        return self.refresh_optimization(p, lease=lease)

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
        key = ("health_policy", p["tenant_id"], p["seller_id"])
        cached = s.info.get(key)
        if cached is None:
            model = MODELS["campaigns"]
            has_campaign = s.scalar(
                select(model.id)
                .where(model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"])
                .limit(1)
            )
            if has_campaign is None:
                # A seller without a campaign (fresh setup, preflight) uses the default policy.
                from app.optimization.numbering import HealthPolicy

                cached = HealthPolicy().model_dump()
            else:
                cached = self.optimization_config(s, p)["health_policy"]
            s.info[key] = cached
        return cached

    def inventory_evidence(self, s, p, fast=False, records=None):
        from app.optimization.projections import campaign_records, number_health

        if fast:
            return self.inventory_capacity(s, p)
        campaign = self.selected_campaign(s, p)
        records = records or campaign_records(s, p, campaign.id)
        health = number_health(
            records["calls"],
            records["features"],
            records["callbacks"],
            policy=self.optimization_config(s, p, campaign.id)["health_policy"],
            now=self.clock(),
        )
        return self.inventory_capacity(s, p, health=health)

    def inventory_capacity(self, s, p, *, health=None):
        """Current capacity is authoritative; health reads the last derived observation."""
        found = s.execute(
            select(ActiveClaim, ActiveClaim.expires_at <= self.clock()).where(
                ActiveClaim.tenant_id == p["tenant_id"], ActiveClaim.seller_id == p["seller_id"]
            )
        ).all()
        claims = [claim for claim, lapsed in found if claim.call_id or not lapsed]
        s.info["active_claims"] = claims
        s.info["expired_claims"] = [claim.id for claim, lapsed in found if not claim.call_id and lapsed]
        attempt = MODELS["call_attempts"]
        # A rolling day, so a reset at midnight can never double a number's traffic.
        today = self.clock() - timedelta(days=1)
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
        warming = {
            v["id"]: v
            for v in result
            if v.get("lifecycle") in {"limited_real_use", "healthy"} and v.get("warmup_started_at")
        }
        history = defaultdict(list)
        if warming:
            for number_id, at, lead_id, status in s.execute(
                select(attempt.op_number_id, attempt.op_at, attempt.op_lead_id, attempt.op_status).where(
                    attempt.tenant_id == p["tenant_id"],
                    attempt.seller_id == p["seller_id"],
                    attempt.op_number_id.in_(warming),
                    attempt.op_at >= min(timestamp(v["warmup_started_at"]) for v in warming.values()),
                )
            ):
                if at is not None:
                    at = at if at.tzinfo else at.replace(tzinfo=UTC)
                    history[number_id].append((at, lead_id, status in RESOLVED_STATES))
        for value in result:
            value["approved_daily_limit"] = value["daily_limit"]
            if value["id"] in warming:
                value["warmup_allowance"] = warmup_allowance(value, history[value["id"]], self.clock())
            value["daily_limit"] = adaptive_daily_limit(value, value["health"])
            value["capacity_mode"] = "adaptive"
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

    def operational_dataset(self, s, p, config=None, *, limit=20000, now=None, include_unclassified=False):
        """Explicit bounded snapshot, newest attempts first. Filter identities before
        decrypting related content. A bound that omits evidence is recorded, not raised."""
        from decimal import Decimal

        from app.coaching.context import lineage_key
        from app.optimization.conversations import active_observations
        from app.postcall.analysis import learning_observations

        if not 1 <= limit <= 20000:
            raise ValueError("Training snapshot bound must be between1 and20000 attempts")
        now = now or self.clock()
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
                    model.op_at <= now,
                )
                .order_by(model.op_at.desc(), model.id.desc())
                .limit(limit + 1)
            )
        )
        result, encoded_bytes = TrainingRows(), 2
        if len(selected) > limit:
            selected = selected[:limit]
            result.truncation = {"reason": "row_bound", "max_attempts": limit, "omitted_rows": None}
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
                call["observations"] = active_observations(observed, now)
                complete = call["id"] in verified and call["id"] not in incomplete
                call["cost_status"] = "settled" if complete else "uncertain"
                call["observed_cost_usd"] = float(costs.get(call["id"], 0)) if complete else None
            page = training_rows(
                calls,
                snapshots,
                events,
                now=now,
                goal=config["conversion_goal"],
                window_days=config["window_days"],
                include_unclassified=include_unclassified,
            )
            # Pages are newest first; within one, keep its newest rows that fit.
            page = sorted(page, key=lambda r: (r["at"], r["call_id"]), reverse=True)
            for kept, row in enumerate(page):
                size = len(json.dumps(row, allow_nan=False).encode()) + 1
                if encoded_bytes + size > TRAINING_SNAPSHOT_MAX_BYTES:
                    result.truncation = {
                        "reason": "byte_bound",
                        "max_bytes": TRAINING_SNAPSHOT_MAX_BYTES,
                        "omitted_rows": len(page) - kept,
                        "unread_attempts": max(0, len(selected) - offset - len(ids)),
                    }
                    result.sort(key=lambda r: (r["at"], r["call_id"]))
                    return result
                encoded_bytes += size
                result.append(row)
        result.sort(key=lambda r: (r["at"], r["call_id"]))
        return result

    def learning_report(self, p):
        """R8.4: settings, plain call counts, the dataset and number evidence, computed when asked."""
        from app.optimization.projections import call_counts, campaign_records

        with self.db.transaction(False) as s:
            s.info["row_cache"] = {}
            campaign = self.selected_campaign(s, p)
            config = self.optimization_config(s, p, campaign.id)
            records = campaign_records(s, p, campaign.id)
            rows = self.operational_dataset(s, p, config, include_unclassified=True)
            now = self.clock()
            return {
                "settings": config,
                "metrics": call_counts(
                    records["calls"],
                    goal=config["conversion_goal"],
                    window_days=config["window_days"],
                    now=now,
                ),
                "dataset": {
                    "rows": len(rows),
                    "pending_rows": sum(r["conversion"] is None for r in rows),
                    "matured_rows": sum(bool(r["window_closed"]) for r in rows),
                    "positive_conversions": sum(r["conversion"] == 1 for r in rows),
                    "snapshots": campaign.data.get("dataset_summaries", [])[-10:],
                },
                "numbers": self.inventory_evidence(s, p, records=records),
                "as_of": now.isoformat(),
            }

    def _check_rollback(self, s, p, rows):
        """R8.6: roll the live model back when its comparison group beats it; record why."""
        from app.optimization.learning import arm_comparison, rollback_due

        for model in self.scoped(s, "model_versions", p):
            promoted = timestamp(model.data.get("promoted_at"))
            if not model.data.get("active") or not promoted:
                continue
            comparison = arm_comparison(rows, promoted)
            if rollback_due(comparison):
                update(
                    model, active=False, rolled_back_at=self.clock().isoformat(), rollback_evidence=comparison
                )
                audit(
                    s,
                    p["tenant_id"],
                    p["seller_id"],
                    None,
                    "model.rolled_back",
                    model.id,
                    self.clock(),
                    {"comparison": {arm: list(value) for arm, value in comparison.items()}},
                )

    def save_operational_dataset(self, p, *, lease=None):
        """One consistent read, published as an immutable content-addressed dataset.

        The rows are as of the snapshot; a source that changes afterwards belongs to
        the next dataset. Nothing here fences on, or locks, the seller revision."""
        as_of = self.clock()
        with self.db.snapshot() as s:
            user = self.learning_owner(s, p)
            if user["role"] != "admin":
                from app.service import fail

                fail(403, "Administrator access required for a learning snapshot")
            watermark = self.finalized_calls(s, p["tenant_id"], p["seller_id"])
            config = self.optimization_config(s, p)
            campaign = self.selected_campaign(s, p)
            campaign_id = campaign.id
            rows = self.operational_dataset(s, p, config, now=as_of)
            value = {
                "campaign_id": campaign_id,
                "goal": config["conversion_goal"],
                "conversation_revision": campaign.data.get("conversation_revision", 0),
                "window_days": config["window_days"],
                "baseline": {k: config[k] for k in ("estimate_policy", "handling_seconds")},
                "rows": list(rows),
            }
        key = dataset_checksum(value)
        with self.db.transaction() as s:
            # Campaign row first, then job rows: every campaign publisher takes them in this order.
            campaign = self.required(s, "campaigns", campaign_id, p, lock=True)
            self.jobs.require_owned_in(s, lease)
            user = self.learning_owner(s, p)
            if user["role"] != "admin":
                from app.service import fail

                fail(403, "Learning owner no longer has administrator access")
            previous = self._by_key(s, p, "learning_datasets", key)
            result = previous or put(
                s,
                "learning_datasets",
                p["tenant_id"],
                p["seller_id"],
                {
                    **value,
                    "as_of": as_of.isoformat(),
                    "source_revision": watermark,
                    "truncation": getattr(rows, "truncation", None),
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
            self._check_rollback(s, p, result.data.get("rows", []))
            manifest = campaign.data.get("dataset_summaries", [])
            revised = sorted(
                [r for r in manifest if r["id"] != result.id] + [summary],
                key=lambda r: (r["as_of"], r["id"]),
            )[-10:]
            if manifest != revised:
                update(campaign, dataset_summaries=revised)
            dataset = doc(result)
        return dataset

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
        from app.ml.worker import trainer_unavailable

        if reason := trainer_unavailable():
            return {
                "status": "trainer_unavailable",
                "rows": len(data),
                "dataset_id": dataset["id"],
                "reason": reason,
            }
        # The job names the immutable dataset; the trainer loads and verifies its rows.
        job = self.jobs.enqueue(
            "ml",
            {
                "seller_id": p["seller_id"],
                "user_id": p["user_id"],
                "source_dataset_id": dataset["id"],
                "input_fingerprint": dataset["checksum"],
                "goal": dataset["goal"],
                "baseline": dataset["baseline"],
                "window_days": dataset["window_days"],
            },
            "operational-train:" + dataset["checksum"],
            tenant_id=p["tenant_id"],
        )
        return {"status": job["status"], "rows": len(data), "job_id": job["id"], "dataset_id": dataset["id"]}

    def refresh_observed_health(self, p, *, lease=None):
        from app.service import fail

        with self.db.transaction(False) as s:
            user = self.learning_owner(s, p)
            if user["role"] != "admin":
                fail(403, "Administrator access required for health refresh")
            numbers = self.inventory_evidence(s, p)
        # Health is a derived as-of view: each page publishes what was computed, and
        # a later source change is repaired by the next scheduled refresh.
        updated = 0
        for offset in range(0, len(numbers), 100):
            with self.db.transaction() as s:
                self.jobs.require_owned_in(s, lease)
                user = self.learning_owner(s, p)
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
        return {"status": "ready", "updated": updated}

    def refresh_learning(self, p, *, lease=None):
        """Explicit CPU-lane work; one bounded dataset for associations and optional training."""
        from app.optimization.conversations import conversation_associations

        dataset = self.save_operational_dataset(p, lease=lease)
        with self.db.transaction(False) as s:
            user = self.learning_owner(s, p)
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
        self.publish_script_learning(
            p, campaign_id, [r for r in dataset["rows"] if r.get("campaign_id") == campaign_id]
        )
        with self.db.transaction() as s:
            # Campaign row first, then job rows: every campaign publisher takes them in this order.
            campaign = self.required(s, "campaigns", campaign_id, p, lock=True)
            self.jobs.require_owned_in(s, lease)
            owner = self.learning_owner(s, p)
            if owner["role"] != "admin":
                from app.service import fail

                fail(403, "Learning owner no longer has administrator access")
            config = self.optimization_config(s, p, campaign_id)
            if not (
                dataset.get("conversation_revision") == campaign.data.get("conversation_revision", 0)
                and dataset["goal"] == config["conversion_goal"]
                and dataset["window_days"] == config["window_days"]
                and vocabulary["version"]
                == campaign.data.get("conversation_vocabulary", {"version": 1})["version"]
            ):
                return {"status": "superseded", "dataset_id": dataset["id"]}
            if campaign.data.get("conversation_report") != report:
                # No screen event for an unchanged report (section 8: no UI churn).
                update(campaign, conversation_report=report)
                emit(s, p["tenant_id"], "optimization.updated", campaign, self.clock())
            last = campaign.data.get("last_training") or {}
        labeled = sum(1 for row in dataset["rows"] if row.get("human") in (0, 1))
        if not config["auto_train"]:
            training = {"status": "disabled"}
        elif not training_due(last, labeled, self.clock()):
            training = {"status": "not_due", "labeled": labeled, "last": last}
        else:
            training = self.train_operational(p, dataset)
            if training.get("job_id"):
                with self.db.transaction() as s:
                    campaign = self.required(s, "campaigns", campaign_id, p, lock=True)
                    update(campaign, last_training={"at": self.clock().isoformat(), "labeled": labeled})
        return {
            "status": "ready",
            "dataset_id": dataset["id"],
            "rows": len(dataset["rows"]),
            "training": training,
        }

    def refresh_optimization(self, p, *, lease=None):
        from app.optimization.projections import campaign_records, estimate_statistics

        with self.db.transaction(False) as s:
            user = self.learning_owner(s, p)
            if user["role"] != "admin":
                from app.service import fail

                fail(403, "Administrator access required for optimization refresh")
            generation = self.finalized_calls(s, p["tenant_id"], p["seller_id"])
            campaign = self.selected_campaign(s, p)
            campaign_id = campaign.id
            config = self.optimization_config(s, p, campaign_id)
            rows = self.operational_dataset(s, p, config, include_unclassified=True)
            leads = campaign_records(s, p, campaign_id)["leads"]
            statistics = estimate_statistics(rows, leads, now=self.clock())
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
            # Campaign row first, then job rows: every campaign publisher takes them in this order.
            campaign = self.required(s, "campaigns", campaign_id, p, lock=True)
            user = self.learning_owner(s, p)
            if user["role"] != "admin":
                from app.service import fail

                fail(403, "Optimization owner no longer has administrator access")
            self.jobs.require_owned_in(s, lease)
            previous = {
                k: v for k, v in (campaign.data.get("selection_profile") or {}).items() if k != "as_of"
            }
            changed = previous != {k: v for k, v in profile.items() if k != "as_of"}
            finalized = self.finalized_calls(s, p["tenant_id"], p["seller_id"])
            # An unchanged refresh writes nothing: a campaign write would bump the report revision
            # and republish the same totals to the screen.
            if changed or campaign.data.get("optimization_refreshed_finalized") != finalized:
                update(campaign, selection_profile=profile, optimization_refreshed_finalized=finalized)
            jobs = []
            for kind in ("health_refresh", "learning_refresh"):
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
            if changed:  # No UI event unless the estimates moved (section 8).
                emit(s, p["tenant_id"], "optimization.updated", campaign, self.clock())
        return {
            "status": "ready",
            "rows": profile["samples"],
            "jobs": [{"id": j["id"], "kind": j["payload"]["kind"]} for j in jobs],
        }
