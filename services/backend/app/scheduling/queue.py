"""Bounded queue reads, batched ranking, and a short authoritative resource claim."""

import secrets
from collections import OrderedDict
from datetime import timedelta

from sqlalchemy import and_, delete, or_, select, tuple_

from app.compliance.policy import authorize
from app.db import MODELS, ActiveClaim, audit, doc, emit, prefetch, put, update
from app.optimization.learning import (
    choose_cohort_candidate,
    conversion_score,
    freeze_features,
    learn_distribution,
    stable_control,
)
from app.optimization.numbering import select_number, timestamp
from app.scheduling.models import company_group, profile_current, saved_prediction
from app.scheduling.policy import queue_decision, rank_candidates

# Top K per page: only these leads and their endpoints are decrypted, scored and checked.
# Another page is read only when every candidate on this one is denied.
PAGE_SIZE, PAGES = 32, 3


class SelectionChanged(Exception):
    """A ranked candidate lost eligibility before its atomic claim."""


class ReservationPlanner:
    def _queue_context(self, s, p, campaign_id, leads):
        ids = [lead.id for lead in leads]
        endpoints = [lead.data["endpoint_id"] for lead in leads]
        # Only equality hashes, references and operational columns are indexed. Text stays encrypted.
        import hashlib
        import hmac

        keys = [
            hmac.new(self.db.identity_key, lead.data["phone"].encode(), hashlib.sha256).hexdigest()
            for lead in leads
        ]
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
                "campaigns": [
                    MODELS["campaigns"].id.in_({campaign_id, *(r.data["campaign_id"] for r in leads)})
                ],
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
                        & (MODELS["model_versions"].ctx_expires_at > self.clock())
                    )
                ],
                "phone_endpoints": [MODELS["phone_endpoints"].id.in_(endpoints)],
                "suppressions": [MODELS["suppressions"].external_key.in_(keys)],
                "lead_number_bindings": [MODELS["lead_number_bindings"].external_key.in_(ids)],
                "consent_evidence": [MODELS["consent_evidence"].ctx_key.in_(endpoints)],
                "experiment_assignments": [MODELS["experiment_assignments"].op_lead_id.in_(ids)],
                "treatment_variants": [],
                "number_leases": [MODELS["number_leases"].op_active.is_(True)],
            }
        )
        prefetch(s, p, specifications)
        s.info["row_cache"][("leads", p["tenant_id"], p["seller_id"])] = leads

    def _queue_presence(self, s, p):
        from app.service import fail

        presence = self.required(s, "users", p["user_id"], p).data.get("presence", {})
        if presence.get("pending_optouts", 0):
            fail(409, "PENDING_LOCAL_OPTOUTS")
        if presence.get("status") != "ready" or not all(
            presence.get(k) for k in ("audio_ready", "telephony_ready", "agent_call_control_id")
        ):
            fail(409, "REP_OR_AUDIO_NOT_READY")

    def _selection(self, p, campaign_id):
        from app.service import fail

        # Keyset pages bound each request while preserving progress past denied candidates.
        # This is a ranking hint only; every eventual claim rechecks permission.
        cursors = self.__dict__.setdefault("_queue_cursors", OrderedDict())
        key = (p["tenant_id"], p["seller_id"], p["user_id"], campaign_id)
        after = cursors.pop(key, None)
        revisit_head = after is not None
        reasons = set()
        for _ in range(PAGES):
            plan, last, denied, complete = self._selection_page(p, campaign_id, after)
            reasons.update(denied)
            if plan:
                return plan
            if complete:
                if revisit_head:
                    # Revisit the head once so newly due/changed work is not skipped.
                    after, revisit_head = None, False
                    continue
                fail(409, {"code": "NO_ELIGIBLE_LEADS", "reasons": sorted(reasons)})
            after = last
        cursors[key] = after
        while len(cursors) > 256:
            cursors.popitem(last=False)
        fail(409, {"code": "QUEUE_SCAN_CONTINUES", "reasons": sorted(reasons), "retryable": True})

    def _selection_page(self, p, campaign_id, after):
        from app.service import fail

        now, model = self.clock(), MODELS["leads"]
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
        with self.db.transaction(False) as s:
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
            self._queue_context(s, p, campaign_id, leads)
            campaign = self.required(s, "campaigns", campaign_id, p)
            self._queue_presence(s, p)
            config = self.optimization_config(s, p, campaign_id)
            # The normal queue reads the latest compact learning projection, never a training corpus.
            profile = campaign.data.get("selection_profile", {})
            if not profile_current(profile, config):
                profile = {}
            inventory = self.inventory_evidence(s, p, fast=True)
            claims = s.info["active_claims"]
            own = next((c for c in claims if c.rep_id == p["user_id"]), None)
            if own:
                fail(409, "LIVE_OBLIGATION" if own.call_id else "RESERVATION_EXISTS")
            bindings = {
                r.data["lead_id"]: r.data["number_id"] for r in self.scoped(s, "lead_number_bindings", p)
            }
            allocations, features, predictions, by_model = {}, {}, {}, {}
            for lead in leads:
                comparison = self.comparison_assignment(
                    s, p, campaign, lead, "number_allocation", persist=False
                )
                allocation = select_number(
                    doc(lead),
                    inventory,
                    bound_id=bindings.get(lead.id),
                    strategy="general_first"
                    if comparison and comparison["arm"] == "control"
                    else "regional_first",
                )
                if comparison:
                    allocation.update(
                        comparison_id=comparison["comparison_id"], comparison_arm=comparison["arm"]
                    )
                allocations[lead.id] = allocation
                number = next((n for n in inventory if n["id"] == allocation["number_id"]), None)
                endpoint = self.required(s, "phone_endpoints", lead.data["endpoint_id"], p)
                values = freeze_features(
                    {
                        **doc(lead),
                        "created_at": lead.created_at.isoformat(),
                        "ownership_status": endpoint.data.get("ownership_status"),
                    },
                    health=number["health"] if number else {"score": None, "samples": 0},
                    now=now,
                )
                values["callback_history"] = lead.data.get("callback_history", 0)
                features[lead.id] = values
                predictions[lead.id] = saved_prediction(profile, values, config)
                serving = self.serving_model(s, p, config, lead.id)
                if serving:
                    by_model.setdefault(serving.id, (serving, []))[1].append(lead.id)
            for serving, identifiers in by_model.values():
                from app.ml.training import predict_funnel_batch

                estimates = predict_funnel_batch(
                    serving.data, [features[i] for i in identifiers], self.settings.data_dir / "models"
                )
                for identifier, estimate in zip(identifiers, estimates, strict=True):
                    if estimate:
                        predictions[identifier].update(**estimate, model_version=serving.id)
            candidates = rank_candidates(
                [{**doc(lead), "propensity": conversion_score(predictions[lead.id])} for lead in leads], now
            )
            by_id = {lead.id: lead for lead in leads}
            claimed = {c.endpoint_id for c in claims}
            denied = []

            def permitted(candidate):
                lead = by_id[candidate["id"]]
                number = next(
                    (
                        n
                        for n in self.scoped(s, "number_inventory", p)
                        if n.id == allocations[lead.id]["number_id"]
                    ),
                    None,
                )
                decision = queue_decision(
                    self.context(s, p, lead, number),
                    lead.data,
                    own_claim=own,
                    endpoint_claimed=lead.data["endpoint_id"] in claimed,
                )
                denied.extend(decision.reasons)
                return decision.allowed

            urgent = next(
                (
                    c
                    for c in candidates
                    if (
                        c.get("requested_contact")
                        or (timestamp(c.get("callback_due_at")) and timestamp(c["callback_due_at"]) <= now)
                        or (timestamp(c.get("coverage_due_at")) and timestamp(c["coverage_due_at"]) <= now)
                    )
                    and permitted(c)
                ),
                None,
            )
            probes = [urgent] if urgent else []
            if not urgent:
                for control in (True, False):
                    group = [c for c in candidates if stable_control(c["id"]) == control]
                    if control:
                        group.sort(key=lambda c: -predictions[c["id"]]["p_human"])
                    selected = next((c for c in group if permitted(c)), None)
                    if selected:
                        probes.append(selected)
            selection = choose_cohort_candidate(
                [
                    {
                        **c,
                        "arm": "control" if stable_control(c["id"]) else "candidate",
                        "p_human": predictions[c["id"]]["p_human"],
                        "score": conversion_score(predictions[c["id"]]),
                    }
                    for c in probes
                ],
                now=now,
                draw=secrets.randbelow(1000000) / 1000000,
            )
            if not selection:
                last = leads[-1] if leads else None
                cursor = (last.op_priority, last.op_coverage_at, last.id) if last else None
                return None, cursor, denied, len(leads) < PAGE_SIZE
            identifier = selection["lead_id"]
            return (
                {
                    "selection": selection,
                    "features": features[identifier],
                    "prediction": predictions[identifier],
                    "allocation": allocations[identifier],
                    "profile": profile,
                    "lead_version": by_id[identifier].version,
                    "configuration": {
                        k: config[k]
                        for k in ("conversion_goal", "window_days", "estimate_policy", "exploration")
                    },
                    "policy_id": campaign.data["policy_id"],
                },
                None,
                denied,
                False,
            )

    def reserve(self, p, campaign_id):
        from app.service import fail

        reasons = set()
        for _ in range(3):
            plan = self._selection(p, campaign_id)
            try:
                return self._claim_selection(p, campaign_id, plan)
            except SelectionChanged as changed:
                reasons.update(*changed.args)
        fail(409, {"code": "QUEUE_CHANGED", "retryable": True, "reasons": sorted(reasons)})

    def _claim_selection(self, p, campaign_id, plan):
        from app.service import fail

        now = self.clock()
        with self.db.transaction() as s, s.no_autoflush:
            self.db.authorize_stream(s, p)
            lead = self.required(s, "leads", plan["selection"]["lead_id"], p)
            if lead.version != plan["lead_version"]:
                raise SelectionChanged()
            self._queue_context(s, p, campaign_id, [lead])
            self._queue_presence(s, p)
            s.info["defer_flush"] = True
            s.execute(
                delete(ActiveClaim).where(
                    ActiveClaim.tenant_id == p["tenant_id"],
                    ActiveClaim.call_id.is_(None),
                    ActiveClaim.expires_at <= now,
                )
            )
            inventory = self.inventory_evidence(s, p, fast=True)
            if any(c.rep_id == p["user_id"] for c in s.info["active_claims"]):
                fail(409, "RESERVATION_EXISTS")
            if any(c.endpoint_id == lead.data["endpoint_id"] for c in s.info["active_claims"]):
                raise SelectionChanged()
            current_binding = next(iter(self.scoped(s, "lead_number_bindings", p)), None)
            if current_binding and current_binding.data["number_id"] != plan["allocation"]["number_id"]:
                raise SelectionChanged()
            allocation = select_number(doc(lead), inventory, bound_id=plan["allocation"]["number_id"])
            if (
                lead.data["status"] != "eligible"
                or lead.data.get("outreach_hold")
                or allocation["number_id"] != plan["allocation"]["number_id"]
            ):
                raise SelectionChanged()
            number = (
                self.required(s, "number_inventory", allocation["number_id"], p)
                if allocation["number_id"]
                else None
            )
            decision = authorize(self.context(s, p, lead, number))
            if not decision.allowed:
                # Eligibility changed after ranking; select again within the bounded retry.
                raise SelectionChanged(decision.reasons)
            campaign = self.required(s, "campaigns", campaign_id, p)
            config = self.optimization_config(s, p, campaign_id)
            if campaign.data["policy_id"] != plan["policy_id"] or any(
                config[k] != v for k, v in plan["configuration"].items()
            ):
                raise SelectionChanged()
            record = self.record_decision(s, p, lead, decision)
            self.comparison_assignment(s, p, campaign, lead, "number_allocation")
            coach_comparison = self.comparison_assignment(
                s, p, campaign, lead, "coaching", rep_id=p["user_id"]
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
                    "decision_id": record.id,
                    "policy_id": campaign.data["policy_id"],
                    "coaching_comparison": coach_comparison,
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
            binding = next(iter(self.scoped(s, "lead_number_bindings", p)), None)
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
            actions = [
                v.id
                for v in self.scoped(s, "treatment_variants", p)
                if v.data.get("approved") and v.data["campaign_id"] == campaign_id
            ] or ["approved-default"]
            urgent = bool(
                lead.data.get("callback_due_at")
                or lead.data.get("requested_contact")
                or timestamp(lead.data["coverage_due_at"]) <= now
            )
            distribution = plan["profile"].get("treatments", {}).get(str(plan["features"]["segment"]))
            if (
                stable_control(lead.id)
                or urgent
                or not distribution
                or set(distribution["probabilities"]) != set(actions)
            ):
                distribution = learn_distribution(
                    actions,
                    [],
                    segment=plan["features"]["segment"],
                    exploration=config["exploration"],
                    control=stable_control(lead.id),
                    urgent=urgent,
                )
            draw, cumulative, selected = secrets.randbelow(1000000) / 1000000, 0.0, actions[-1]
            for action, probability in distribution["probabilities"].items():
                cumulative += probability
                if draw < cumulative:
                    selected = action
                    break
            choice = {
                "action": selected,
                "propensity": distribution["probabilities"][selected],
                "eligible_actions": actions,
                "action_distribution": distribution["probabilities"],
                "treatment_estimates": distribution.get("estimates", {}),
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
                    "feature_schema_version": 2,
                    "campaign_id": campaign_id,
                    "features": plan["features"],
                    "predictions": plan["prediction"],
                    "propensity": choice["propensity"],
                    "experiment_arm": "control" if stable_control(lead.id) else "candidate",
                },
                now=now,
            )
            allocation = plan["allocation"]
            allocation_record = put(
                s,
                "allocation_decisions",
                p["tenant_id"],
                p["seller_id"],
                {"lead_id": lead.id, "rep_id": p["user_id"], **allocation, "created_at": now.isoformat()},
                now=now,
            )
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
                    "experiment_arm": "control" if stable_control(lead.id) else "candidate",
                    "outcome_window_days": config["window_days"],
                },
                now=now,
            )
            update(
                reservation,
                feature_snapshot_id=snapshot.id,
                allocation_decision_id=allocation_record.id,
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
