"""Replaceable source contributions for shared operational metrics.

Raw text never enters a vector. Lead uniqueness and rep-time union have their
own source units instead of being summed from individual call attempts.
"""

import hashlib
import hmac
import json
import math
import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from sqlalchemy import func, or_, select

from app.db import MODELS, Job, doc, put, uid, update
from app.operational_schema import call_work_bounds
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


def unit_key(definition, namespace, kind, identity):
    if (
        not re.fullmatch(r"[a-f0-9]{64}", definition)
        or not re.fullmatch(r"[a-zA-Z0-9-]{1,36}", namespace)
        or kind not in {"lead", "call", "rep-day", "usage", "help"}
        or not isinstance(identity, str)
        or not 1 <= len(identity) <= 128
    ):
        raise ValueError("Invalid scoped contribution identity")
    return f"v{definition}:{namespace}:{kind}:{identity}"


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
    known = [
        derive_labels(value["kind"])
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
        "held_meetings": int(original and any(row["meeting_held"] is True for row in outcomes + known)),
        "unique_conversions": int(original and any(row[label] is True for row in outcomes + known)),
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


def rep_day_values(intervals, calls, *, rep_id, day, now):
    from app.measurement import work_summary

    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    work = work_summary(
        [row for row in intervals if row.get("rep_id") == rep_id],
        [row for row in calls if row.get("rep_id") == rep_id],
        now=now,
        start_at=start,
        end_at=start + timedelta(days=1),
    )
    return {
        "rep_work_seconds": work["seconds"] or 0,
        "work_evidence_count": int(rep_id in work["by_rep"]),
        "stale_intervals": work["stale_intervals"],
    }


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


def delta_values(previous, current):
    """Atomic replacement subtracts the old vector exactly once, including zeros."""
    delta = {}
    for key in sorted(previous.keys() | current.keys()):
        old, new = previous.get(key, 0), current.get(key, 0)
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (old, new)
        ):
            raise ValueError("Contribution values must be finite numbers")
        if old != new:
            delta[key] = new - old
    return delta


def dirty_source_units(changed, session):
    """Source IDs only; the worker reads current authorized records after commit."""
    units = {}
    for row, previous in changed:
        table = row.__tablename__
        scope = (row.tenant_id, row.seller_id)
        deleted = row in session.deleted
        kind = {
            "leads": "lead",
            "call_attempts": "call",
            "usage_ledger": "usage",
            "coach_events": "help",
        }.get(table)
        if kind:
            units[(*scope, kind, row.id)] = deleted
        if table == "followup_tasks" and (row.data.get("inbound") or (previous or {}).get("inbound")):
            units[(*scope, "call", "callback:" + row.id)] = deleted
        if table == "campaigns" and any(
            row.data.get(key) != (previous or {}).get(key)
            for key in ("optimization_trial", "controlled_comparisons")
        ):
            units[(*scope, "campaign-leads", row.id)] = deleted
        if table == "outcomes" and row.data.get("kind") == "conversation_lineage":
            # A retraction need not repeat the old guidance link. Its stable
            # observation identity still invalidates that exposure contribution.
            model = MODELS["outcomes"]
            prior = session.scalars(
                select(model.data).where(
                    model.tenant_id == scope[0],
                    model.seller_id == scope[1],
                    model.ctx_key == row.data.get("call_id"),
                    model.ctx_status == "conversation_lineage",
                )
            )
            for value in prior:
                if value.get("id") == row.data.get("id") and value.get("observed_guidance_id"):
                    units[(*scope, "help", value["observed_guidance_id"])] = False
        for value in [row.data, *([previous] if previous else [])]:
            if table == "outcomes" and value.get("kind") == "attempt_work" and value.get("call_id"):
                units[(*scope, "call", value["call_id"])] = False
            if table in {
                "call_attempts",
                "conversion_events",
                "experiment_assignments",
                "feature_snapshots",
                "compliance_decisions",
            } and value.get("lead_id"):
                units[(*scope, "lead", value["lead_id"])] = False
            if table in {"conversion_events", "feature_snapshots"} and value.get("lead_id"):
                units[(*scope, "lead-calls", value["lead_id"])] = False
            if table == "suppressions" and value.get("phone"):
                database = session.info["database"]
                key = hmac.new(database.identity_key, value["phone"].encode(), hashlib.sha256).hexdigest()
                endpoints = MODELS["phone_endpoints"]
                endpoint = session.scalar(
                    select(endpoints.id).where(
                        endpoints.tenant_id == scope[0],
                        endpoints.seller_id == scope[1],
                        endpoints.external_key == key,
                    )
                )
                if endpoint:
                    units[(*scope, "endpoint-leads", endpoint)] = False
            if table == "coaching_feedback" and value.get("coach_event_id"):
                units[(*scope, "help", value["coach_event_id"])] = False
            if table == "outcomes" and value.get("observed_guidance_id"):
                units[(*scope, "help", value["observed_guidance_id"])] = False
            if table not in {"work_intervals", "call_attempts"} or not value.get("rep_id"):
                continue
            if table == "call_attempts":
                start, end = call_work_bounds(value)
                if not start and (value.get("submitted_at") or value.get("provider_id")):
                    start = timestamp(value.get("submitted_at") or value.get("started_at"))
                    end = start
            else:
                start = timestamp(value.get("started_at"))
                end = timestamp(value.get("ended_at") or value.get("lease_until"))
                # A heartbeat only changes the trailing span. Historical days were
                # already projected; an actual date correction still touches both ranges.
                if previous and all(
                    previous.get(k) == row.data.get(k) for k in ("rep_id", "started_at", "ended_at")
                ):
                    start = max(start, timestamp(previous.get("heartbeat_at")) or start) if start else None
            if start and end and end >= start:
                day = start.date()
                while day <= end.date():
                    units[(*scope, "rep-day", value["rep_id"] + ":" + day.isoformat())] = False
                    day += timedelta(days=1)
    return units


