"""Bounded queue reads, batched ranking, and a short authoritative resource claim."""

import threading
import time
from collections import OrderedDict
from datetime import timedelta

from sqlalchemy import and_, delete, or_, select, tuple_
from sqlalchemy.exc import IntegrityError

from app import ops
from app.calling.decide import Situation, require
from app.compliance.policy import authorize
from app.optimization.numbering import select_number, timestamp
from app.platform.db import MODELS, ActiveClaim, audit, doc, emit, prefetch, put, update
from app.scheduling.models import FEATURE_SCHEMA_VERSION, company_group
from app.scheduling.policy import queue_decision, rank_candidates

# Top K per page: only these leads and their endpoints are decrypted, scored and checked.
# Another page is read only when every candidate on this one is denied.
PAGE_SIZE, PAGES = 32, 3
# Preparation bookkeeping on the lead row. Packet work targets exactly the top-of-queue
# leads, so a claim must not abort on it; any other change (eligibility, queue order or a
# model feature) re-selects, so the frozen decision inputs match the claimed lead.
BOOKKEEPING_FIELDS = frozenset({"preparation", "preparation_lease", "preparation_revision"})


def decision_state(lead):
    return {k: v for k, v in lead.data.items() if k not in BOOKKEEPING_FIELDS}


IN_FLIGHT_SECONDS = 30  # Safety expiry; a claim attempt releases its pick as soon as it ends.
_IN_FLIGHT_LOCK = threading.Lock()


class SelectionChanged(Exception):
    """A ranked candidate lost eligibility before its atomic claim."""


def _hold_in_flight(service, p, lead_id):
    """Atomically mark a candidate as being claimed by this rep within this authority.

    Concurrent reps otherwise rank the same top candidate: one claim wins and the rest
    re-select repeatedly. This is a ranking hint; the database claim stays authoritative.
    """
    held = service.__dict__.setdefault("_in_flight_leads", {})
    key, now = (p["tenant_id"], p["seller_id"], lead_id), time.monotonic()
    with _IN_FLIGHT_LOCK:
        owner = held.get(key)
        if owner and owner[0] != p["user_id"] and owner[1] > now:
            return False
        held[key] = (p["user_id"], now + IN_FLIGHT_SECONDS)
        return True


def _number_holds(service, p, number_id, *, hold=None):
    """Count other reps' in-flight claims on a number; optionally add or drop this rep's."""
    held = service.__dict__.setdefault("_in_flight_numbers", {})
    key, now = (p["tenant_id"], p["seller_id"], number_id), time.monotonic()
    with _IN_FLIGHT_LOCK:
        owners = {u: t for u, t in held.get(key, {}).items() if t > now}
        if hold is True:
            owners[p["user_id"]] = now + IN_FLIGHT_SECONDS
        elif hold is False:
            owners.pop(p["user_id"], None)
        if owners:
            held[key] = owners
        else:
            held.pop(key, None)
        return sum(user != p["user_id"] for user in owners)


def _release_in_flight(service, p, lead_ids):
    held = service.__dict__.setdefault("_in_flight_leads", {})
    with _IN_FLIGHT_LOCK:
        for lead_id in lead_ids:
            key = (p["tenant_id"], p["seller_id"], lead_id)
            if held.get(key, (None,))[0] == p["user_id"]:
                del held[key]


