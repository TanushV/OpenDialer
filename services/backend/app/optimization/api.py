from datetime import datetime
from typing import Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field

from app.db import audit, doc, emit, put, uid, update
from app.optimization.numbering import timestamp
from app.service import fail


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OptimizationSettings(Strict):
    conversion_goal: Literal["qualified_conversation", "meeting_held", "sale_won"] | None = None
    window_days: int | None = Field(default=None, ge=1, le=180)
    exploration: float | None = Field(default=None, ge=0, le=0.3)
    auto_train: bool | None = None
    horizon_hours: int | None = Field(default=None, ge=1, le=168)
    rep_minutes: float | None = Field(default=None, ge=0, le=100800)
    handling_seconds: float | None = Field(default=None, ge=10, le=7200)
    daily_capacity_per_number: int | None = Field(default=None, ge=1, le=1000)
    training_min_rows: int | None = Field(default=None, ge=100, le=20000)


class Conversion(Strict):
    event_id: str = Field(min_length=3, max_length=200)
    kind: Literal["qualified_conversation", "meeting_held", "sale_won"]
    call_id: str | None = None
    occurred_at: datetime
    source_reference: str = Field(min_length=3, max_length=1000)
    value_usd: float | None = Field(default=None, ge=0)


class NumberReview(Strict):
    decision: Literal["increase", "reduce", "pause", "resume"]
    review_reference: str = Field(min_length=3, max_length=1000)
    daily_limit: int | None = Field(default=None, ge=1, le=1000)


class Retraction(Strict):
    reason: str = Field(min_length=3, max_length=1000)


class Feedback(Strict):
    coach_event_id: str
    useful: bool | None = None
    grounded: bool | None = None
    timely: bool | None = None
    distracting: bool | None = None
    delivery_ms: float | None = Field(default=None, ge=0, le=300000)
    render_ms: float | None = Field(default=None, ge=0, le=300000)
    event_to_render_ms: float | None = Field(default=None, ge=0, le=300000)
    note: str = Field(default="", max_length=1000)


class Empty(Strict):
    pass


class ModelTrial(Strict):
    review_reference: str = Field(min_length=3, max_length=1000)
    max_leads: int = Field(default=200, ge=20, le=10000)
    duration_days: int = Field(default=14, ge=1, le=90)


class Replay(Strict):
    cases: list[dict] = Field(min_length=1, max_length=200)
    review_reference: str = Field(min_length=3, max_length=1000)


class Comparison(Strict):
    family: Literal["number_allocation", "coaching"]
    review_reference: str = Field(min_length=3, max_length=1000)
    duration_days: int = Field(default=14, ge=1, le=90)