def enqueue_units(session, units):
    """Coalesce before_flush without nested flush or replacement of a running lease."""
    pending = {(job.tenant_id, job.key): job for job in session.new if isinstance(job, Job)}
    keys = {}
    for tenant, seller, kind, identity in units:
        key = f"projection:{seller}:{kind}:{identity}"
        if (tenant, key) not in pending:
            keys.setdefault(tenant, []).append(key)
    for tenant, identities in sorted(keys.items()):
        for offset in range(0, len(identities), 100):
            for job in session.scalars(
                select(Job)
                .where(Job.tenant_id == tenant, Job.key.in_(identities[offset : offset + 100]))
                .order_by(Job.key)
                .with_for_update()
            ):
                pending[tenant, job.key] = job
    for (tenant, seller, kind, identity), deleted in sorted(units.items()):
        key = f"projection:{seller}:{kind}:{identity}"
        job = pending.get((tenant, key))
        payload = {
            "kind": "metric_dependents"
            if kind in {"endpoint-leads", "lead-calls", "campaign-leads"}
            else "metric_refresh",
            "seller_id": seller,
            "source_kind": kind,
            "source_id": identity,
            "deleted": deleted,
            "desired_generation": (job.payload.get("desired_generation", 0) if job else 0) + 1,
            "claimed_generation": job.payload.get("claimed_generation", 0) if job else 0,
        }
        if not job:
            job = Job(
                id=uid(),
                tenant_id=tenant,
                seller_id=seller,
                owner_id="",
                queue="ml",
                key=key,
                payload=payload,
                status="queued",
                priority=30,
                attempts=0,
                max_attempts=3,
                available_at=datetime(1970, 1, 1, tzinfo=UTC),
            )
            session.add(job)
            pending[tenant, key] = job
        else:
            job.payload = payload
            job.available_at = datetime(1970, 1, 1, tzinfo=UTC)
            if job.status != "running":
                job.status, job.error, job.attempts = "queued", None, 0