def _queue_context(service, s, p, campaign_id, leads):
    ids = [lead.id for lead in leads]
    endpoints = [lead.data["endpoint_id"] for lead in leads]
    # Only equality hashes, references and operational columns are indexed. Text stays encrypted.

    keys = [lead.data["phone"] for lead in leads]
    specifications = {
        kind: []
        for kind in (
            "sellers",
            "users",
            "policy_versions",
            "number_inventory",
            "number_health",
            "budgets",
        )
    }
    specifications.update(
        {
            "campaigns": [MODELS["campaigns"].id.in_({campaign_id, *(r.data["campaign_id"] for r in leads)})],
            "registry_snapshots": [
                (
                    MODELS["registry_snapshots"].ctx_key.in_(("national", "state"))
                    & (MODELS["registry_snapshots"].ctx_status == "published")
                )
                | MODELS["registry_snapshots"].external_key.in_(["prepaid:" + key for key in keys])
            ],
            "model_versions": [
                (MODELS["model_versions"].ctx_key == "active")
                | (
                    (MODELS["model_versions"].ctx_key == "trial")
                    & (MODELS["model_versions"].ctx_expires_at > service.clock())
                )
            ],
            "phone_endpoints": [MODELS["phone_endpoints"].id.in_(endpoints)],
            "suppressions": [MODELS["suppressions"].external_key.in_(keys)],
            "lead_number_bindings": [MODELS["lead_number_bindings"].external_key.in_(ids)],
            "experiment_assignments": [MODELS["experiment_assignments"].op_lead_id.in_(ids)],
            "number_leases": [MODELS["number_leases"].op_active.is_(True)],
        }
    )
    prefetch(s, p, specifications)
    s.info["row_cache"][("leads", p["tenant_id"], p["seller_id"])] = leads


def _company_contacted(service, s, p, lead, claims, now):
    """Contacts at one company are never called back to back: after any call there, the
    others wait for the next calling day in the prospect's time zone. A requested callback
    or contact request is exempt."""
    from zoneinfo import ZoneInfo

    key = lead.data.get("company_key")
    if not key or lead.data.get("requested_contact") or lead.data.get("callback_due_at"):
        return False
    model = MODELS["leads"]
    siblings = s.info.get("list_company_siblings", {}).get(key)
    if siblings is None:
        siblings = list(
            s.scalars(
                select(model).where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.op_lead_id == key,
                    model.id != lead.id,
                )
            )
        )
    else:
        siblings = [sibling for sibling in siblings if sibling.id != lead.id]
    if not siblings:
        return False
    if {c.lead_id for c in claims} & {sibling.id for sibling in siblings}:
        return True
    try:
        zone = ZoneInfo(lead.data.get("timezone") or "UTC")
    except (ValueError, KeyError):
        zone = ZoneInfo("UTC")
    today = now.astimezone(zone).date()
    return any(
        (last := timestamp(sibling.data.get("last_attempt_at"))) and last.astimezone(zone).date() == today
        for sibling in siblings
    )


def _queue_presence(service, s, p):
    require(Situation(blockers=tuple(ops.dial_blockers_in(service, s, p, rep=True))))


def _selection(service, s, p, campaign_id):

    # Keyset pages bound each request while preserving progress past denied candidates.
    # This is a ranking hint only; every eventual claim rechecks permission.
    # A pass starts at the head and may continue over several requests; reaching its end
    # reports exhaustion, and the next request starts a fresh pass at the head, so newly due
    # or changed work there is never skipped.
    cursors = service.__dict__.setdefault("_queue_cursors", OrderedDict())
    key = (
        p["tenant_id"],
        p["seller_id"],
        p["user_id"],
        campaign_id,
        tuple(p.get("dial_lists") or ()),
        p.get("callback_lead_id"),
    )
    after, reasons = cursors.pop(key, (None, frozenset()))
    reasons = set(reasons)
    for _ in range(PAGES):
        plan, last, denied, complete = ops._selection_page(service, s, p, campaign_id, after)
        reasons.update(denied)
        if plan:
            return plan
        if complete:
            require(Situation(queue="none"), ops.no_lead_detail(service, s, p, reasons))
        after = last
    cursors[key] = (after, frozenset(reasons))
    while len(cursors) > 256:
        cursors.popitem(last=False)
    require(
        Situation(queue="scanning"),
        {"code": "QUEUE_SCAN_CONTINUES", "reasons": sorted(reasons), "retryable": True},
    )


