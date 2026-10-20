import asyncio
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.db import audit, doc, emit, put, uid, update
from app.optimization.goals import goal_plan
from app.optimization.numbering import HealthPolicy, timestamp
from app.scheduling.models import EstimatePolicy
from app.service import fail


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StaffedWindow(Strict):
    start_at: AwareDatetime
    end_at: AwareDatetime
    reps: int = Field(ge=1, le=100, strict=True)
    pool_id: str | None = Field(default=None, min_length=1, max_length=64)
    rep_ids: list[Annotated[str, Field(min_length=1, max_length=36)]] | None = Field(
        default=None, min_length=1, max_length=100
    )

    @model_validator(mode="after")
    def ordered(self):
        if self.end_at <= self.start_at or (self.end_at - self.start_at).total_seconds() > 7 * 86400:
            raise ValueError("A staffed window must be positive and at most seven days")
        if self.rep_ids is not None and (
            len(set(self.rep_ids)) != self.reps or len(self.rep_ids) != self.reps
        ):
            raise ValueError("Named staffing must list each available rep once and match the headcount")
        return self


class OptimizationSettings(Strict):
    expected_campaign_version: int | None = Field(default=None, ge=1)
    conversion_goal: Literal["qualified_conversation", "meeting_held", "sale_won"] | None = None
    window_days: int | None = Field(default=None, ge=1, le=180)
    exploration: float | None = Field(default=None, ge=0, le=0.3)
    auto_train: bool | None = None
    horizon_hours: int | None = Field(default=None, ge=1, le=168)
    rep_minutes: float | None = Field(default=None, ge=0, le=100800)
    handling_seconds: float | None = Field(default=None, ge=10, le=7200)
    daily_capacity_per_number: int | None = Field(default=None, ge=1, le=1000)
    training_min_rows: int | None = Field(default=None, ge=100, le=20000)
    staffed_windows: list[StaffedWindow] | None = Field(default=None, max_length=100)
    additional_ready_after_hours: int | None = Field(default=None, ge=0, le=168)
    staffing_timezone: str | None = None
    staffing_start_hour: int | None = Field(default=None, ge=0, le=23)
    staffing_end_hour: int | None = Field(default=None, ge=1, le=24)
    staffing_reps: int | None = Field(default=None, ge=1, le=100)
    staffing_weekdays: list[Annotated[int, Field(ge=0, le=6)]] | None = Field(
        default=None, min_length=1, max_length=7
    )
    account_channel_limit: int | None = Field(default=None, ge=1, le=1000)
    minimum_expansion_age_hours: int | None = Field(default=None, ge=1, le=168)
    production_holdout_fraction: float | None = Field(default=None, ge=0.01, le=0.5, allow_inf_nan=False)
    health_policy: HealthPolicy | None = None
    estimate_policy: EstimatePolicy | None = None

    @model_validator(mode="after")
    def distinct_windows(self):
        if self.staffing_timezone is not None:
            try:
                ZoneInfo(self.staffing_timezone)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise ValueError("A valid staffing IANA timezone is required") from exc
        if self.staffing_weekdays is not None and len(set(self.staffing_weekdays)) != len(
            self.staffing_weekdays
        ):
            raise ValueError("Each staffed weekday is listed once")
        windows = sorted(self.staffed_windows or [], key=lambda w: w.start_at)
        if len({rep for w in windows for rep in (w.rep_ids or [])}) > 100:
            raise ValueError("A staffing plan supports at most 100 named reps")
        for i, a in enumerate(windows):
            for b in windows[i + 1 :]:
                if b.start_at >= a.end_at:
                    break
                if (a.rep_ids is None or b.rep_ids is None) and not (a.pool_id and a.pool_id == b.pool_id):
                    raise ValueError(
                        "Overlapping periods must name every rep; anonymous headcount cannot overlap"
                    )
        return self