def definition_version(config, epoch):
    return hashlib.sha256(
        json.dumps(
            {
                "metrics": "cohort-work-v13",
                "epoch": epoch,
                "goal": config["conversion_goal"],
                "window_days": config["window_days"],
                "health_policy": config.get("health_policy", {}),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _scoped_query(p, table):
    model = MODELS[table]
    return select(model).where(model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"])


def _unit_snapshot(service, p, payload):
    kind, identity = payload["source_kind"], payload["source_id"]
    with service.db.transaction(False) as session:
        campaign = session.scalar(
            _scoped_query(p, "campaigns").order_by(MODELS["campaigns"].created_at).limit(1)
        )
        config = service.optimization_config(session, p, campaign.id)
        definition = definition_version(
            config, campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
        )
        pointer = campaign.data.get("metric_projection", {})
        namespaces = [pointer.get("active_namespace", "initial")]
        if pointer.get("building_namespace"):
            namespaces.append(pointer["building_namespace"])
        if payload.get("target_namespace"):
            namespaces = [ns for ns in namespaces if ns == payload["target_namespace"]]
        targets = {}
        for ns in namespaces:
            prefix = "building" if ns == pointer.get("building_namespace") else "active"
            targets[ns] = {
                "config": pointer.get(prefix + "_config", config),
                "definition": pointer.get(prefix + "_definition", definition),
            }
        table = {
            "lead": "leads",
            "call": "call_attempts",
            "usage": "usage_ledger",
            "help": "coach_events",
        }.get(kind)
        callback_source = kind == "call" and identity.startswith("callback:")
        if callback_source:
            table = "followup_tasks"
        source = (
            session.scalar(
                _scoped_query(p, table).where(
                    MODELS[table].id == (identity[9:] if callback_source else identity)
                )
            )
            if table
            else None
        )
        row = doc(source) if source else {}
        if callback_source and not row.get("inbound"):
            row = {}
        calls, events, intervals, feedback, suppressed, feature = [], [], [], None, False, None
        work = None
        assignments, features, decisions, studies, observations = [], [], [], {}, []
        comparisons = [
            {k: v for k, v in item.items() if k != "population"}
            for item in campaign.data.get("controlled_comparisons", [])
        ]
        if kind == "lead" and row:
            calls = [
                doc(r)
                for r in session.scalars(
                    _scoped_query(p, "call_attempts").where(MODELS["call_attempts"].op_lead_id == identity)
                )
            ]
            events = [
                doc(r)
                for r in session.scalars(
                    _scoped_query(p, "conversion_events").where(
                        MODELS["conversion_events"].op_lead_id == identity
                    )
                )
            ]
            assignments = [
                doc(r)
                for r in session.scalars(
                    _scoped_query(p, "experiment_assignments").where(
                        MODELS["experiment_assignments"].op_lead_id == identity
                    )
                )
            ]
            features = (
                [
                    doc(r)
                    for r in session.scalars(
                        _scoped_query(p, "feature_snapshots").where(
                            MODELS["feature_snapshots"].id.in_([r.get("feature_snapshot_id") for r in calls])
                        )
                    )
                ]
                if calls
                else []
            )
            decisions = (
                [
                    doc(r)
                    for r in session.scalars(
                        _scoped_query(p, "compliance_decisions").where(
                            MODELS["compliance_decisions"].id.in_(
                                [r.get("submission_decision_id") for r in calls]
                            )
                        )
                    )
                ]
                if calls
                else []
            )
            for model_id in {a.get("trial_model_id") for a in assignments} - {None}:
                model = session.scalar(
                    _scoped_query(p, "model_versions").where(MODELS["model_versions"].id == model_id)
                )
                if model and model.data.get("trial_epoch"):
                    studies[model.data["trial_epoch"]] = model.data.get("trial_study", {})
            epoch = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
            studies[epoch] = campaign.data.get("optimization_trial") or {}
            phone = row.get("phone")
            if phone:
                key = hmac.new(service.db.identity_key, phone.encode(), hashlib.sha256).hexdigest()
                denial = service._by_key(session, p, "suppressions", key)
                suppressed = bool(denial and denial.data.get("active", True))
        elif kind == "call" and row and not callback_source:
            record = service._by_key(session, p, "outcomes", "attempt-work:" + row["id"])
            work = doc(record) if record else None
            if work:
                row.update({k: work[k] for k in ("rep_work_intervals", "rep_work_incomplete") if k in work})
            record = session.scalar(
                _scoped_query(p, "feature_snapshots").where(
                    MODELS["feature_snapshots"].id == row.get("feature_snapshot_id")
                )
            )
            feature = doc(record) if record else None
            events = [
                doc(r)
                for r in session.scalars(
                    _scoped_query(p, "conversion_events").where(
                        MODELS["conversion_events"].op_lead_id == row.get("lead_id")
                    )
                )
            ]
        elif kind == "rep-day":
            rep, day = identity.rsplit(":", 1)
            start = datetime.fromisoformat(day).replace(tzinfo=UTC)
            end = start + timedelta(days=1)
            for relation, end_field, target in [
                ("work_intervals", "op_due_at", intervals),
                ("call_attempts", "op_work_end_at", calls),
            ]:
                model = MODELS[relation]
                target.extend(
                    doc(r)
                    for r in session.scalars(
                        _scoped_query(p, relation).where(
                            model.op_owner_id == rep,
                            model.op_at < end,
                            getattr(model, end_field) >= start,
                        )
                    )
                )
        elif kind == "help" and row:
            call = session.scalar(
                _scoped_query(p, "call_attempts").where(MODELS["call_attempts"].id == row.get("call_id"))
            )
            rep = row.get("rep_id") or (call.data.get("rep_id") if call else None)
            if rep:
                row["rep_id"] = rep
                record = service._by_key(session, p, "coaching_feedback", identity + ":" + rep)
                feedback = doc(record) if record else None
            if call:
                calls = [doc(call)]
                assignments = [
                    doc(r)
                    for r in session.scalars(
                        _scoped_query(p, "experiment_assignments").where(
                            MODELS["experiment_assignments"].op_lead_id == call.data.get("lead_id")
                        )
                    )
                ]
                latest = {}
                for record in service.context_rows(
                    session,
                    p,
                    "outcomes",
                    call.id,
                    MODELS["outcomes"].ctx_status == "conversation_lineage",
                    limit=None,
                ):
                    value = record.data
                    if value["version"] >= latest.get(value["id"], {}).get("version", 0):
                        latest[value["id"]] = dict(value)
                observations = list(latest.values())
        previous = {
            ns: service._by_key(
                session, p, "metric_contributions", unit_key(targets[ns]["definition"], ns, kind, identity)
            )
            for ns in namespaces
        }
        previous = {ns: {"version": r.version, **dict(r.data)} if r else None for ns, r in previous.items()}
        job = session.scalar(
            select(Job).where(
                Job.tenant_id == p["tenant_id"], Job.key == f"projection:{p['seller_id']}:{kind}:{identity}"
            )
        )
        generation = job.payload["desired_generation"] if job else 0
    return {
        "kind": kind,
        "identity": identity,
        "row": row,
        "calls": calls,
        "events": events,
        "intervals": intervals,
        "feedback": feedback,
        "feature": feature,
        "assignments": assignments,
        "features": features,
        "decisions": decisions,
        "studies": studies,
        "comparisons": comparisons,
        "observations": observations,
        "suppressed": suppressed,
        "campaign_id": campaign.id,
        "config": config,
        "definition": definition,
        "namespaces": namespaces,
        "targets": targets,
        "previous": previous,
        "generation": generation,
        "pointer": dict(pointer),
        "input_version_fingerprint": hashlib.sha256(
            json.dumps(
                [
                    sorted(
                        (r.get("id"), r.get("version"))
                        for r in [
                            row,
                            *calls,
                            *events,
                            *intervals,
                            *assignments,
                            *features,
                            *decisions,
                            feedback or {},
                            feature or {},
                            work or {},
                        ]
                        if r.get("id")
                    ),
                    definition,
                    suppressed,
                    studies,
                    comparisons,
                    observations,
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest(),
    }


def _calculate_unit(snapshot, now):
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
        if at := timestamp(row.get("started_at")):
            boundary = at + timedelta(days=config["window_days"])
            if boundary > now:
                refresh.append(boundary)
        start = timestamp(
            row.get("submitted_at") or (row.get("started_at") if row.get("provider_id") else None)
        )
        refresh.extend(
            t
            for t in (start, start + timedelta(days=config["window_days"]) if start else None)
            if t and t > now
        )
        if start:
            refresh.extend(
                boundary
                for days in (1, 28)
                if (boundary := start + timedelta(days=days, microseconds=1)) > now
            )
        if at := timestamp(row.get("started_at")):
            refresh.extend(
                boundary
                for boundary in (
                    at + timedelta(days=7, microseconds=1),
                    at + timedelta(days=28, microseconds=1),
                )
                if boundary > now
            )
    elif kind == "usage" and row:
        values = usage_values(row)
    elif kind == "help" and row:
        values = help_values(row, snapshot["feedback"], now=now)
    elif kind == "rep-day":
        rep, day = snapshot["identity"].rsplit(":", 1)
        values = rep_day_values(
            snapshot["intervals"],
            snapshot["calls"],
            rep_id=rep,
            day=datetime.fromisoformat(day).date(),
            now=now,
        )
        end = datetime.fromisoformat(day).replace(tzinfo=UTC) + timedelta(days=1)
        bounds = [
            (timestamp(i.get("started_at")), timestamp(i.get("ended_at") or i.get("lease_until")))
            for i in snapshot["intervals"]
        ]
        bounds.extend(call_work_bounds(c) for c in snapshot["calls"])
        for start_at, end_at in bounds:
            if start_at and end_at and now < end_at and now < end:
                refresh.append(min(end, start_at if start_at > now else now + timedelta(seconds=60), end_at))
    else:
        values = {}
    if kind == "call" and row:
        at = timestamp(row.get("occurred_at") or row.get("started_at") or row.get("created_at"))
        if at:
            refresh.extend(
                t
                for t in (at, at + timedelta(days=config["health_policy"]["window_days"], microseconds=1))
                if t > now
            )
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
    return values, min(refresh).isoformat() if refresh else None


def _groups(snapshot, namespace):
    groups = [{"bucket_kind": "total", "bucket_day": None}]
    kind, row = snapshot["kind"], snapshot["row"]
    at = timestamp(row.get("at") or row.get("submitted_at") or row.get("started_at"))
    day = (
        snapshot["identity"].rsplit(":", 1)[1] if kind == "rep-day" else at.date().isoformat() if at else None
    )
    if kind != "lead" and day:
        groups.append({"bucket_kind": "daily", "bucket_day": day})
    if kind == "rep-day":
        groups.append(
            {"bucket_kind": "rep_total", "bucket_day": None, "rep_id": snapshot["identity"].rsplit(":", 1)[0]}
        )
        groups.append(
            {"bucket_kind": "rep", "bucket_day": day, "rep_id": snapshot["identity"].rsplit(":", 1)[0]}
        )
    elif kind == "help" and row.get("rep_id"):
        groups.append({"bucket_kind": "rep_total", "bucket_day": None, "rep_id": row["rep_id"]})
    return [
        {
            **g,
            "namespace": namespace,
            "campaign_id": snapshot["campaign_id"],
            "definition_version": snapshot["definition"],
        }
        for g in groups
    ]


def _group_key(service, group):
    digest = hmac.new(
        service.db.identity_key,
        json.dumps(group, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"v{group['definition_version']}:{group['namespace']}:{digest}"


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


def _apply_bucket_delta(service, session, p, group, delta, fields=()):
    key = _group_key(service, group)
    model = MODELS["metric_buckets"]
    bucket = session.scalar(
        _scoped_query(p, "metric_buckets").where(model.external_key == key).with_for_update()
    )
    previous = bucket.data.get("values", {}) if bucket else {}
    total = {**dict.fromkeys(fields, 0), **previous, **{k: previous.get(k, 0) + v for k, v in delta.items()}}
    value = {**group, "group": group, "group_key": key.rsplit(":", 1)[-1], "values": total}
    kind = group["bucket_kind"]
    empty = (
        (kind == "drift" and total.get("rows", 0) == 0)
        or (kind == "trial" and total.get("control:leads", 0) + total.get("candidate:leads", 0) == 0)
        or (
            kind
            in {
                "comparison",
                "number_health",
                "number_health_stratum",
                "call_cost",
                "estimate",
                "retry",
                "treatment",
            }
            and not any(total.values())
        )
    )
    if empty:
        if bucket:
            session.delete(bucket)
    elif bucket:
        update(bucket, **value)
    else:
        put(session, "metric_buckets", p["tenant_id"], p["seller_id"], value, key=key, now=service.clock())
    return previous, total


def refresh_unit(service, p, payload):
    """Compute outside writes, then replace one source vector under a generation fence."""
    if payload["source_kind"] in {"endpoint-leads", "lead-calls", "campaign-leads"}:
        return expand_dependents(service, p, payload)
    snapshot = _unit_snapshot(service, p, payload)
    if not snapshot["namespaces"]:
        return {"status": "superseded"}
    calculated = {
        ns: (
            *_calculate_unit({**snapshot, **target}, service.clock()),
            _detail_groups({**snapshot, **target}, service.clock(), ns),
        )
        for ns, target in snapshot["targets"].items()
    }
    kind, identity = snapshot["kind"], snapshot["identity"]
    with service.db.transaction() as session:
        # Match originating writes' short revision -> job -> data lock order.
        # This is the existing seller watermark row, never an advisory/global lock.
        service.report_revision(session, p, lock=True)
        if rebuild_lease := payload.get("rebuild_lease"):
            owner = session.get(Job, rebuild_lease[0], with_for_update=True)
            if not _rebuild_owned(owner, rebuild_lease, service.clock()):
                return {"status": "lost_lease"}
        job = session.scalar(
            select(Job)
            .where(
                Job.tenant_id == p["tenant_id"], Job.key == f"projection:{p['seller_id']}:{kind}:{identity}"
            )
            .with_for_update()
        )
        if payload.get("job_id"):
            from app.jobs.queue import aware

            if (
                not job
                or job.id != payload["job_id"]
                or job.status != "running"
                or job.lease_owner != payload.get("lease_owner")
                or not job.lease_expires_at
                or aware(job.lease_expires_at) <= service.clock()
            ):
                return {"status": "lost_lease"}
        if job and job.payload["desired_generation"] != snapshot["generation"]:
            return {"status": "superseded"}
        campaign = session.scalar(
            _scoped_query(p, "campaigns")
            .where(MODELS["campaigns"].id == snapshot["campaign_id"])
            .with_for_update()
        )
        if not campaign or campaign.data.get("metric_projection", {}) != snapshot["pointer"]:
            return {"status": "superseded"}
        current_definition = definition_version(
            {**snapshot["config"], **campaign.data.get("optimization", {})},
            campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2"),
        )
        if current_definition != snapshot["definition"]:
            return {"status": "superseded"}
        current = {}
        for namespace in snapshot["namespaces"]:
            key = unit_key(snapshot["targets"][namespace]["definition"], namespace, kind, identity)
            model = MODELS["metric_contributions"]
            previous = session.scalar(
                _scoped_query(p, "metric_contributions").where(model.external_key == key).with_for_update()
            )
            expected = snapshot["previous"][namespace]
            if (previous.version if previous else None) != (expected["version"] if expected else None):
                return {"status": "superseded"}
            current[namespace] = previous
        changed = False
        for namespace, previous in current.items():
            target = snapshot["targets"][namespace]
            values, due, details = calculated[namespace]
            details = details + _call_cost_group({**snapshot, **target}, values, namespace)
            key = unit_key(target["definition"], namespace, kind, identity)
            saved = previous.data if previous else {}
            groups = (_groups({**snapshot, **target}, namespace) if values else []) + [g for g, _ in details]
            group_values = {_group_key(service, g): v for g, v in details}
            fingerprint = hashlib.sha256(
                json.dumps(
                    [
                        values,
                        groups,
                        group_values,
                        due,
                        snapshot["generation"],
                        snapshot["input_version_fingerprint"],
                    ],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            if saved.get("input_fingerprint") == fingerprint:
                continue
            old_groups = {_group_key(service, g): g for g in saved.get("groups", [])}
            new_groups = {_group_key(service, g): g for g in groups}
            cost_delta = {}
            for group_key in sorted(old_groups.keys() | new_groups.keys()):
                delta = delta_values(
                    saved.get("group_values", {}).get(group_key, saved.get("values", {}))
                    if group_key in old_groups
                    else {},
                    group_values.get(group_key, values) if group_key in new_groups else {},
                )
                if not delta:
                    continue
                group = new_groups.get(group_key) or old_groups[group_key]
                before, after = _apply_bucket_delta(
                    service, session, p, group, delta, group_values.get(group_key, values)
                )
                if group["bucket_kind"] == "call_cost":
                    for name, change in delta_values(
                        observed_call_cost(before), observed_call_cost(after)
                    ).items():
                        cost_delta[name] = cost_delta.get(name, 0) + change
            if cost_delta:
                # Same short seller watermark fence as the source replacement.
                # Receipt and eligibility changes update the total atomically,
                # without reading the call's ledger history or another job.
                total_group = _groups({**snapshot, **target}, namespace)[0]
                _apply_bucket_delta(service, session, p, total_group, cost_delta)
            value = {
                "source_kind": kind,
                "source_id": identity,
                "source_version": snapshot["row"].get("version", snapshot["generation"]),
                "campaign_id": snapshot["campaign_id"],
                "definition_version": target["definition"],
                "namespace": namespace,
                "input_fingerprint": fingerprint,
                "input_version_fingerprint": snapshot["input_version_fingerprint"],
                "dirty_generation": snapshot["generation"],
                "values": values,
                "group_values": group_values,
                "groups": groups,
                "next_refresh_at": due,
                "as_of": service.clock().isoformat(),
            }
            if previous:
                update(previous, **value)
            else:
                put(
                    session,
                    "metric_contributions",
                    p["tenant_id"],
                    p["seller_id"],
                    value,
                    key=key,
                    now=service.clock(),
                )
            changed = True
        if not snapshot["pointer"]:
            campaign.data = {
                **campaign.data,
                "metric_projection": {
                    "active_namespace": "initial",
                    "active_definition": snapshot["definition"],
                    "active_config": snapshot["config"],
                },
            }
        return {
            "status": "ready" if changed else "unchanged",
            "planning": {"campaign_id": snapshot["row"]["campaign_id"], "lead_id": identity}
            if kind == "lead" and snapshot["row"].get("campaign_id")
            else None,
        }


def refresh_due(service, p, *, limit=100):
    if not 1 <= limit <= 100:
        raise ValueError("At most100 due contributions per sweep")
    with service.db.transaction(False) as session:
        model = MODELS["metric_contributions"]
        campaign = session.scalar(
            _scoped_query(p, "campaigns").order_by(MODELS["campaigns"].created_at).limit(1)
        )
        pointer = campaign.data.get("metric_projection", {})
        prefixes = [
            f"v{pointer[k + '_definition']}:{pointer[k + '_namespace']}:"
            for k in ("active", "building")
            if pointer.get(k + "_definition")
        ]
        if not prefixes:
            return {"processed": 0}
        sources = list(
            session.execute(
                select(model.source_kind, model.source_id)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.next_refresh_at <= service.clock(),
                    or_(
                        *((model.external_key >= key) & (model.external_key < key + "~") for key in prefixes)
                    ),
                )
                .order_by(model.next_refresh_at, model.id)
                .limit(limit)
            )
        )
    for kind, identity in dict.fromkeys(sources):
        refresh_unit(service, p, {"source_kind": kind, "source_id": identity})
    return {"processed": len(sources)}


def expand_dependents(service, p, payload):
    """A saved100-ID page replaces unbounded work inside a denial/correction write."""
    kind, identity = payload["source_kind"], payload["source_id"]
    table, field, target_kind = {
        "endpoint-leads": ("leads", "op_endpoint_id", "lead"),
        "lead-calls": ("call_attempts", "op_lead_id", "call"),
        "campaign-leads": ("leads", "op_campaign_id", "lead"),
    }[kind]
    key = f"projection:{p['seller_id']}:{kind}:{identity}"
    with service.db.transaction(False) as session:
        job = session.scalar(select(Job).where(Job.tenant_id == p["tenant_id"], Job.key == key))
        if not job:
            return {"status": "superseded", "processed": 0}
        generation = job.payload["desired_generation"]
        cursor = job.payload.get("cursor", "") if job.payload.get("cursor_generation") == generation else ""
        if job.payload.get("completed_generation") == generation:
            return {"status": "ready", "processed": 0}
        model = MODELS[table]
        ids = list(
            session.scalars(
                select(model.id)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    getattr(model, field) == identity,
                    model.id > cursor,
                )
                .order_by(model.id)
                .limit(100)
            )
        )
    with service.db.transaction() as session:
        # Source writers acquire these same keys in order. Parent-first would
        # invert that order for lead-calls expansion and a concurrent correction.
        locked = {
            j.key: j
            for j in session.scalars(
                select(Job)
                .where(
                    Job.tenant_id == p["tenant_id"],
                    Job.key.in_([key, *(f"projection:{p['seller_id']}:{target_kind}:{i}" for i in ids)]),
                )
                .order_by(Job.key)
                .with_for_update()
            )
        }
        job = locked.get(key)
        if not job or job.payload["desired_generation"] != generation:
            return {"status": "superseded", "processed": 0}
        if payload.get("job_id"):
            from app.jobs.queue import aware

            if (
                job.id != payload["job_id"]
                or job.status != "running"
                or job.lease_owner != payload.get("lease_owner")
                or not job.lease_expires_at
                or aware(job.lease_expires_at) <= service.clock()
            ):
                return {"status": "lost_lease", "processed": 0}
        current_cursor = (
            job.payload.get("cursor", "") if job.payload.get("cursor_generation") == generation else ""
        )
        if current_cursor != cursor:
            return {"status": "superseded", "processed": 0}
        enqueue_units(session, {(p["tenant_id"], p["seller_id"], target_kind, i): False for i in ids})
        job.payload = {
            **job.payload,
            "cursor_generation": generation,
            "cursor": ids[-1] if ids else cursor,
            "completed_generation": generation if len(ids) < 100 else None,
        }
    return {"status": "ready" if len(ids) < 100 else "pending", "processed": len(ids)}


# Rebuild is explicit recovery work. Normal corrections use only dirty units.
_REBUILD_SOURCES = (
    ("leads", "lead"),
    ("call_attempts", "call"),
    ("usage_ledger", "usage"),
    ("coach_events", "help"),
    ("work_intervals", "rep-day"),
    ("call_attempts", "rep-day"),
    ("followup_tasks", "call"),
)


def _rebuild_control(service, p, *, start, lease=None):
    with service.db.transaction() as session:
        watermark = service.report_revision(session, p, lock=True)
        campaign = session.scalar(
            _scoped_query(p, "campaigns").order_by(MODELS["campaigns"].created_at).limit(1).with_for_update()
        )
        config = service.optimization_config(session, p, campaign.id)
        epoch = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
        definition = definition_version(config, epoch)
        key = f"metric-rebuild:{p['seller_id']}:{campaign.id}"
        job = session.scalar(
            select(Job).where(Job.tenant_id == p["tenant_id"], Job.key == key).with_for_update()
        )
        state = dict(job.payload) if job else {}
        if lease and not _rebuild_owned(job, lease, service.clock()):
            return state, "lost_lease"
        pointer = dict(campaign.data.get("metric_projection", {}))
        if start and (not state or state.get("state") != "building" or state["definition"] != definition):
            namespace = uid()
            state = {
                "kind": "metric_rebuild",
                "seller_id": p["seller_id"],
                "campaign_id": campaign.id,
                "user_id": p.get("user_id"),
                "role": p.get("role"),
                "namespace": namespace,
                "definition": definition,
                "epoch": epoch,
                "config": config,
                "state": "building",
                "cursor": {"phase": 0, "after_id": "", "after_at": None, "day": None},
                "source_watermark": watermark,
                "processed": 0,
                "desired_generation": state.get("desired_generation", 0) + 1,
                "claimed_generation": state.get("claimed_generation", 0),
            }
            pointer = {
                **pointer,
                "active_namespace": pointer.get("active_namespace", "initial"),
                "active_definition": pointer.get("active_definition", definition),
                "active_config": pointer.get("active_config", config),
                "building_namespace": namespace,
                "building_definition": definition,
                "building_config": config,
            }
            campaign.data = {**campaign.data, "metric_projection": pointer}
            group = {
                "bucket_kind": "total",
                "bucket_day": None,
                "campaign_id": campaign.id,
                "definition_version": definition,
                "namespace": namespace,
            }
            group_key = _group_key(service, group)
            put(
                session,
                "metric_buckets",
                p["tenant_id"],
                p["seller_id"],
                {
                    **group,
                    "group": group,
                    "group_key": group_key.rsplit(":", 1)[1],
                    "values": {"original_cohort": 0},
                    "as_of": service.clock().isoformat(),
                },
                key=group_key,
                now=service.clock(),
            )
            if job:
                job.payload = state
                if job.status != "running":
                    job.status, job.attempts, job.error = "queued", 0, None
                job.available_at = service.clock()
            else:
                job = Job(
                    id=uid(),
                    tenant_id=p["tenant_id"],
                    seller_id=p["seller_id"],
                    queue="ml",
                    key=key,
                    payload=state,
                    priority=20,
                    attempts=0,
                    max_attempts=3,
                    status="queued",
                    available_at=service.clock(),
                )
                session.add(job)
        return state, definition


def _rebuild_source_page(service, p, state, lease=None):
    """At most500 indexed source identities; work spans resume within a row."""
    from sqlalchemy import and_, or_

    cursor = dict(state["cursor"])
    table, kind = _REBUILD_SOURCES[cursor["phase"]]
    model = MODELS[table]
    with service.db.transaction(False) as session:
        fields = [model.id, model.created_at]
        if kind == "rep-day":
            end = model.op_due_at if table == "work_intervals" else model.op_work_end_at
            fields.extend([model.op_owner_id, model.op_at, end])
        query = select(*fields).where(model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"])
        if cursor["after_at"]:
            at = timestamp(cursor["after_at"])
            query = query.where(
                or_(model.created_at > at, and_(model.created_at == at, model.id > cursor["after_id"]))
            )
        rows = list(session.execute(query.order_by(model.created_at, model.id).limit(500)))
    processed = 0
    for row in rows:
        if kind == "rep-day":
            rep, begin, end = row[2:]
            day = timestamp(cursor["day"]).date() if cursor.get("day") else begin.date() if begin else None
            while rep and day and end and day <= end.date():
                if processed == 500:
                    cursor["day"] = day.isoformat()
                    return cursor, processed
                result = refresh_unit(
                    service,
                    p,
                    {
                        "source_kind": "rep-day",
                        "source_id": rep + ":" + day.isoformat(),
                        "target_namespace": state["namespace"],
                        "rebuild_lease": lease,
                    },
                )
                if result["status"] not in {"ready", "unchanged"}:
                    return state["cursor"], processed
                processed += 1
                day += timedelta(days=1)
        else:
            result = refresh_unit(
                service,
                p,
                {
                    "source_kind": kind,
                    "source_id": "callback:" + row.id if table == "followup_tasks" else row.id,
                    "target_namespace": state["namespace"],
                    "rebuild_lease": lease,
                },
            )
            if result["status"] not in {"ready", "unchanged"}:
                return state["cursor"], processed
            processed += 1
        cursor.update(after_id=row.id, after_at=row.created_at.isoformat(), day=None)
        if processed == 500:
            return cursor, processed
    if len(rows) < 500:
        cursor = {"phase": cursor["phase"] + 1, "after_id": "", "after_at": None, "day": None}
    return cursor, processed


def _rebuild_catchup(service, p):
    """Use existing leases and completion, never run arbitrary ML/paid jobs."""
    worker = "projection-replay-" + uid()
    processed = 0
    for _ in range(100):
        job = service.jobs.claim(
            "ml", worker, tenant_id=p["tenant_id"], key_prefix=f"projection:{p['seller_id']}:"
        )
        if not job:
            break
        try:
            result = refresh_unit(service, p, {**job["payload"], "job_id": job["id"], "lease_owner": worker})
            if result["status"] == "lost_lease":
                break
            if result["status"] in {"pending", "superseded"}:
                service.jobs.coalesce_refresh(job["id"], worker)
            else:
                service.jobs.complete(job["id"], worker)
            processed += 1
        except Exception as exc:
            service.jobs.fail(job["id"], worker, type(exc).__name__)
            raise
    return processed


def _rebuild_owned(job, lease, now):
    from app.jobs.queue import aware

    return bool(
        job
        and job.id == lease[0]
        and job.status == "running"
        and job.lease_owner == lease[1]
        and job.lease_expires_at
        and aware(job.lease_expires_at) > now
    )


def rebuild_page(service, p, *, start=False, lease=None):
    """Resume one inactive page, then drain original dirty jobs before activation."""
    state, definition = _rebuild_control(service, p, start=start, lease=lease)
    empty = {
        "processed": 0,
        "published": False,
        "cursor": state.get("cursor"),
        "source_watermark": state.get("source_watermark"),
    }
    if not state:
        return {**empty, "status": "not_started"}
    if definition == "lost_lease":
        return {**empty, "status": "lost_lease"}
    if state["definition"] != definition:
        return {**empty, "status": "superseded"}
    if state["state"] == "ready":
        return {**empty, "published": True, "status": "ready"}
    cursor = state["cursor"]
    scanning = cursor["phase"] < len(_REBUILD_SOURCES)
    if scanning:
        next_cursor, processed = _rebuild_source_page(service, p, state, lease)
    else:
        next_cursor, processed = cursor, _rebuild_catchup(service, p)
    result = {**empty, "processed": processed, "cursor": next_cursor, "status": "pending"}
    with service.db.transaction() as session:
        watermark = service.report_revision(session, p, lock=True)
        job = session.scalar(
            select(Job)
            .where(
                Job.tenant_id == p["tenant_id"],
                Job.key == f"metric-rebuild:{p['seller_id']}:{state['campaign_id']}",
            )
            .with_for_update()
        )
        if lease and not _rebuild_owned(job, lease, service.clock()):
            return {**result, "status": "lost_lease"}
        campaign = session.scalar(
            _scoped_query(p, "campaigns")
            .where(MODELS["campaigns"].id == state["campaign_id"])
            .with_for_update()
        )
        pointer = dict(campaign.data.get("metric_projection", {}))
        current = definition_version(
            service.optimization_config(session, p, campaign.id),
            campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2"),
        )
        if (
            not job
            or job.payload.get("namespace") != state["namespace"]
            or job.payload["cursor"] != cursor
            or pointer.get("building_namespace") != state["namespace"]
            or current != definition
        ):
            return {**result, "status": "superseded"}
        state = {**state, "cursor": next_cursor, "processed": state["processed"] + processed}
        if not scanning:
            pending = session.scalar(
                select(Job.status)
                .where(
                    Job.tenant_id == p["tenant_id"],
                    Job.key.startswith(f"projection:{p['seller_id']}:", autoescape=True),
                    Job.status.in_(["queued", "running", "dead"]),
                )
                .order_by(Job.status)
                .limit(1)
            )
            if pending:
                job.payload = state
                return {**result, "status": "blocked" if pending == "dead" else "pending"}
            retired = list(pointer.get("retired", []))
            retired.append(
                {"namespace": pointer["active_namespace"], "definition": pointer["active_definition"]}
            )
            pointer = {k: v for k, v in pointer.items() if not k.startswith("building_")}
            pointer.update(
                active_namespace=state["namespace"],
                active_definition=definition,
                active_config=state["config"],
                source_watermark=watermark,
                as_of=service.clock().isoformat(),
                retired=retired,
            )
            campaign.data = {**campaign.data, "metric_projection": pointer}
            state.update(state="ready", source_watermark=watermark)
            for retired_namespace in retired:
                if retired_namespace["definition"] != definition:
                    continue  # Keep frozen historical definitions; retire only redundant copies.
                for table in ("metric_contributions", "metric_buckets"):
                    self_key = f"metric-cleanup:{p['seller_id']}:{definition}:{retired_namespace['namespace']}:{table}"
                    service.jobs.enqueue_in(
                        session,
                        "ml",
                        {
                            "kind": "metric_cleanup",
                            "seller_id": p["seller_id"],
                            "campaign_id": campaign.id,
                            **retired_namespace,
                            "table": table,
                        },
                        self_key,
                        tenant_id=p["tenant_id"],
                        priority=1,
                    )
            if job.status != "running":
                job.status = "completed"
            result.update(published=True, status="ready", source_watermark=watermark)
        job.payload = state
    return result


def _total_in(service, session, p, *, rep_only=False):
    campaign = session.scalar(_scoped_query(p, "campaigns").order_by(MODELS["campaigns"].created_at).limit(1))
    pointer = campaign.data.get("metric_projection", {})
    config = pointer.get("active_config") or service.optimization_config(session, p, campaign.id)
    group = {
        "bucket_kind": "rep_total" if rep_only else "total",
        "bucket_day": None,
        "campaign_id": campaign.id,
        "definition_version": pointer.get("active_definition")
        or definition_version(
            config, campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
        ),
        "namespace": pointer.get("active_namespace", "initial"),
        **({"rep_id": p["user_id"]} if rep_only else {}),
    }
    bucket = service._by_key(session, p, "metric_buckets", _group_key(service, group))
    return config, group, dict(bucket.data["values"]) if bucket else {}


def read_coaching_metrics(service, p):
    with service.db.transaction(False) as session:
        _, _, values = _total_in(service, session, p, rep_only=p.get("role") == "rep")
    return coaching_from_counts(values)


def read_dataset_metrics(service, p):
    with service.db.transaction(False) as session:
        _, _, values = _total_in(service, session, p)
        campaign = session.scalar(
            _scoped_query(p, "campaigns").order_by(MODELS["campaigns"].created_at).limit(1)
        )
        saved = campaign.data.get("dataset_summaries", [])[-10:]
    return {
        **{
            key: values.get("dataset_" + key, 0)
            for key in (
                "rows",
                "pending_rows",
                "matured_rows",
                "positive_conversions",
                "missing_snapshot_calls",
            )
        },
        "snapshots": saved,
    }


def read_estimate_statistics(service, p):
    """Read bounded conditional cells with exact pooled support; never partial local rates."""
    with service.db.transaction(False) as session:
        _, group, values = _total_in(service, session, p)
        prefix = f"v{group['definition_version']}:{group['namespace']}:"
        model = MODELS["metric_buckets"]
        results = {}
        for kind in ("estimate", "retry", "treatment"):
            results[kind] = [
                dict(row.data)
                for row in session.scalars(
                    _scoped_query(p, "metric_buckets")
                    .where(
                        model.external_key >= prefix,
                        model.external_key < prefix + "~",
                        model.bucket_kind == kind,
                    )
                    .order_by(model.external_key)
                    .limit(1001)
                )
            ]
    cells, retry = results["estimate"], results["retry"]
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
            for row in results["treatment"]
        }
        if len(results["treatment"]) <= 1000
        else {},
        "treatments_complete": len(results["treatment"]) <= 1000,
        "total": {k.removeprefix("estimate:"): v for k, v in values.items() if k.startswith("estimate:")},
        "cells": [{"features": row["group"]["features"], "counts": row["values"]} for row in cells[:1000]],
        "cells_complete": len(cells) <= 1000,
        "retry_observations": [
            {
                **{k: row["group"][k] for k in ("segment", "attempt_index")},
                **row["values"],
                "matured": True,
                "available_at": service.clock().isoformat(),
            }
            for row in retry
        ]
        if len(retry) <= 1000
        else [],
        "retry_complete": len(retry) <= 1000,
    }


def read_drift_metrics(service, p, *, limit=1000):
    if not 1 <= limit <= 1000:
        raise ValueError("At most1000 drift strata can be read per report")
    with service.db.transaction(False) as session:
        _, group, _ = _total_in(service, session, p)
        prefix = f"v{group['definition_version']}:{group['namespace']}:"
        model = MODELS["metric_buckets"]
        buckets = list(
            session.scalars(
                _scoped_query(p, "metric_buckets")
                .where(
                    model.external_key >= prefix,
                    model.external_key < prefix + "~",
                    model.bucket_kind == "drift",
                )
                .order_by(model.external_key)
                .limit(limit + 1)
            )
        )
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
            "as_of": service.clock().isoformat(),
            "reason": "More comparable strata than the bounded report can inspect; no monitoring conclusion published",
            "inspected_limit": limit,
        }
    periods = {"reference": {}, "recent": {}}
    for bucket in buckets:
        group = bucket.data["group"]
        key = tuple(group["stratum"]) if group["stratum"] is not None else None
        periods[group["period"]][key] = bucket.data["values"]
    return {
        **drift_from_counts(periods["reference"], periods["recent"]),
        "complete": True,
        "as_of": service.clock().isoformat(),
        "windows": "Latest 7 days versus preceding 21 days",
    }


def read_number_health_metrics(service, p):
    from app.optimization.numbering import health_from_counts

    with service.db.transaction(False) as session:
        config, group, _ = _total_in(service, session, p)
        prefix = f"v{group['definition_version']}:{group['namespace']}:"
        model = MODELS["metric_buckets"]
        query = _scoped_query(p, "metric_buckets").where(
            model.external_key >= prefix, model.external_key < prefix + "~"
        )
        totals = [
            dict(r.data)
            for r in session.scalars(
                query.where(model.bucket_kind == "number_health").order_by(model.external_key).limit(1001)
            )
        ]
        strata = [
            dict(r.data)
            for r in session.scalars(
                query.where(model.bucket_kind == "number_health_stratum")
                .order_by(model.external_key)
                .limit(1001)
            )
        ]
        contributions = MODELS["metric_contributions"]
        expiry = session.scalar(
            select(func.min(contributions.next_refresh_at)).where(
                contributions.tenant_id == p["tenant_id"],
                contributions.seller_id == p["seller_id"],
                contributions.external_key >= prefix,
                contributions.external_key < prefix + "~",
            )
        )
    if len(totals) > 1000:
        raise ValueError("Number inventory exceeds the bounded health report; no partial inventory published")
    distinct = {}
    for row in strata:
        identity = row["group"]["number_id"]
        distinct[identity] = distinct.get(identity, 0) + 1
    return {
        row["group"]["number_id"]: health_from_counts(
            row["values"],
            now=service.clock(),
            policy=config["health_policy"],
            comparison_strata=distinct.get(row["group"]["number_id"], 0) if len(strata) <= 1000 else None,
            next_expiry_at=timestamp(expiry).isoformat() if expiry else None,
        )
        for row in totals
    }


def read_comparison_metrics(service, p):
    from app.optimization.experiments import comparison_from_counts

    with service.db.transaction(False) as session:
        _, group, _ = _total_in(service, session, p)
        campaign = session.scalar(
            _scoped_query(p, "campaigns").where(MODELS["campaigns"].id == group["campaign_id"])
        )
        studies = campaign.data.get("controlled_comparisons", [])
        prefix = f"v{group['definition_version']}:{group['namespace']}:"
        model = MODELS["metric_buckets"]
        buckets = [
            dict(r.data)
            for r in session.scalars(
                _scoped_query(p, "metric_buckets")
                .where(
                    model.external_key >= prefix,
                    model.external_key < prefix + "~",
                    model.bucket_kind == "comparison",
                )
                .order_by(model.external_key)
                .limit(1001)
            )
        ]
    if len(buckets) > 1000:
        raise ValueError("Comparison strata exceed the bounded report; no partial inference is published")
    counts, strata, private = {}, {}, {}
    for bucket in buckets:
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
            study, counts.get(study["id"], {}), strata.get(study["id"], {}), now=service.clock()
        )
        for study in studies
    ]


def read_trial_metrics(service, p, *, epoch=None):
    with service.db.transaction(False) as session:
        config, group, _ = _total_in(service, session, p)
        campaign = session.scalar(
            _scoped_query(p, "campaigns").where(MODELS["campaigns"].id == group["campaign_id"])
        )
        current = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
        prefix = f"v{group['definition_version']}:{group['namespace']}:"
        model = MODELS["metric_buckets"]
        buckets = [
            dict(r.data)
            for r in session.scalars(
                _scoped_query(p, "metric_buckets")
                .where(
                    model.external_key >= prefix,
                    model.external_key < prefix + "~",
                    model.bucket_kind == "trial",
                )
                .order_by(model.external_key)
                .limit(1001)
            )
        ]
    if len(buckets) > 1000:
        raise ValueError(
            "Trial history exceeds the bounded report; explicit historical pagination is required"
        )
    epoch = epoch or current
    selected = [b for b in buckets if b["group"]["epoch"] == epoch]
    if not selected and epoch != current:
        from app.service import fail

        fail(404, "Controlled cohort not found")
    if len(selected) > 1:
        raise ValueError("Cohort definition changed without a new epoch; rebuild required")
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
    if study and service.clock() < timestamp(study["ends_at"]) + timedelta(days=meta["window_days"]):
        result["sufficient_evidence"] = False
        result["analysis_status"] = "Enrollment and all predeclared outcome windows must close"
    return result


def read_metrics(service, p):
    """One total plus bounded rep totals; never source history or a training dataset."""
    from app.measurement import connect_health_counts, work_summary

    with service.db.transaction(False) as session:
        config, group, values = _total_in(service, session, p)
        definition, namespace = group["definition_version"], group["namespace"]
        prefix = f"v{definition}:{namespace}:"
        contributions = MODELS["metric_contributions"]
        next_refresh = session.scalar(
            select(func.min(contributions.next_refresh_at)).where(
                contributions.tenant_id == p["tenant_id"],
                contributions.seller_id == p["seller_id"],
                contributions.external_key >= prefix,
                contributions.external_key < prefix + "~",
            )
        )
        model = MODELS["metric_buckets"]
        reps = list(
            session.scalars(
                _scoped_query(p, "metric_buckets")
                .where(
                    model.external_key >= prefix,
                    model.external_key < prefix + "~",
                    model.bucket_kind == "rep_total",
                )
                .order_by(model.external_key)
                .limit(501)
            )
        )
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
    work = work_summary([], [], now=service.clock())
    work.update(
        seconds=values.get("rep_work_seconds", 0) if values.get("work_evidence_count") else None,
        by_rep={
            r.data["group"]["rep_id"]: r.data["values"]["rep_work_seconds"]
            for r in reps[:500]
            if r.data["values"].get("work_evidence_count")
        },
        stale_intervals=values.get("stale_intervals", 0),
        rep_details_complete=len(reps) <= 500,
    )
    health = connect_health_counts(values)
    denominator = counts["original_eligible_denominator"]
    return {
        **counts,
        "definition_version": definition,
        "projection_namespace": namespace,
        "next_refresh_at": timestamp(next_refresh).isoformat() if next_refresh else None,
        "as_of": service.clock().isoformat(),
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
            name + "_cost_usd": values.get(key + "_microusd", 0) / 1_000_000
            for name, key in [
                ("reserved", "reserved"),
                ("verified", "verified"),
                ("gross", "gross"),
                ("credited", "credited"),
            ]
        },
        "observed_cost_usd": values.get("observed_call_cost_microusd", 0) / 1_000_000
        if values.get("observed_cost_calls")
        else None,
        "observed_cost_calls": values.get("observed_cost_calls", 0),
        "unknown_cost_calls": values.get("unknown_cost_calls", 0),
        "billing_verified": bool(values.get("usage_count")) and not values.get("unverified_usage_count"),
        "simulation": service.settings.simulation,
    }


def cleanup_page(service, p, payload, *, lease=None):
    """Delete at most500 obsolete derived rows by indexed namespace, never source data."""
    table = payload["table"]
    if table not in {"metric_contributions", "metric_buckets"}:
        raise ValueError("Only derived metric namespaces may be removed")
    definition, namespace = payload["definition"], payload["namespace"]
    # The same identity validation as source writes, with no content-derived path.
    unit_key(definition, namespace, "lead", "validation")
    with service.db.transaction() as session:
        service.report_revision(session, p, lock=True)
        if lease:
            job = session.get(Job, lease[0], with_for_update=True)
            if not _rebuild_owned(job, lease, service.clock()):
                return {"status": "lost_lease", "processed": 0}
        campaign = session.scalar(
            _scoped_query(p, "campaigns")
            .where(MODELS["campaigns"].id == payload["campaign_id"])
            .with_for_update()
        )
        pointer = dict(campaign.data.get("metric_projection", {})) if campaign else {}
        if (
            namespace in {pointer.get("active_namespace"), pointer.get("building_namespace")}
            or definition != pointer.get("active_definition")
            or {"namespace": namespace, "definition": definition} not in pointer.get("retired", [])
        ):
            return {"status": "protected", "processed": 0}
        prefix = f"v{definition}:{namespace}:"
        model = MODELS[table]
        ids = list(
            session.scalars(
                select(model.id)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.external_key >= prefix,
                    model.external_key < prefix + "~",
                )
                .order_by(model.external_key)
                .limit(500)
            )
        )
        if ids:
            from sqlalchemy import delete

            session.execute(delete(model).where(model.id.in_(ids)))
        remaining = False
        for name in ("metric_contributions", "metric_buckets"):
            derived = MODELS[name]
            remaining |= bool(
                session.scalar(
                    select(derived.id)
                    .where(
                        derived.tenant_id == p["tenant_id"],
                        derived.seller_id == p["seller_id"],
                        derived.external_key >= prefix,
                        derived.external_key < prefix + "~",
                    )
                    .limit(1)
                )
            )
        if not remaining:
            pointer["retired"] = [
                item
                for item in pointer["retired"]
                if item != {"namespace": namespace, "definition": definition}
            ]
            campaign.data = {**campaign.data, "metric_projection": pointer}
    return {"status": "pending" if len(ids) == 500 else "ready", "processed": len(ids)}