def _selection_page(service, s, p, campaign_id, after):

    now, model = service.clock(), MODELS["leads"]
    # Exactly ix_leads_queue: promised callbacks (2), requested contact (1), then coverage
    # deadline order (overdue first). Only these top K rows are read and decrypted.
    page = []
    if after is not None:
        priority, coverage, identity = after
        page.append(
            or_(
                model.op_priority < priority,
                and_(
                    model.op_priority == priority,
                    tuple_(model.op_coverage_at, model.id) > tuple_(coverage, identity),
                ),
            )
        )
    leads = list(
        s.scalars(
            select(model)
            .where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.op_campaign_id == campaign_id,
                model.op_status == "eligible",
                or_(model.op_due_at <= now, model.op_due_at.is_(None)),
                *page,
            )
            .order_by(model.op_priority.desc(), model.op_coverage_at, model.id)
            .limit(PAGE_SIZE)
        )
    )
    ops._queue_context(service, s, p, campaign_id, leads)
    campaign = ops.required(service, s, "campaigns", campaign_id, p)
    ops._queue_presence(service, s, p)
    config = ops.optimization_config(service, s, p, campaign_id)
    # Another rep's selection may be claiming a number right now; count it as load so
    # concurrent reps plan on different numbers (the claim rereads capacity).
    inventory = [
        {**n, "active_obligations": n.get("active_obligations", 0) + ops._number_holds(service, p, n["id"])}
        for n in ops.inventory_evidence(service, s, p, fast=True)
    ]
    if service.managed.enabled(p["tenant_id"]):
        inventory = service.managed.assigned(p["tenant_id"], inventory)
    claims = s.info["active_claims"]
    own = next((c for c in claims if c.rep_id == p["user_id"]), None)
    if own:
        require(Situation(claim="live_call" if own.call_id else "reservation"))
    bindings = {
        r.data["lead_id"]: r.data["number_id"] for r in ops.scoped(service, s, "lead_number_bindings", p)
    }
    # D31: StatML is archived. A local number first, and a plain order: callbacks due, never called, longest waiting.
    allocations = {
        lead.id: select_number(
            doc(lead), inventory, bound_id=bindings.get(lead.id), strategy="regional_first"
        )
        for lead in leads
    }
    candidates = rank_candidates([doc(lead) for lead in leads], now)
    # Number health compares each number's pickups with this, the pickup rate across every number lately,
    # so a number that picks up less than the others shows (it was the model's estimate before D31).
    answers = sum((n.get("health") or {}).get("human_answers", 0) for n in inventory)
    classified = sum((n.get("health") or {}).get("human_classified_samples", 0) for n in inventory)
    average_pickup = (answers + 1) / (classified + 2)
    by_id = {lead.id: lead for lead in leads}
    claimed = {c.endpoint_id for c in claims}
    denied = []

    held = []

    def permitted(candidate):
        lead = by_id[candidate["id"]]
        if not ops._hold_in_flight(service, p, lead.id):
            denied.append("CANDIDATE_IN_FLIGHT")
            return False
        if not ops.own_test_line(service, p, lead) and ops._company_contacted(
            service, s, p, lead, claims, now
        ):
            denied.append("COMPANY_CONTACTED_TODAY")
            ops._release_in_flight(service, p, [lead.id])
            return False
        held.append(lead.id)
        number = next(
            (
                n
                for n in ops.scoped(service, s, "number_inventory", p)
                if n.id == allocations[lead.id]["number_id"]
            ),
            None,
        )
        decision = queue_decision(
            ops.context(service, s, p, lead, number),
            lead.data,
            own_claim=own,
            endpoint_claimed=lead.data["endpoint_id"] in claimed,
            by_hand=p.get("callback_lead_id") == lead.id,
            now=now,
        )
        denied.extend(decision.reasons)
        if not decision.allowed:
            ops._release_in_flight(service, p, [held.pop()])
        return decision.allowed

    chosen = next((c for c in candidates if permitted(c)), None)
    selection = chosen and {
        "lead_id": chosen["id"],
        "lead_selection_probability": 1.0,
        "selection_reason": "callbacks_then_never_called_then_longest_waiting",
        "eligible_lead_ids": [c["id"] for c in candidates],
    }
    if not selection:
        ops._release_in_flight(service, p, held)
        last = leads[-1] if leads else None
        cursor = (last.op_priority, last.op_coverage_at, last.id) if last else None
        return None, cursor, denied, len(leads) < PAGE_SIZE
    identifier = selection["lead_id"]
    ops._release_in_flight(service, p, [lead_id for lead_id in held if lead_id != identifier])
    if allocations[identifier]["number_id"]:
        ops._number_holds(service, p, allocations[identifier]["number_id"], hold=True)
    return (
        {
            "selection": selection,
            "features": {"decision_at": now.isoformat(), "conversation_sources": {}},
            "prediction": {"model_version": None, "p_human": average_pickup},
            "allocation": allocations[identifier],
            "profile": {},
            "lead_version": by_id[identifier].version,
            "lead_state": decision_state(by_id[identifier]),
            "configuration": {
                k: config[k] for k in ("conversion_goal", "window_days", "estimate_policy", "exploration")
            },
            "policy_id": campaign.data["policy_id"],
        },
        None,
        denied,
        False,
    )


