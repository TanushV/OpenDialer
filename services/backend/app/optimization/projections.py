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

from sqlalchemy import select

from app.db import MODELS, Job, doc, put, uid, update
from app.operational_schema import call_work_bounds
from app.optimization.learning import conversion_at, outcome_at
from app.optimization.numbering import timestamp
from app.scheduling.models import derive_labels


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
    return values


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
    return values


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
        for value in [row.data, *([previous] if previous else [])]:
            if table in {"call_attempts", "conversion_events"} and value.get("lead_id"):
                units[(*scope, "lead", value["lead_id"])] = False
            if table == "coaching_feedback" and value.get("coach_event_id"):
                units[(*scope, "help", value["coach_event_id"])] = False
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
            "kind": "metric_refresh",
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
                "metrics": "cohort-work-v4",
                "epoch": epoch,
                "goal": config["conversion_goal"],
                "window_days": config["window_days"],
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
        table = {
            "lead": "leads",
            "call": "call_attempts",
            "usage": "usage_ledger",
            "help": "coach_events",
        }.get(kind)
        source = (
            session.scalar(_scoped_query(p, table).where(MODELS[table].id == identity)) if table else None
        )
        row = doc(source) if source else {}
        calls, events, intervals, feedback, suppressed = [], [], [], None, False
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
            phone = row.get("phone")
            if phone:
                key = hmac.new(service.db.identity_key, phone.encode(), hashlib.sha256).hexdigest()
                denial = service._by_key(session, p, "suppressions", key)
                suppressed = bool(denial and denial.data.get("active", True))
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
                record = service._by_key(session, p, "coaching_feedback", identity + ":" + rep)
                feedback = doc(record) if record else None
        previous = {
            ns: service._by_key(session, p, "metric_contributions", unit_key(definition, ns, kind, identity))
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
        "suppressed": suppressed,
        "campaign_id": campaign.id,
        "config": config,
        "definition": definition,
        "namespaces": namespaces,
        "previous": previous,
        "generation": generation,
        "pointer": dict(pointer),
        "input_version_fingerprint": hashlib.sha256(
            json.dumps(
                [
                    [
                        (r.get("id"), r.get("version"))
                        for r in [row, *calls, *events, *intervals, feedback or {}]
                    ],
                    definition,
                    suppressed,
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
    elif kind == "call" and row:
        values = call_values(row, config=config, now=now)
        start = timestamp(
            row.get("submitted_at") or (row.get("started_at") if row.get("provider_id") else None)
        )
        refresh.extend(
            t
            for t in (start, start + timedelta(days=config["window_days"]) if start else None)
            if t and t > now
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
            {"bucket_kind": "rep", "bucket_day": day, "rep_id": snapshot["identity"].rsplit(":", 1)[0]}
        )
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
    return hmac.new(
        service.db.identity_key,
        json.dumps(group, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()


def refresh_unit(service, p, payload):
    """Compute outside writes, then replace one source vector under a generation fence."""
    snapshot = _unit_snapshot(service, p, payload)
    values, due = _calculate_unit(snapshot, service.clock())
    kind, identity = snapshot["kind"], snapshot["identity"]
    with service.db.transaction() as session:
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
            _scoped_query(p, "campaigns").where(MODELS["campaigns"].id == snapshot["campaign_id"])
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
            key = unit_key(snapshot["definition"], namespace, kind, identity)
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
            key = unit_key(snapshot["definition"], namespace, kind, identity)
            saved = previous.data if previous else {}
            groups = _groups(snapshot, namespace) if values else []
            fingerprint = hashlib.sha256(
                json.dumps(
                    [values, groups, due, snapshot["generation"], snapshot["input_version_fingerprint"]],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            if saved.get("input_fingerprint") == fingerprint:
                continue
            old_groups = {_group_key(service, g): g for g in saved.get("groups", [])}
            new_groups = {_group_key(service, g): g for g in groups}
            for group_key in sorted(old_groups.keys() | new_groups.keys()):
                delta = delta_values(
                    saved.get("values", {}) if group_key in old_groups else {},
                    values if group_key in new_groups else {},
                )
                if not delta:
                    continue
                bucket_model = MODELS["metric_buckets"]
                bucket = session.scalar(
                    _scoped_query(p, "metric_buckets")
                    .where(bucket_model.external_key == group_key)
                    .with_for_update()
                )
                old_values = bucket.data.get("values", {}) if bucket else {}
                total = {**old_values, **{k: old_values.get(k, 0) + v for k, v in delta.items()}}
                value = {
                    **(new_groups.get(group_key) or old_groups[group_key]),
                    "group": new_groups.get(group_key) or old_groups[group_key],
                    "group_key": group_key,
                    "values": total,
                }
                if bucket:
                    update(bucket, **value)
                else:
                    put(
                        session,
                        "metric_buckets",
                        p["tenant_id"],
                        p["seller_id"],
                        value,
                        key=group_key,
                        now=service.clock(),
                    )
            value = {
                "source_kind": kind,
                "source_id": identity,
                "source_version": snapshot["row"].get("version", snapshot["generation"]),
                "campaign_id": snapshot["campaign_id"],
                "definition_version": snapshot["definition"],
                "namespace": namespace,
                "input_fingerprint": fingerprint,
                "input_version_fingerprint": snapshot["input_version_fingerprint"],
                "dirty_generation": snapshot["generation"],
                "values": values,
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
        return {"status": "ready" if changed else "unchanged"}


def refresh_due(service, p, *, limit=100):
    if not 1 <= limit <= 100:
        raise ValueError("At most100 due contributions per sweep")
    with service.db.transaction(False) as session:
        model = MODELS["metric_contributions"]
        sources = list(
            session.execute(
                select(model.source_kind, model.source_id)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.next_refresh_at <= service.clock(),
                )
                .order_by(model.next_refresh_at, model.id)
                .limit(limit)
            )
        )
    for kind, identity in dict.fromkeys(sources):
        refresh_unit(service, p, {"source_kind": kind, "source_id": identity})
    return {"processed": len(sources)}