class Conversion(Strict):
    event_id: str = Field(min_length=3, max_length=200)
    kind: Literal["qualified_conversation", "meeting_held", "sale_won"]
    call_id: str | None = None
    occurred_at: datetime
    source_reference: str = Field(min_length=3, max_length=1000)
    value_usd: float | None = Field(default=None, ge=0)


class NumberReview(Strict):
    request_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1, strict=True)
    decision: Literal["increase", "reduce", "pause", "resume"]
    review_reference: str = Field(min_length=3, max_length=1000)
    daily_limit: int | None = Field(default=None, ge=1, le=1000)


class Retraction(Strict):
    reason: str = Field(min_length=3, max_length=1000)


class Feedback(Strict):
    coach_event_id: str
    request_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=0, strict=True)
    displayed: Literal[True] | None = None
    used_by_rep: bool | None = None
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

    @model_validator(mode="after")
    def bounded(self):
        from app.coaching.replay import validate_cases

        validate_cases(self.cases)
        return self


class Comparison(Strict):
    family: Literal["number_allocation", "coaching"]
    review_reference: str = Field(min_length=3, max_length=1000)
    duration_days: int = Field(default=14, ge=1, le=90)
    request_id: UUID | None = None
    strategy_ids: list[str] | None = Field(default=None, min_length=2, max_length=8)
    strategy_policy: Literal["two_arm", "adaptive"] = "two_arm"
    candidate_probability: float = Field(default=0.5, ge=0.1, le=0.9, allow_inf_nan=False)
    evidence_comparison_id: str | None = None

    @model_validator(mode="after")
    def finite(self):
        if self.strategy_ids is not None and (
            not self.request_id
            or self.family != "coaching"
            or self.strategy_ids[0] != "control"
            or len(self.strategy_ids) != len(set(self.strategy_ids))
            or self.strategy_policy == "two_arm"
            and len(self.strategy_ids) != 2
        ):
            raise ValueError(
                "Reviewed finite strategies require a request identity, control first and two arms for a fixed comparison"
            )
        return self


class ConversationAlias(Strict):
    field: Literal["concept", "approach"]
    source: str = Field(min_length=1, max_length=160)
    target: str = Field(min_length=1, max_length=160)


class VocabularyEdit(Strict):
    expected_version: int = Field(ge=1)
    aliases: list[ConversationAlias] = Field(max_length=100)
    reason: str = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def validate_aliases(self):
        from app.optimization.conversations import aliases_for

        aliases_for(self.model_dump())
        return self


class ObservationReview(Strict):
    expected_version: int = Field(ge=1)
    decision: Literal["confirm", "retract"]
    reason: str = Field(min_length=3, max_length=500)