def reserve(service, p, campaign_id, list_names=None, callback_lead_id=None):
    """Rank and claim in one transaction; a lost uniqueness race ranks again."""
    from app.service import fail

    p = {**p, "dial_lists": list_names, "callback_lead_id": callback_lead_id}
    if list_names == [] and not callback_lead_id:
        fail(409, "Choose at least one list")
    reasons = set()
    for _ in range(3):
        plan = None
        try:
            with service.db.transaction() as s, s.no_autoflush:
                # Claims run in parallel (shared); opt-outs and other authorization changes
                # still order against them. Endpoint, lead and rep uniqueness are constraints.
                service.db.authorize_stream(s, p, shared=True)
                plan = ops._selection(service, s, p, campaign_id)
                return ops._claim_in(service, s, p, campaign_id, plan)
        except SelectionChanged as changed:
            reasons.update(*changed.args)
        except IntegrityError:
            reasons.add("CONCURRENT_CLAIM")
        finally:
            if plan:
                ops._release_in_flight(service, p, [plan["selection"]["lead_id"]])
                if plan["allocation"]["number_id"]:
                    ops._number_holds(service, p, plan["allocation"]["number_id"], hold=False)
    fail(409, {"code": "QUEUE_CHANGED", "retryable": True, "reasons": sorted(reasons)})


