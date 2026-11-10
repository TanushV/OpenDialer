from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app import ops
from app.optimization.goals import goal_plan
from app.optimization.numbering import HealthPolicy, timestamp
from app.platform.db import audit, doc, emit, put, uid, update
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
    conversion_goal: (
        Literal["qualified_conversation", "meeting_booked", "meeting_held", "sale_won"] | None
    ) = None
    window_days: int | None = Field(default=None, ge=1, le=180)
    exploration: float | None = Field(default=None, ge=0, le=0.3)
    auto_train: bool | None = None
    horizon_hours: int | None = Field(default=None, ge=1, le=168)
    rep_minutes: float | None = Field(default=None, ge=0, le=100800)
    handling_seconds: float | None = Field(default=None, ge=10, le=7200)
    daily_capacity_per_number: int | None = Field(default=None, ge=1, le=1000)
    training_min_rows: int | None = Field(default=None, ge=20, le=20000)
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
    kind: Literal["qualified_conversation", "meeting_booked", "meeting_held", "sale_won"]
    call_id: str | None = None
    occurred_at: datetime
    source_reference: str = Field(min_length=3, max_length=1000)
    value_usd: float | None = Field(default=None, ge=0)


class Retraction(Strict):
    reason: str = Field(min_length=3, max_length=1000)


class Empty(Strict):
    pass


class ModelTrial(Strict):
    review_reference: str = Field(min_length=3, max_length=1000)
    max_leads: int = Field(default=200, ge=20, le=10000)
    duration_days: int = Field(default=14, ge=1, le=90)


def register_optimization(app, service, principal, administrator):

    @app.get("/v1/optimization")
    def report(p=Depends(principal)):
        return ops.learning_report(service, p)

    @app.post("/v1/admin/optimization/preview")
    def preview(body: OptimizationSettings, p=Depends(administrator)):
        with service.db.transaction(False) as s:
            campaign = ops.scoped(service, s, "campaigns", p)[0]
            current = ops.optimization_config(service, s, p, campaign.id)
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
            campaign = ops.scoped(service, s, "campaigns", p)[0]
            if (
                body.expected_campaign_version is not None
                and campaign.version != body.expected_campaign_version
            ):
                fail(409, "Campaign changed; preview the current definition before saving")
            if body.staffed_windows is not None:
                from sqlalchemy import select

                from app.platform.db import MODELS

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
                }
                if available != roster:
                    fail(422, "Named staffing requires current active reps in this workspace")
            previous = ops.optimization_config(service, s, p)
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
                ops.ensure_experiment_cohort(service, s, p)
                summary, changes = goal_plan(
                    service, s, p, campaign, values.get("conversion_goal", previous["conversion_goal"])
                )
                for lead, state in changes:
                    if any(lead.data.get(key) != value for key, value in state.items()):
                        update(lead, **state)
                        ops.refresh_packet_in(service, s, p, lead)
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
                for candidate in ops.scoped(service, s, "model_versions", p):
                    if candidate.data.get("trial_active") or candidate.data.get("active"):
                        update(
                            candidate,
                            trial_active=False,
                            active=False,
                            deactivation_reason="Conversion definition changed",
                        )
                ops.ensure_experiment_cohort(service, s, p)
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
            return ops.optimization_config(service, s, p)

    @app.post("/v1/admin/optimization/refresh")
    def refresh(p=Depends(administrator)):
        return ops.request_optimization_refresh(service, p)

    @app.get("/v1/leads/{lead_id}/conversions")
    def list_conversions(lead_id: str, p=Depends(principal)):
        with service.db.transaction(False) as s:
            ops.required(service, s, "leads", lead_id, p)
            return [
                doc(r) for r in ops.scoped(service, s, "conversion_events", p) if r.data["lead_id"] == lead_id
            ]

    @app.post("/v1/leads/{lead_id}/conversions")
    def conversion(lead_id: str, body: Conversion, p=Depends(principal)):
        now = service.clock()
        if body.occurred_at.tzinfo is None or body.occurred_at > now:
            fail(422, "A real observed UTC/timezone-aware conversion time no later than now is required")
        with service.db.transaction() as s:
            lead = ops.required(service, s, "leads", lead_id, p)
            if body.call_id:
                call = ops.required(service, s, "call_attempts", body.call_id, p)
                if call.data["lead_id"] != lead.id or body.occurred_at < timestamp(call.data["started_at"]):
                    fail(422, "Conversion must match the lead and occur after the attributed attempt")
            data = {**body.model_dump(mode="json"), "lead_id": lead.id}
            previous = next(
                (
                    r
                    for r in ops.scoped(service, s, "conversion_events", p)
                    if r.external_key == body.event_id
                ),
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
            ops.reconcile_goal_status(service, s, p, lead)
            from app.optimization.conversations import invalidate_report

            invalidate_report(service, s, p, lead.data["campaign_id"])
            audit(s, p["tenant_id"], p["seller_id"], p["user_id"], "conversion.recorded", result.id, now)
            emit(s, p["tenant_id"], "conversion.recorded", lead, now, {"kind": body.kind})
            return doc(result)

    @app.post("/v1/leads/{lead_id}/conversions/{event_id}/retract")
    def retract_conversion(lead_id: str, event_id: str, body: Retraction, p=Depends(principal)):
        with service.db.transaction() as s:
            lead = ops.required(service, s, "leads", lead_id, p)
            result = ops.required(service, s, "conversion_events", event_id, p)
            if result.data["lead_id"] != lead.id:
                fail(404, "Conversion not found")
            if result.data.get("retracted"):
                return doc(result)
            update(
                result,
                retracted=True,
                retraction_reason=body.reason,
                retracted_by=p["user_id"],
                retracted_at=service.clock().isoformat(),
            )
            ops.reconcile_goal_status(service, s, p, lead)
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

    @app.get("/v1/admin/numbers/{number_id}/health")
    def number_health(number_id: str, p=Depends(administrator)):
        return ops.number_health(service, p, number_id)

    @app.post("/v1/admin/numbers/{number_id}/resume")
    def resume_number(number_id: str, p=Depends(administrator)):
        return ops.resume_number(service, p, number_id)