def register_optimization(app, service, principal, administrator):
    @app.get("/v1/optimization")
    def report(epoch: str | None = None, p=Depends(principal)):
        return service.optimization_report(p, epoch=epoch)

    @app.post("/v1/admin/optimization/comparisons")
    def start_comparison(body: Comparison, p=Depends(administrator)):
        from datetime import timedelta

        with service.db.transaction() as s:
            campaign = service.scoped(s, "campaigns", p)[0]
            config = service.optimization_config(s, p)
            studies = campaign.data.get("controlled_comparisons", [])
            if any(
                r["family"] == body.family
                and not r.get("stopped_at")
                and timestamp(r["ends_at"]) > service.clock()
                for r in studies
            ):
                fail(409, "This comparison already has an active protocol")
            if len(studies) >= 20:
                fail(409, "Retain and review existing comparison history before opening another protocol")
            population = [
                lead.id
                for lead in service.scoped(s, "leads", p)
                if lead.data.get("campaign_id") == campaign.id and lead.data.get("originally_eligible", True)
            ]
            study = {
                **body.model_dump(),
                "id": uid(),
                "starts_at": service.clock().isoformat(),
                "ends_at": (service.clock() + timedelta(days=body.duration_days)).isoformat(),
                "population": population,
                "goal": config["conversion_goal"],
                "window_days": config["window_days"],
                "control": "general_first" if body.family == "number_allocation" else "approved_static",
                "candidate": "regional_first" if body.family == "number_allocation" else "configured_coach",
                "reviewed_by": p["user_id"],
            }
            update(campaign, controlled_comparisons=studies + [study])
            if body.family == "number_allocation":
                for lead in service.scoped(s, "leads", p):
                    service.comparison_assignment(s, p, campaign, lead, body.family)
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "comparison.started",
                study["id"],
                service.clock(),
                {"family": body.family, "review_reference": body.review_reference},
            )
            emit(s, p["tenant_id"], "optimization.updated", campaign, service.clock())
            return {k: v for k, v in study.items() if k != "population"}

    @app.post("/v1/admin/optimization/comparisons/{comparison_id}/stop")
    def stop_comparison(comparison_id: str, body: Retraction, p=Depends(administrator)):
        with service.db.transaction() as s:
            campaign = service.scoped(s, "campaigns", p)[0]
            studies = campaign.data.get("controlled_comparisons", [])
            if not any(r["id"] == comparison_id for r in studies):
                fail(404, "Comparison not found")
            studies = [
                {**r, "stopped_at": service.clock().isoformat(), "stop_reason": body.reason}
                if r["id"] == comparison_id
                else r
                for r in studies
            ]
            update(campaign, controlled_comparisons=studies)
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "comparison.stopped",
                comparison_id,
                service.clock(),
                {"reason": body.reason},
            )
            emit(s, p["tenant_id"], "optimization.updated", campaign, service.clock())
            return {"stopped": True}

    @app.put("/v1/admin/optimization")
    def configure(body: OptimizationSettings, p=Depends(administrator)):
        values = body.model_dump(exclude_none=True)
        with service.db.transaction() as s:
            campaign = service.scoped(s, "campaigns", p)[0]
            previous = service.optimization_config(s, p)
            update(campaign, optimization={**previous, **values})
            if any(
                key in values and values[key] != previous[key] for key in ("conversion_goal", "window_days")
            ):
                update(campaign, optimization_epoch=uid(), optimization_trial=None)
                for candidate in service.scoped(s, "model_versions", p):
                    if candidate.data.get("trial_active"):
                        update(candidate, trial_active=False)
                service.ensure_experiment_cohort(s, p)
            if values.get("conversion_goal") and values["conversion_goal"] != previous["conversion_goal"]:
                for model in service.scoped(s, "model_versions", p):
                    if model.data.get("active"):
                        update(model, active=False, deactivation_reason="Conversion goal changed")
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "optimization.configured",
                campaign.id,
                service.clock(),
                {"fields": sorted(values)},
            )
            emit(s, p["tenant_id"], "optimization.updated", campaign, service.clock())
            return service.optimization_config(s, p)

    @app.post("/v1/admin/optimization/refresh")
    def refresh(p=Depends(administrator)):
        return service.refresh_optimization(p)

    @app.post("/v1/admin/optimization/train")
    def train(body: Empty, p=Depends(administrator)):
        return service.train_operational(p)

    @app.post("/v1/admin/models/{model_id}/start-trial")
    def start_trial(model_id: str, body: ModelTrial, p=Depends(administrator)):
        from datetime import timedelta

        from app.ml.training import predict_funnel

        with service.db.transaction() as s:
            model = service.required(s, "model_versions", model_id, p)
            config = service.optimization_config(s, p)
            metrics = model.data.get("metrics", {})
            source = next(
                (
                    r
                    for r in service.scoped(s, "learning_datasets", p)
                    if r.id == model.data.get("source_dataset_id")
                ),
                None,
            )
            if (
                model.data.get("active")
                or model.data.get("goal") != config["conversion_goal"]
                or not model.data.get("source_dataset_id")
                or not source
                or source.data.get("goal") != config["conversion_goal"]
                or metrics.get("brier", 1) >= metrics.get("pooled_baseline_brier", 0)
                or not predict_funnel(model.data, {}, service.settings.data_dir / "models")
            ):
                fail(409, "An owned verified candidate that beats the deployed pooled baseline is required")
            campaign = service.scoped(s, "campaigns", p)[0]
            for previous in service.scoped(s, "model_versions", p):
                if previous.data.get("trial_active"):
                    update(previous, trial_active=False)
            ends = service.clock() + timedelta(days=body.duration_days)
            study = {
                "model_id": model.id,
                "starts_at": service.clock().isoformat(),
                "ends_at": ends.isoformat(),
                "max_leads": body.max_leads,
                "review_reference": body.review_reference,
                "reviewed_by": p["user_id"],
                "control_fraction": 0.1,
            }
            update(campaign, optimization_epoch=uid(), optimization_trial=study)
            epoch = service.ensure_experiment_cohort(s, p)
            update(
                model,
                trial_active=True,
                trial_ends_at=ends.isoformat(),
                trial_epoch=epoch,
                trial_study=study,
                trial_review_reference=body.review_reference,
            )
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "model.controlled_trial_started",
                model.id,
                service.clock(),
                study,
            )
            emit(s, p["tenant_id"], "optimization.updated", campaign, service.clock())
            return doc(model)

    @app.post("/v1/admin/models/{model_id}/stop-trial")
    def stop_trial(model_id: str, body: Retraction, p=Depends(administrator)):
        with service.db.transaction() as s:
            model = service.required(s, "model_versions", model_id, p)
            update(
                model,
                trial_active=False,
                trial_stopped_at=service.clock().isoformat(),
                trial_stop_reason=body.reason,
            )
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "model.controlled_trial_stopped",
                model.id,
                service.clock(),
                {"reason": body.reason},
            )
            return doc(model)

    @app.get("/v1/leads/{lead_id}/conversions")
    def list_conversions(lead_id: str, p=Depends(principal)):
        with service.db.transaction(False) as s:
            service.required(s, "leads", lead_id, p)
            allowed_calls = {
                r.id
                for r in service.scoped(s, "call_attempts", p)
                if p["role"] != "rep" or r.data.get("rep_id") == p["user_id"]
            }
            return [
                doc(r)
                for r in service.scoped(s, "conversion_events", p)
                if r.data["lead_id"] == lead_id
                and (p["role"] != "rep" or r.data.get("call_id") in allowed_calls)
            ]

    @app.post("/v1/leads/{lead_id}/conversions")
    def conversion(lead_id: str, body: Conversion, p=Depends(principal)):
        now = service.clock()
        if body.occurred_at.tzinfo is None or body.occurred_at > now:
            fail(422, "A real observed UTC/timezone-aware conversion time no later than now is required")
        with service.db.transaction() as s:
            lead = service.required(s, "leads", lead_id, p)
            if p["role"] == "rep" and not body.call_id:
                fail(403, "A rep must attach the conversion to their own call")
            if body.call_id:
                call = service.required(s, "call_attempts", body.call_id, p)
                if call.data["lead_id"] != lead.id or body.occurred_at < timestamp(call.data["started_at"]):
                    fail(422, "Conversion must match the lead and occur after the attributed attempt")
            data = {**body.model_dump(mode="json"), "lead_id": lead.id}
            previous = next(
                (r for r in service.scoped(s, "conversion_events", p) if r.external_key == body.event_id),
                None,
            )
            if previous:
                if any(previous.data.get(k) != value for k, value in data.items()):
                    fail(409, "Conversion event ID already belongs to different evidence")
                return doc(previous)
            result = put(
                s,
                "conversion_events",
                p["tenant_id"],
                p["seller_id"],
                {
                    **data,
                    "recorded_by": p["user_id"],
                    "recorded_at": now.isoformat(),
                    "evidence_scope": "operator-recorded observed conversion",
                },
                key=body.event_id,
                now=now,
            )
            service.reconcile_goal_status(s, p, lead)
            audit(s, p["tenant_id"], p["seller_id"], p["user_id"], "conversion.recorded", result.id, now)
            emit(s, p["tenant_id"], "conversion.recorded", lead, now, {"kind": body.kind})
            return doc(result)

    @app.post("/v1/leads/{lead_id}/conversions/{event_id}/retract")
    def retract_conversion(lead_id: str, event_id: str, body: Retraction, p=Depends(principal)):
        with service.db.transaction() as s:
            lead = service.required(s, "leads", lead_id, p)
            result = service.required(s, "conversion_events", event_id, p)
            if result.data["lead_id"] != lead.id:
                fail(404, "Conversion not found")
            if p["role"] == "rep":
                service.required(s, "call_attempts", result.data.get("call_id"), p)
            update(
                result,
                retracted=True,
                retraction_reason=body.reason,
                retracted_by=p["user_id"],
                retracted_at=service.clock().isoformat(),
            )
            service.reconcile_goal_status(s, p, lead)
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "conversion.retracted",
                result.id,
                service.clock(),
                {"reason": body.reason},
            )
            emit(s, p["tenant_id"], "conversion.recorded", lead, service.clock(), {"retracted": True})
            return doc(result)

    @app.post("/v1/admin/numbers/{number_id}/review")
    def review_number(number_id: str, body: NumberReview, p=Depends(administrator)):
        with service.db.transaction() as s:
            number = service.required(s, "number_inventory", number_id, p)
            view = next(n for n in service.inventory_evidence(s, p) if n["id"] == number_id)
            limit = body.daily_limit or number.data.get("daily_limit", 5)
            if body.decision in {"increase", "resume"}:
                if not number.data.get("identity_configured") or not number.data.get("callback_tested"):
                    fail(409, "Identity and observed callback evidence are required")
                if body.decision == "increase" and (
                    view["health"]["samples"] < 5 or view["health"]["status"] == "investigating"
                ):
                    fail(409, "Review observed legitimate use before increasing number workload")
                if limit > service.optimization_config(s, p)["daily_capacity_per_number"]:
                    fail(409, "Requested limit exceeds the reviewed per-number planning ceiling")
                lifecycle = "limited_real_use"
            elif body.decision == "pause":
                lifecycle = "paused_for_outbound"
            else:
                lifecycle = number.data["lifecycle"]
                if limit >= number.data.get("daily_limit", 5):
                    fail(422, "Reduction must lower the existing daily limit")
            update(
                number,
                lifecycle=lifecycle,
                daily_limit=limit,
                inbound_retained=True,
                real_use_review_reference=body.review_reference,
            )
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "number.evidence_reviewed",
                number.id,
                service.clock(),
                {
                    "decision": body.decision,
                    "reference": body.review_reference,
                    "health_samples": view["health"]["samples"],
                },
            )
            emit(s, p["tenant_id"], "number.updated", number, service.clock())
            return doc(number)

    @app.post("/v1/calls/{call_id}/coach/feedback")
    def feedback(call_id: str, body: Feedback, p=Depends(principal)):
        with service.db.transaction() as s:
            service.required(s, "call_attempts", call_id, p)
            card = service.required(s, "coach_events", body.coach_event_id, p)
            if card.data["call_id"] != call_id:
                fail(422, "Feedback must reference this call's card")
            key = body.coach_event_id + ":" + p["user_id"]
            previous = next(
                (r for r in service.scoped(s, "coaching_feedback", p) if r.external_key == key), None
            )
            values = {
                **body.model_dump(exclude_none=True),
                "call_id": call_id,
                "reviewer_id": p["user_id"],
                "reviewed_at": service.clock().isoformat(),
            }
            result = (
                update(previous, **values)
                if previous
                else put(
                    s,
                    "coaching_feedback",
                    p["tenant_id"],
                    p["seller_id"],
                    values,
                    key=key,
                    now=service.clock(),
                )
            )
            return doc(result)

    @app.post("/v1/admin/optimization/trial")
    def evaluate_trial(body: Empty, p=Depends(administrator)):
        report = service.optimization_report(p)["trial"]
        with service.db.transaction() as s:
            result = put(
                s,
                "evaluation_reports",
                p["tenant_id"],
                p["seller_id"],
                {
                    **report,
                    "kind": "conversion_trial",
                    "created_at": service.clock().isoformat(),
                    "simulation": service.settings.simulation,
                    "review_required": True,
                    "eligible_for_activation": False,
                },
                now=service.clock(),
            )
            return doc(result)

    @app.post("/v1/admin/coaching/replay")
    async def replay(body: Replay, p=Depends(administrator)):
        from app.coaching.context import build_context, ground_guidance, retrieve_cards
        from app.coaching.replay import evaluate

        class ReplayProvider:
            async def suggest(self, request, deadline_ms=1200):
                state = build_context(
                    request.get("recent_dialogue", []),
                    version=request["state_version"],
                    facts=request.get("facts", []),
                    commitments=[],
                )
                cards = retrieve_cards(state, request.get("approved_cards", []))
                if not cards:
                    return {"action": "NO_ACTION"}
                result = {
                    "action": "SHOW_CARD",
                    "kind": state["trigger"],
                    "say_or_ask": cards[0]["text"],
                    "reason": "Approved replay prompt",
                    "state_version": request["state_version"],
                    "evidence_segment_ids": state["last_remote"]["evidence_segment_ids"],
                    "playbook_card_ids": [cards[0]["id"]],
                    "expires_in_ms": 7000,
                }
                ground_guidance(result, request)
                return result

        # A replay never auto-selects a paid route: simulation/static evaluation is explicit.
        provider = ReplayProvider() if service.settings.simulation else service.configured_coach(p)[0]
        if provider is None:
            fail(409, "Configure the live coach before running a provider replay")
        usage_object_id = "coach-replay:" + uid()

        class MeteredReplay:
            def __getattr__(self, name):
                return getattr(provider, name)

            async def suggest(self, request, deadline_ms=1200):
                from app.providers.inference import InferenceError, InferenceResult

                request_key = "replay:" + uid()
                if not service.reserve_optional_budget(
                    p, request_key, 0.01, object_id=usage_object_id, work_kind="coach_replay"
                ):
                    raise InferenceError("Replay exceeds the optional inference budget")
                result = None
                try:
                    result = await provider.suggest(request, deadline_ms)
                    return result
                finally:
                    if provider.last_usage or provider.last_request_id:
                        service.usage(
                            p,
                            "openrouter" if getattr(provider, "last_provider", None) else "groq",
                            "coach_replay",
                            InferenceResult(result or {}, provider.last_usage, provider.last_request_id),
                            object_id=usage_object_id,
                            reserved=0,
                            request_key=request_key,
                        )

        report = await evaluate(
            body.cases,
            provider if service.settings.simulation else MeteredReplay(),
            model="approved_static" if service.settings.simulation else provider.model,
        )
        with service.db.transaction() as s:
            result = put(
                s,
                "evaluation_reports",
                p["tenant_id"],
                p["seller_id"],
                {
                    **report,
                    "kind": "coaching_replay",
                    "usage_object_id": usage_object_id,
                    "review_reference": body.review_reference,
                    "created_at": service.clock().isoformat(),
                    "simulation": service.settings.simulation,
                },
                now=service.clock(),
            )
            return doc(result)