def _claim_in(service, s, p, campaign_id, plan):
    now = service.clock()
    s.info["defer_flush"] = True
    if s.info.get("expired_claims"):
        # Lapsed reservations seen while ranking; deleted here so the new claim fits.
        s.execute(
            delete(ActiveClaim).where(
                ActiveClaim.id.in_(s.info["expired_claims"]),
                ActiveClaim.call_id.is_(None),
                ActiveClaim.expires_at <= now,
            )
        )
    lead = ops.required(service, s, "leads", plan["selection"]["lead_id"], p)
    number = (
        ops.required(service, s, "number_inventory", plan["allocation"]["number_id"], p)
        if plan["allocation"]["number_id"]
        else None
    )
    decision = authorize(ops.context(service, s, p, lead, number))
    if not decision.allowed:
        raise SelectionChanged(decision.reasons)
    campaign = ops.required(service, s, "campaigns", campaign_id, p)
    config = ops.optimization_config(service, s, p, campaign_id)
    ops.comparison_assignment(service, s, p, campaign, lead, "number_allocation")
    coach_comparison = ops.comparison_assignment(
        service, s, p, campaign, lead, "coaching", rep_id=p["user_id"]
    )
    script_comparison = ops.comparison_assignment(
        service, s, p, campaign, lead, "script", rep_id=p["user_id"]
    )
    reservation = put(
        s,
        "reservations",
        p["tenant_id"],
        p["seller_id"],
        {
            "rep_id": p["user_id"],
            "lead_id": lead.id,
            "endpoint_id": lead.data["endpoint_id"],
            "number_id": number.id,
            "expires_at": (now + timedelta(minutes=2)).isoformat(),
            "status": "reserved",
            "dial_scope": {
                "dial_lists": p.get("dial_lists"),
                "callback_lead_id": p.get("callback_lead_id"),
            },
            "policy_id": campaign.data["policy_id"],
            "coaching_comparison": coach_comparison,
            "script_comparison": script_comparison,
        },
        now=now,
    )
    s.add(
        ActiveClaim(
            tenant_id=p["tenant_id"],
            seller_id=p["seller_id"],
            endpoint_id=lead.data["endpoint_id"],
            lead_id=lead.id,
            rep_id=p["user_id"],
            number_id=number.id,
            reservation_id=reservation.id,
            expires_at=now + timedelta(minutes=2),
        )
    )
    binding = ops._by_key(service, s, p, "lead_number_bindings", lead.id)
    if binding:
        update(binding, number_id=number.id)
    else:
        put(
            s,
            "lead_number_bindings",
            p["tenant_id"],
            p["seller_id"],
            {"lead_id": lead.id, "number_id": number.id},
            key=lead.id,
            now=now,
        )
    actions = ["approved-default"]  # Script wording is tested by script comparisons instead.
    # No treatment may be randomized without a registered comparison (REMEDIATION 9.4);
    # none exists, so the reviewed default is chosen with certainty and logged as such.
    choice = {
        "action": actions[0],
        "propensity": 1.0,
        "eligible_actions": actions,
        "action_distribution": {action: float(action == actions[0]) for action in actions},
        "treatment_estimates": {},
        "treatment_reason": "no_registered_treatment_comparison",
    }
    snapshot = put(
        s,
        "feature_snapshots",
        p["tenant_id"],
        p["seller_id"],
        {
            "lead_id": lead.id,
            "as_of": plan["features"]["decision_at"],
            "company_id": company_group(lead.data),
            "company_group_basis": "supplied_identity_not_verified_business_ownership",
            "decision_at": plan["features"]["decision_at"],
            "source_versions": {
                "lead": {"id": lead.id, "version": plan["lead_version"]},
                "conversation": plan["features"]["conversation_sources"],
            },
            "goal": config["conversion_goal"],
            "goal_version": campaign.data.get("optimization_epoch", "conversion-v2"),
            "outcome_window_days": config["window_days"],
            "estimate_policy": config["estimate_policy"],
            "work_assumption_seconds": config["handling_seconds"],
            "policy_version": campaign.data["policy_id"],
            "model_version": plan["prediction"]["model_version"],
            "allowed_actions": choice["eligible_actions"],
            "chosen_action": choice["action"],
            "action_distribution": choice["action_distribution"],
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "campaign_id": campaign_id,
            "features": plan["features"],
            "predictions": plan["prediction"],
            "propensity": choice["propensity"],
            "experiment_arm": "none",
        },
        now=now,
    )
    allocation = plan["allocation"]
    if allocation.get("reassignment_reason"):
        audit(
            s,
            p["tenant_id"],
            p["seller_id"],
            p["user_id"],
            "number.binding_changed",
            lead.id,
            now,
            allocation,
        )
    policy_decision = put(
        s,
        "policy_decisions",
        p["tenant_id"],
        p["seller_id"],
        {
            "lead_id": lead.id,
            "feature_snapshot_id": snapshot.id,
            **choice,
            "policy_version": "conversion-coverage-v3",
            "rep_id": p["user_id"],
            "allocation": allocation,
            "predictions": plan["prediction"],
            "conversion_goal": config["conversion_goal"],
            **plan["selection"],
            "experiment_arm": "none",
            "outcome_window_days": config["window_days"],
        },
        now=now,
    )
    update(
        reservation,
        feature_snapshot_id=snapshot.id,
        treatment_variant_id=choice["action"],
        policy_decision_id=policy_decision.id,
    )
    emit(s, p["tenant_id"], "reservation.created", reservation, now)
    return {
        "reservation_id": reservation.id,
        "expires_at": reservation.data["expires_at"],
        "lead": doc(lead),
        "number": doc(number),
        "policy_version": campaign.data["policy_id"],
    }