def register_optimization(app, service, principal, administrator):
    @app.get("/v1/optimization/conversations")
    def conversation_report(p=Depends(principal)):
        with service.db.transaction(False) as s:
            campaign = service.scoped(s, "campaigns", p)[0]
            vocabulary = campaign.data.get("conversation_vocabulary", {"version": 1, "aliases": []})
            cached = campaign.data.get("conversation_report", {})
            config = service.optimization_config(s, p, campaign.id)
            stale = (
                cached.get("stale")
                or cached.get("goal") != config["conversion_goal"]
                or cached.get("window_days") != config["window_days"]
                or cached.get("vocabulary_version") != vocabulary["version"]
            )
            return {
                "vocabulary": vocabulary,
                "report": {
                    **cached,
                    "status": "stale" if cached and stale else "ready" if cached else "awaiting_refresh",
                    "patterns": [] if stale else cached.get("patterns", []),
                    "stale": bool(stale),
                },
                "campaign_id": campaign.id,
            }

    @app.put("/v1/admin/optimization/conversation-vocabulary")
    def edit_conversation_vocabulary(body: VocabularyEdit, p=Depends(administrator)):
        from app.optimization.conversations import invalidate_report

        with service.db.transaction() as s:
            campaign = service.scoped(s, "campaigns", p)[0]
            existing = campaign.data.get("conversation_vocabulary", {"version": 1})
            if existing["version"] != body.expected_version:
                fail(409, "Concept names changed; refresh before reviewing")
            vocabulary = {
                "version": existing["version"] + 1,
                "aliases": [a.model_dump() for a in body.aliases],
                "reviewed_by": p["user_id"],
                "reviewed_at": service.clock().isoformat(),
            }
            update(campaign, conversation_vocabulary=vocabulary)
            invalidate_report(service, s, p, campaign.id)
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "conversation.vocabulary_reviewed",
                campaign.id,
                service.clock(),
                {"reason": body.reason, "previous": existing, "new": vocabulary},
            )
            service.jobs.enqueue_in(
                s,
                "ml",
                {"kind": "optimization_refresh", "seller_id": p["seller_id"], "user_id": p["user_id"]},
                f"conversation-vocabulary:{campaign.id}:{vocabulary['version']}",
                tenant_id=p["tenant_id"],
            )
            emit(s, p["tenant_id"], "optimization.updated", campaign, service.clock())
            return vocabulary

    @app.post("/v1/calls/{call_id}/observations/{observation_id}/review")
    def review_observation(call_id: str, observation_id: str, body: ObservationReview, p=Depends(principal)):
        from app.coaching.context import invalidate_state, lineage_metadata, upgrade_state
        from app.optimization.conversations import invalidate_report
        from app.postcall.analysis import learning_observations
        from app.postcall.commitments import invalidate as invalidate_commitments

        with service.db.transaction() as s:
            call = service.required(s, "call_attempts", call_id, p)
            s.refresh(call, with_for_update=True)
            note = service._by_key(s, p, "notes", call_id)
            memory = service._by_key(s, p, "conversation_states", call_id)
            if not memory or memory.data.get("schema_version") != 2:
                fail(409, "Conversation migration required before reviewing")
            if not service._coach_authority(s, p, call):
                fail(403, "Conversation review is no longer authorized")
            state = upgrade_state(memory.data, call_id=call_id)
            observation = next(
                (v for v in state["observations"] if v["id"] == observation_id and v["kind"] == "approach"),
                None,
            )
            if not observation:
                fail(404, "Observation not found")
            if observation["version"] != body.expected_version:
                fail(409, "Observation changed; refresh before reviewing")
            previous = lineage_metadata(observation)
            observation.update(
                state="confirmed" if body.decision == "confirm" else "retracted",
                review_status="reviewed",
                epistemic_status="human_confirmed",
                reviewer_id=p["user_id"],
                reviewed_at=service.clock().isoformat(),
                available_at=service.clock().isoformat(),
                version=observation["version"] + 1,
            )
            state["version"] += 1
            state["finalization"]["status"] = "pending"
            state = invalidate_state(
                state,
                observation_id,
                observation["version"],
                at=service.clock(),
                lineage=service._conversation_lineage(s, p, call_id),
                source_kind="observation",
            )
            state["_lineage_updates"].extend(
                [
                    {
                        **previous,
                        "invalidated_at": service.clock().isoformat(),
                        "review_status": "unavailable",
                    },
                    lineage_metadata(observation),
                ]
            )
            service._store_conversation(s, p, call, state, memory)
            invalidate_commitments(
                service,
                s,
                p,
                call,
                observation_ids={i["id"] for i in state["_lineage_updates"] if i.get("invalidated_at")},
            )
            lead = service.required(s, "leads", call.data["lead_id"], p)
            invalidate_report(service, s, p, lead.data["campaign_id"])
            service.refresh_packet_in(s, p, lead)
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "conversation.observation_reviewed",
                observation_id,
                service.clock(),
                {"reason": body.reason, "decision": body.decision, "call_id": call_id},
            )
            emit(s, p["tenant_id"], "analysis.ready", note or memory, service.clock())
            return learning_observations([observation], service._conversation_lineage(s, p, call_id))[0]

    @app.get("/v1/optimization")
    def report(epoch: str | None = None, p=Depends(principal)):
        return service.cached_optimization_report(p, epoch=epoch)

    @app.post("/v1/admin/optimization/comparisons")
    def start_comparison(body: Comparison, p=Depends(administrator)):
        from datetime import timedelta

        with service.db.transaction() as s:
            campaign = service.scoped(s, "campaigns", p)[0]
            s.refresh(campaign, with_for_update=True)
            config = service.optimization_config(s, p)
            studies = campaign.data.get("controlled_comparisons", [])
            from app.coaching.sources import digest

            request = body.model_dump(mode="json")
            prior = next(
                (r for r in studies if body.request_id and r.get("request_id") == str(body.request_id)), None
            )
            if prior:
                if prior.get("request_digest") != digest(request):
                    fail(409, "Comparison request identity was already used with different content")
                return {k: v for k, v in prior.items() if k != "population"}
            extra = {}
            if body.strategy_ids:
                catalog = {r["id"]: r for r in service.strategy_catalog(s, p)}
                if any(i not in catalog for i in body.strategy_ids):
                    fail(422, "Every approach must have a current seller-reviewed example")
                extra["strategies"] = [
                    {k: catalog[i][k] for k in ("id", "source_version")} for i in body.strategy_ids
                ]
                if body.strategy_policy == "adaptive":
                    _, _, _, report_key = service.report_scope(s, p)
                    saved = service._by_key(s, p, "report_projections", report_key)
                    evidence = next(
                        (
                            r
                            for r in (saved.data["report"].get("comparisons", []) if saved else [])
                            if r["id"] == body.evidence_comparison_id
                        ),
                        None,
                    )
                    if (
                        not evidence
                        or not evidence.get("sufficient_evidence")
                        or evidence.get("strategy_policy") != "two_arm"
                        or evidence.get("strategy_ids") != body.strategy_ids
                        or evidence.get("strategies") != extra["strategies"]
                        or evidence.get("goal") != config["conversion_goal"]
                        or evidence.get("window_days") != config["window_days"]
                        or saved.data["source_revision"] != service.report_revision(s, p)
                        or timestamp(saved.data["expires_at"]) <= service.clock()
                    ):
                        fail(
                            409,
                            "Adaptive strategies need fresh, matured, supported reviewed comparison evidence for this exact set",
                        )
                    extra.update(
                        initial_strategy_policy=evidence["strategy_policy_snapshot"], report_key=report_key
                    )
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
                **request,
                **extra,
                "request_digest": digest(request),
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

    @app.get("/v1/optimization/strategies")
    def strategies(p=Depends(principal)):
        with service.db.transaction(False) as s:
            return {"strategies": service.strategy_catalog(s, p)}

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

    @app.post("/v1/admin/optimization/preview")
    def preview(body: OptimizationSettings, p=Depends(administrator)):
        with service.db.transaction(False) as s:
            campaign = service.scoped(s, "campaigns", p)[0]
            current = service.optimization_config(s, p, campaign.id)
            result, _ = goal_plan(service, s, p, campaign, body.conversion_goal or current["conversion_goal"])
            return {
                **result,
                "from_window_days": current["window_days"],
                "to_window_days": body.window_days or current["window_days"],
            }

    @app.put("/v1/admin/optimization")
    def configure(body: OptimizationSettings, p=Depends(administrator)):
        values = body.model_dump(exclude_none=True, exclude={"expected_campaign_version"})
        if "account_channel_limit" in body.model_fields_set:
            values["account_channel_limit"] = body.account_channel_limit
        if body.staffed_windows is not None:
            values["staffed_windows"] = [
                {
                    "start_at": w.start_at.isoformat(),
                    "end_at": w.end_at.isoformat(),
                    "reps": w.reps,
                    **({"rep_ids": w.rep_ids} if w.rep_ids is not None else {}),
                    **({"pool_id": w.pool_id} if w.pool_id is not None else {}),
                }
                for w in sorted(body.staffed_windows, key=lambda w: w.start_at)
            ]
        with service.db.transaction() as s:
            service.db.authorize_stream(s, p)
            campaign = service.scoped(s, "campaigns", p)[0]
            if (
                body.expected_campaign_version is not None
                and campaign.version != body.expected_campaign_version
            ):
                fail(409, "Campaign changed; preview the current definition before saving")
            if body.staffed_windows is not None:
                from sqlalchemy import select

                from app.db import MODELS

                roster = {rep for w in body.staffed_windows for rep in (w.rep_ids or [])}
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
                if available != roster:
                    fail(422, "Named staffing requires current active reps in this workspace")
            previous = service.optimization_config(s, p)
            if body.estimate_policy is not None:
                values["estimate_policy"] = EstimatePolicy.model_validate(
                    {**previous["estimate_policy"], **body.estimate_policy.model_dump(exclude_unset=True)}
                ).model_dump(mode="json")
            merged = {**previous, **values}
            if merged["staffing_start_hour"] >= merged["staffing_end_hour"]:
                fail(422, "The staffing end must follow its start; split an overnight shift into two periods")
            definition_changed = any(
                key in values and values[key] != previous[key] for key in ("conversion_goal", "window_days")
            )
            if definition_changed:
                service.ensure_experiment_cohort(s, p)
                summary, changes = goal_plan(
                    service, s, p, campaign, values.get("conversion_goal", previous["conversion_goal"])
                )
                for lead, state in changes:
                    if any(lead.data.get(key) != value for key, value in state.items()):
                        update(lead, **state)
                        service.refresh_packet_in(s, p, lead)
                        emit(s, p["tenant_id"], "lead.goal_updated", lead, service.clock())
                update(
                    campaign,
                    goal_history=[
                        *campaign.data.get("goal_history", []),
                        {
                            "epoch": campaign.data.get("optimization_epoch", "conversion-v2"),
                            "definition": previous,
                            "changed_at": service.clock().isoformat(),
                            "summary": summary,
                        },
                    ],
                    selection_profile={},
                )
            update(campaign, optimization={**previous, **values})
            if definition_changed:
                update(campaign, optimization_epoch=uid(), optimization_trial=None)
                for candidate in service.scoped(s, "model_versions", p):
                    if candidate.data.get("trial_active") or candidate.data.get("active"):
                        update(
                            candidate,
                            trial_active=False,
                            active=False,
                            deactivation_reason="Conversion definition changed",
                        )
                service.ensure_experiment_cohort(s, p)
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
        return service.request_optimization_refresh(p)

    @app.post("/v1/admin/optimization/train")
    def train(body: Empty, p=Depends(administrator)):
        return service.train_operational(p)

    @app.post("/v1/admin/models/{model_id}/start-trial")
    def start_trial(model_id: str, body: ModelTrial, p=Depends(administrator)):
        from datetime import timedelta

        from app.ml.training import predict_funnel, retired_reason
        from app.optimization.learning import TRIAL_CONTROL_FRACTION

        with service.db.transaction() as s:
            model = service.required(s, "model_versions", model_id, p)
            config = service.optimization_config(s, p)
            metrics = model.data.get("metrics", {})
            from app.db import get

            source = get(s, "learning_datasets", model.data.get("source_dataset_id"), p["tenant_id"])
            if (
                model.data.get("active")
                or model.data.get("goal") != config["conversion_goal"]
                or not model.data.get("source_dataset_id")
                or not source
                or source.seller_id != p["seller_id"]
                or source.data.get("goal") != config["conversion_goal"]
                or (
                    (
                        not model.data.get("reviewable")
                        or not service.model_source_current(s, p, model, config)
                    )
                    if model.data.get("feature_schema_version", 1) == 2
                    else metrics.get("brier", 1) >= metrics.get("pooled_baseline_brier", 0)
                )
                or not predict_funnel(model.data, {}, service.settings.data_dir / "models")
            ):
                fail(
                    409,
                    retired_reason(model.data)
                    or "An owned verified candidate that beats the deployed pooled baseline is required",
                )
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
                "control_fraction": TRIAL_CONTROL_FRACTION,
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
            from app.optimization.conversations import invalidate_report

            invalidate_report(service, s, p, lead.data["campaign_id"])
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
            if result.data.get("retracted"):
                return doc(result)
            update(
                result,
                retracted=True,
                retraction_reason=body.reason,
                retracted_by=p["user_id"],
                retracted_at=service.clock().isoformat(),
            )
            service.reconcile_goal_status(s, p, lead)
            from app.optimization.conversations import invalidate_report

            invalidate_report(service, s, p, lead.data["campaign_id"])
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

    @app.get("/v1/admin/numbers/{number_id}/review")
    def number_evidence(number_id: str, lead_id: str | None = None, p=Depends(administrator)):
        return service.number_review_view(p, number_id, lead_id)

    @app.post("/v1/admin/numbers/{number_id}/review")
    def review_number(number_id: str, body: NumberReview, p=Depends(administrator)):
        data = body.model_dump(mode="json", exclude_none=True)
        with service.db.transaction() as s:
            service.db.authorize_stream(s, p)
            service._configuration_actor(s, p)
            number = service.required(s, "number_inventory", number_id, p)
            emergency = body.decision == "pause" and body.request_id is None and body.daily_limit is None
            identity = (
                None
                if emergency
                else service.local_edit_in(s, p, "number_review", number_id, number, data, "owner_id")
            )
            if identity and identity[2]:
                return {**doc(number), "receipt": identity[2]}
            if identity:
                service.check_edit_version(number, data)
            view = next(n for n in service.inventory_evidence(s, p, fast=True) if n["id"] == number_id)
            config = service.optimization_config(s, p)
            limit = body.daily_limit or number.data.get("daily_limit", 5)
            if body.decision in {"increase", "resume"}:
                if not number.data.get("identity_configured") or view["configuration_errors"]:
                    fail(409, "Confirmed owned identity and disabled recording are required")
                if not view["callback_evidence_current"]:
                    fail(409, "Current connected callback evidence is required")
                if view["health"]["status"] in {"blocked", "investigating"}:
                    fail(409, "Resolve the recorded health evidence before increasing or resuming")
                if limit > service.number_workload_ceiling(config):
                    fail(409, "Requested limit exceeds reviewed ceiling or available staff capacity")
                if body.decision == "increase" and limit <= number.data["daily_limit"]:
                    fail(422, "Increase must raise the existing limit")
                lifecycle = "healthy" if view["health"]["status"] == "observed" else "limited_real_use"
            elif body.decision == "pause":
                lifecycle = "paused_for_outbound"
            else:
                lifecycle = number.data["lifecycle"]
                if limit >= number.data.get("daily_limit", 5):
                    fail(422, "Reduction must lower the existing daily limit")
            evidence = {
                "decision": body.decision,
                "reference": body.review_reference,
                "previous_limit": number.data["daily_limit"],
                "new_limit": limit,
                "health_samples": view["health"]["samples"],
                "health_policy_version": view["health"].get("policy_version"),
                "health_as_of": view["health"].get("as_of"),
                "callback_proof": number.data.get("callback_proof"),
                "reviewed_at": service.clock().isoformat(),
                "actor_id": p["user_id"],
            }
            update(
                number,
                lifecycle=lifecycle,
                daily_limit=limit,
                inbound_retained=True,
                real_use_review_reference=body.review_reference,
                workload_review=evidence,
            )
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "number.evidence_reviewed",
                number.id,
                service.clock(),
                evidence,
            )
            emit(s, p["tenant_id"], "number.updated", number, service.clock())
            return {
                **doc(number),
                **({"receipt": service.local_edit_receipt(s, p, identity, number)} if identity else {}),
            }

    def feedback_card(s, p, call_id, card_id):
        call = service.required(s, "call_attempts", call_id, p)
        card = service.required(s, "coach_events", card_id, p)
        if card.data["call_id"] != call_id:
            fail(422, "Feedback must reference this call's card")
        if not service._coach_authority(s, p, call) or card.data.get("user_id") != p["user_id"]:
            fail(403, "Only the receiving rep may acknowledge or report using guidance")
        return card

    @app.get("/v1/calls/{call_id}/coach/feedback")
    def read_feedback(call_id: str, coach_event_id: str, p=Depends(principal)):
        from app.coaching.context import state_view
        from app.optimization.experiments import observed_strategy_uses

        with service.db.transaction(False) as s:
            feedback_card(s, p, call_id, coach_event_id)
            previous = service._by_key(s, p, "coaching_feedback", coach_event_id + ":" + p["user_id"])
            state = service._by_key(s, p, "conversation_states", call_id)
            view = (
                state_view(state.data, lineage=service._conversation_lineage(s, p, call_id)) if state else {}
            )
            return {
                **(doc(previous) if previous else {"version": 0, "used_by_rep": None}),
                "observed_in_speech": observed_strategy_uses(view.get("observations", []), coach_event_id),
            }

    @app.post("/v1/calls/{call_id}/coach/feedback")
    def feedback(call_id: str, body: Feedback, p=Depends(principal)):
        import hashlib
        import hmac
        import json

        from app.coaching import sources

        with service.db.transaction() as s:
            service.db.authorize_stream(s, p)
            card = feedback_card(s, p, call_id, body.coach_event_id)
            s.refresh(card, with_for_update=True)
            key = body.coach_event_id + ":" + p["user_id"]
            previous = service._by_key(s, p, "coaching_feedback", key)
            data = body.model_dump(mode="json", exclude_none=True)
            identity = None
            if body.displayed or body.used_by_rep is not None:
                if body.request_id is None:
                    fail(409, "client_upgrade_required: identified display/use acknowledgement required")
            if body.request_id:
                receipt_key = "edit:coach_feedback:" + str(body.request_id)
                digest = hmac.new(
                    service.db.identity_key,
                    json.dumps([call_id, p["user_id"], data], sort_keys=True).encode(),
                    hashlib.sha256,
                ).hexdigest()
                old = service._by_key(s, p, "outcomes", receipt_key)
                if old:
                    if old.data["digest"] != digest:
                        fail(409, "Feedback request identity already used for different content")
                    return doc(previous) if previous else {"version": 0, "used_by_rep": None}
                identity = (receipt_key, digest, None)
            if body.used_by_rep is not None and body.expected_version != (
                previous.version if previous else 0
            ):
                fail(409, "Feedback changed; reload before correcting rep use")
            exposure = dict(card.data.get("strategy_exposure", {}))
            if body.displayed and not exposure.get("displayed_at"):
                if (
                    not service.raw.get("help:" + p["tenant_id"] + ":" + card.id)
                    or timestamp(card.data["expires_at"]) <= service.clock()
                    or card.data.get("status") == "withdrawn"
                    or not sources.current(service, s, p, card.data.get("context_sources"))
                ):
                    fail(409, "Guidance expired or changed before display acknowledgement")
                exposure["displayed_at"] = service.clock().isoformat()
                exposure["display_evidence"] = "authenticated_client_render_ack"
                state = service._by_key(s, p, "conversation_states", call_id)
                if state:
                    ids = state.data.get("displayed_coach_event_ids", [])
                    update(state, displayed_coach_event_ids=[*ids, card.id][-8:])
            if body.used_by_rep is not None:
                exposure.update(used_by_rep=body.used_by_rep, use_reported_at=service.clock().isoformat())
            timing = {k: data[k] for k in ("delivery_ms", "render_ms", "event_to_render_ms") if k in data}
            if timing:
                exposure["timing"] = {**exposure.get("timing", {}), **timing}
            if exposure != card.data.get("strategy_exposure", {}):
                update(card, strategy_exposure=exposure)
            # Passive transport/render evidence never changes a human edit version.
            if not body.model_fields_set.intersection(
                {"used_by_rep", "useful", "grounded", "timely", "distracting", "note"}
            ):
                if identity:
                    service.local_edit_receipt(
                        s, p, identity, previous or card, object_version=previous.version if previous else 0
                    )
                return doc(previous) if previous else {"version": 0, "used_by_rep": None}
            values = {
                k: v
                for k, v in data.items()
                if k not in {"request_id", "expected_version", "displayed", *timing}
            }
            values.update(call_id=call_id, reviewer_id=p["user_id"], reviewed_at=service.clock().isoformat())
            if exposure.get("displayed_at"):
                values["displayed_at"] = exposure["displayed_at"]
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
            if identity:
                service.local_edit_receipt(s, p, identity, result)
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
        from app.coaching.engine import finish_shielded
        from app.coaching.replay import evaluate

        def authorized(s):
            user = service.required(s, "users", p["user_id"], p)
            if not service._coach_authority(s, p) or user.data.get("role") not in {"owner", "admin"}:
                fail(403, "Replay requires current administrator access")

        def still_authorized():
            with service.db.transaction(False) as s:
                authorized(s)

        class ReplayProvider:
            seller_approved = True

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
                ground_guidance(result, request, cached=True)
                return result

        # A replay never auto-selects a paid route: simulation/static evaluation is explicit.
        provider = (
            ReplayProvider()
            if service.settings.simulation
            else await asyncio.to_thread(service.configured_coach, p)
        )
        if not service.settings.simulation:
            provider = provider[0]
        if provider is None:
            fail(409, "Configure the live coach before running a provider replay")
        usage_object_id = "coach-replay:" + uid()

        class MeteredReplay:
            last_usage, last_request_id, last_provider, last_model = {}, None, None, None

            async def suggest(self, request, deadline_ms=1200):
                return await self._request("suggest", "coach_replay", request, deadline_ms)

            async def review(self, request, deadline_ms=1200):
                return await self._request("review", "coach_replay_review", request, deadline_ms)

            async def _request(self, method, kind, request, deadline_ms):
                from app.providers.inference import InferenceError

                request_key = "replay:" + uid()
                self.last_usage, self.last_request_id, self.last_provider, self.last_model = (
                    {},
                    None,
                    None,
                    None,
                )
                submitted = False
                try:
                    await asyncio.to_thread(still_authorized)
                    if not await service._reserve_coach(p, request_key, usage_object_id, kind):
                        raise InferenceError("Replay exceeds the optional inference budget")
                    await asyncio.to_thread(still_authorized)
                    provider.last_usage, provider.last_request_id = {}, None
                    submitted = True
                    return await getattr(provider, method)(request, deadline_ms)
                finally:
                    if submitted:
                        self.last_usage = getattr(provider, "last_usage", {})
                        self.last_request_id = getattr(provider, "last_request_id", None)
                        self.last_provider = getattr(provider, "last_provider", None)
                        self.last_model = getattr(provider, "last_model", None) or getattr(
                            provider, "model", None
                        )
                    await finish_shielded(
                        service._settle_coach(
                            p, request_key, usage_object_id, kind, "openrouter", self, submitted
                        )
                    )

        report = await evaluate(
            body.cases,
            provider if service.settings.simulation else MeteredReplay(),
            model="approved_static" if service.settings.simulation else provider.model,
        )

        def publish():
            with service.db.transaction() as s:
                authorized(s)
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

        return await asyncio.to_thread(publish)
