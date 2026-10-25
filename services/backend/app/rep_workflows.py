"""Local commitments and rep-reported outcomes. No external scheduling effects."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from app.db import MODELS, audit, doc, emit, get, put, uid, update
from app.optimization.goals import review_token
from app.postcall.commitments import ACTIVE as ACTIVE_TASKS
from app.service import dt, fail
from app.telephony.reducer import TERMINAL

# An AI-suggested callback time defers automatic outreach this long at most; only a
# rep-stated callback holds outreach until the rep confirms a time.
SUGGESTION_HOLD = timedelta(days=7)
GOAL_OUTCOMES = {"qualified_conversation", "meeting_held", "sale_won"}
PROMISE_STATUS = {
    "open": "confirmed",
    "snoozed": "confirmed",
    "needs_confirmation": "needs_confirmation",
    "completed": "completed",
    "cancelled": "cancelled",
}


def zoned_time(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Include a UTC offset; an unzoned time is ambiguous")
    return parsed.astimezone(UTC).isoformat()


class DatedEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1, strict=True)
    due_at: str | None = None
    timezone: str | None = None

    @field_validator("due_at")
    @classmethod
    def validate_time(cls, value):
        return zoned_time(value)

    @field_validator("timezone")
    @classmethod
    def validate_zone(cls, value):
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError("An IANA timezone is required") from exc
        return value


class TaskEdit(DatedEdit):
    # Derived from due_at's own offset (never client-set) and checked against the task zone.
    due_offset_minutes: int | None = None
    status: Literal["open", "snoozed", "needs_confirmation", "completed", "cancelled"] | None = None
    owner_id: str | None = None
    note: str | None = Field(default=None, max_length=1000)
    confirmation_state: Literal["suggested", "confirmed", "needs_review"] | None = None
    confirmation_reason: str | None = Field(default=None, min_length=3, max_length=500)
    snoozed_until: str | None = None
    window_end: str | None = None

    @field_validator("snoozed_until", "window_end")
    @classmethod
    def validate_reminder(cls, value):
        return zoned_time(value)

    @model_validator(mode="before")
    @classmethod
    def record_offset(cls, data):
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k != "due_offset_minutes"}
            try:
                parsed = datetime.fromisoformat(data["due_at"].replace("Z", "+00:00"))
            except (KeyError, AttributeError, TypeError, ValueError):
                return data
            if parsed.utcoffset() is not None:
                data["due_offset_minutes"] = int(parsed.utcoffset().total_seconds() // 60)
        return data

    @field_validator("status")
    @classmethod
    def status_cannot_be_null(cls, value):
        if value is None:
            raise ValueError("Task status cannot be null")
        return value


class MeetingEdit(DatedEdit):
    expected_review_token: str | None = Field(default=None, min_length=64, max_length=64)
    status: Literal["booked", "held", "no_show", "cancelled", "rescheduled"]
    next_action: Literal["keep_hold", "resume", "callback"] | None = None
    callback_at: str | None = None
    reference: str = Field(default="", max_length=300)

    @field_validator("callback_at")
    @classmethod
    def validate_callback(cls, value):
        return zoned_time(value)


class OutreachEdit(DatedEdit):
    expected_review_token: str | None = Field(default=None, min_length=64, max_length=64)
    next_action: Literal["resume", "callback"]
    reason: str = Field(min_length=3, max_length=500)


class RepWorkflows:
    def local_edit_in(self, s, p, kind, requested_id, row, data, owner_field):
        """Authorize before replay; the receipt is metadata, never a historical object view."""
        self.db.authorize_stream(s, p)
        s.refresh(row, with_for_update=True)
        owner_id = row.data.get(owner_field)
        if not self._coach_authority(s, p):
            fail(403, "Current account or session cannot edit this record")
        user = self.required(s, "users", p["user_id"], p)
        if "rep" in {p["role"], user.data["role"]} and owner_id not in (None, p["user_id"]):
            fail(403, "This record belongs to another rep")
        if data.get("request_id") is None or data.get("expected_version") is None:
            fail(
                409,
                {
                    "code": "client_upgrade_required",
                    "message": "Update the client to save identified, versioned edits",
                },
            )
        try:
            request_id = str(UUID(str(data["request_id"])))
        except (ValueError, TypeError, AttributeError):
            fail(422, "request_id must be a UUID")
        if type(data["expected_version"]) is not int or data["expected_version"] < 1:
            fail(422, "expected_version must be a positive integer")
        key = f"edit:{kind}:{request_id}"
        digest = hashlib.sha256(
            json.dumps(
                [requested_id, p["user_id"], data], sort_keys=True, separators=(",", ":"), default=str
            ).encode()
        ).hexdigest()
        receipt = self._by_key(s, p, "outcomes", key)
        if receipt:
            if receipt.data["digest"] != digest or receipt.data["object_id"] != row.id:
                fail(409, "Request identity was already used for a different edit")
            return key, digest, receipt.data["receipt"]
        return key, digest, None

    def local_edit_receipt(self, s, p, identity, row, *, object_version=None):
        key, digest, _ = identity
        receipt = {
            "request_id": key.rsplit(":", 1)[1],
            "object_id": row.id,
            "version": row.version if object_version is None else object_version,
            "at": self.clock().isoformat(),
        }
        put(
            s,
            "outcomes",
            p["tenant_id"],
            p["seller_id"],
            {
                "kind": "edit_receipt",
                "object_id": row.id,
                "digest": digest,
                "actor_id": p["user_id"],
                "receipt": receipt,
            },
            key=key,
            now=self.clock(),
        )
        return receipt

    def check_edit_version(self, row, data):
        if row.version != data["expected_version"]:
            fail(
                409,
                {
                    "code": "version_conflict",
                    "current_version": row.version,
                    "message": "Record changed; reload before saving",
                },
            )

    def check_prospect_review(self, s, p, lead, data):
        token = data.get("expected_review_token")
        if token:
            campaign = self.required(s, "campaigns", lead.data["campaign_id"], p)
            matches = token == review_token(lead, campaign)
        else:
            matches = lead.version == data["expected_version"]
        if not matches:
            fail(409, "Prospect context changed; refresh before saving your review")

    def resolve_goal_review(self, p, lead_id, data):
        with self.db.transaction() as s:
            lead = self.required(s, "leads", lead_id, p)
            identity = self.local_edit_in(s, p, "outreach", lead_id, lead, data, "rep_id")
            if identity[2]:
                return {**doc(lead), "edit_receipt": identity[2]}
            self.check_prospect_review(s, p, lead, data)
            if lead.data.get("outreach_hold") != "goal_changed":
                fail(409, "Refresh the prospect; only a goal-change review can be resolved here")
            if p["role"] == "rep" and lead.data.get("rep_id") not in (None, p["user_id"]):
                fail(403, "This prospect belongs to another rep")
            if self.suppressed(s, p, lead.data["phone"]):
                fail(403, "An opt-out cannot be cleared by a next-action review")
            if data["next_action"] == "callback":
                if not data.get("due_at") or not data.get("timezone"):
                    fail(422, "Confirm a callback time and timezone")
                self._callback_task(
                    s,
                    p,
                    lead,
                    key="goal-callback:" + lead.id,
                    call_id=None,
                    owner_id=p["user_id"],
                    due_at=data["due_at"],
                    timezone=data["timezone"],
                    note=data["reason"],
                    explicit_replacement=True,
                )
            update(lead, outreach_hold=None)
            self.refresh_packet_in(s, p, lead)
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "lead.goal_reviewed",
                lead.id,
                self.clock(),
                {"next_action": data["next_action"], "reason": data["reason"]},
            )
            emit(s, p["tenant_id"], "lead.goal_updated", lead, self.clock())
            return {**doc(lead), "edit_receipt": self.local_edit_receipt(s, p, identity, lead)}

    def prospect_context_in(self, s, p, lead, calls, commitments, tasks, *, decision=None):
        from app.optimization.conversations import active_observations
        from app.optimization.learning import freeze_features
        from app.scheduling.models import saved_prediction

        campaign = self.required(s, "campaigns", lead.data["campaign_id"], p)
        config = self.optimization_config(s, p, campaign.id)
        features = freeze_features(doc(lead), health={"score": None, "samples": 0}, now=self.clock())
        profile = campaign.data.get("selection_profile", {})
        if profile.get("goal") != config["conversion_goal"]:
            profile = {}
        prediction = saved_prediction(profile, features, config)
        if decision:
            prediction = {
                **decision["predictions"],
                "as_of": decision.get("decision_at") or decision.get("as_of"),
            }
            config = {
                **config,
                "conversion_goal": decision.get("goal", config["conversion_goal"]),
                "estimate_policy": decision.get("estimate_policy"),
                "handling_seconds": decision.get("work_assumption_seconds"),
            }
        recent = sorted(calls, key=lambda c: c.get("started_at", ""))[-10:]
        model = MODELS["notes"]
        notes = (
            list(
                s.scalars(
                    select(model)
                    .where(
                        model.tenant_id == p["tenant_id"],
                        model.seller_id == p["seller_id"],
                        model.external_key.in_([c["id"] for c in recent]),
                    )
                    .order_by(model.created_at.desc())
                )
            )
            if recent
            else []
        )
        note_views = [self.note_view_in(s, p, n) for n in notes]
        current = next((n["notes"] for n in note_views if not n.get("stale")), {})
        memories = [(n["call_id"], n["call_memory"]) for n in note_views if n.get("call_memory")]
        answers = [i for _, m in memories for i in m.get("answered_questions", [])][:12]
        questions = [
            i
            for _, m in memories
            for i in m.get("open_questions", [])
            if i.get("status") not in {"resolved", "retracted", "stale"}
        ][:12]
        gaps = [
            {"call_id": identity, **m.get("finalization", {}), "coverage": m.get("coverage", {})}
            for identity, m in memories
            if m.get("finalization", {}).get("status") != "ready"
            or m.get("coverage", {}).get("unavailable_segments")
        ]
        preparation = self.preparation_state(s, p, lead)
        if lead.data.get("outreach_hold"):
            why = "Outreach paused: " + lead.data["outreach_hold"].replace("_", " ")
        elif lead.data.get("callback_due_at"):
            why = "A requested callback takes priority at the confirmed time."
        elif lead.data.get("requested_contact"):
            why = "This prospect requested contact."
        elif dt(lead.data.get("coverage_due_at")) and dt(lead.data["coverage_due_at"]) <= self.clock():
            why = "This lead is due for coverage; a low prediction does not exclude it."
        else:
            why = "Eligible work is ranked by pooled outcome estimates within calling windows."
        samples = int(prediction.get("observations", 0))
        return {
            "review_token": review_token(lead, campaign),
            "tasks": tasks,
            "prospect_brief": {
                "version": lead.version,
                "evidence_status": "sparse" if not recent else "observed",
                "summary": current.get("summary") or "No prior interpreted conversation is available.",
                "needs": current.get("problems", []),
                "unresolved": current.get("objections", []),
                "unknowns": current.get("unknowns", []),
                "prior_answers": answers,
                "unanswered_questions": questions,
                "interpretation_gaps": gaps,
                "source_versions": [
                    {
                        "call_id": identity,
                        "version": m["version"],
                        "transcript_version": m.get("transcript_version"),
                    }
                    for identity, m in memories
                ],
                "freshness": {
                    "state": preparation["status"],
                    "preparation_revision": lead.data.get("preparation_revision", 0),
                },
                "observations": active_observations(
                    [item for note in note_views for item in note.get("observations", [])], self.clock()
                )[:12],
                "commitments": commitments,
                "human_notes": [
                    {"call_id": n.data["call_id"], "text": n.data["human_text"]}
                    for n in notes
                    if n.data.get("human_text")
                ],
                "last_outcome": lead.data.get("last_outcome"),
                "last_outcome_at": lead.data.get("last_outcome_at"),
                "meeting": lead.data.get("meeting"),
                "next_question": "Confirm the agreed next step."
                if any(t.get("status") in ACTIVE_TASKS for t in tasks)
                else questions[0]["text"]
                if questions
                else "What would make this conversation useful for you?",
                "raw_evidence": "Only current five-minute chunks are available; this brief retains derived notes.",
            },
            "statistical_guidance": {
                "human_probability": prediction["p_human"],
                "right_party_given_human": prediction["p_right_party_given_human"],
                "conversion_given_right_party": prediction["p_conversion_given_right_party"],
                "conversion_goal": config["conversion_goal"],
                "samples": samples,
                "conversion_samples": prediction.get("conversion_observations", 0),
                "stage_support": prediction.get("stage_support", {}),
                "prior_only": prediction.get("prior_only", False),
                "estimate_level": prediction.get("estimate_level"),
                "estimate_policy": config["estimate_policy"],
                "estimate_policy_version": prediction.get("estimate_policy_version"),
                "work_assumption_seconds": config["handling_seconds"],
                "estimate_basis": "pooled"
                if prediction["model_version"].startswith("pooled-")
                else "reviewed_model",
                "uncertainty": "Reviewed model estimates; history counts describe the pooled comparison, not confidence about this prospect."
                if not prediction["model_version"].startswith("pooled-")
                else "Sparse evidence; pooled starting estimates, not individual guarantees."
                if samples < 30
                else "Pooled estimates; conditional outcomes have separate sample support.",
                "model_version": prediction["model_version"],
                "as_of": prediction.get("as_of"),
                "source": decision["source"] if decision else "current_pooled_estimate",
                "decision_id": decision["id"] if decision else None,
                "why_now": why,
            },
        }

    def lead_records(self, s, p, kind, lead_id, *conditions, limit=None, newest=False):
        model = MODELS[kind]
        if kind == "followup_tasks":
            from sqlalchemy import or_

            conditions = (*conditions, or_(model.op_status != "aliased", model.op_status.is_(None)))
        return list(
            s.scalars(
                select(model)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.op_lead_id == lead_id,
                    *conditions,
                )
                .order_by(
                    model.op_at.desc() if newest else model.op_at, model.id.desc() if newest else model.id
                )
                .limit(limit)
            )
        )

    def refresh_packet_in(self, s, p, lead):
        """Mark the brief dirty and queue its refresh; features and goal state are
        recomputed by that background job (refresh_continuity), not in the caller's save."""
        update(lead, preparation_revision=lead.data.get("preparation_revision", 0) + 1)
        self.queue_packet_refresh_in(s, p, lead)
        emit(s, p["tenant_id"], "packet.stale", lead, self.clock())

    def queue_packet_refresh_in(self, s, p, lead):
        self.jobs.coalesce_live_in(
            s,
            "scripts",
            {
                "kind": "packet_refresh",
                "lead_ids": [lead.id],
                "preparation_revision": lead.data.get("preparation_revision", 0),
                "seller_id": p["seller_id"],
                "user_id": p["user_id"],
            },
            f"continuity:{p['seller_id']}:{lead.id}",
            tenant_id=p["tenant_id"],
            priority=5,
            available_at=self.clock(),
        )

    def refresh_continuity(self, p, lead_ids):
        """Background half of a packet refresh: lead features and goal state.

        A rep's save only marks the brief dirty; this recomputation over the lead's
        history runs in the packet_refresh job, one short write per lead.
        """
        for lead_id in dict.fromkeys(lead_ids):
            with self.db.transaction() as s:
                lead = self.required(s, "leads", lead_id, p)
                s.refresh(lead, with_for_update=True)
                self.refresh_conversation_features_in(s, p, lead)
                self.reconcile_goal_status(s, p, lead)

    def refresh_conversation_features_in(self, s, p, lead):
        from app.optimization.conversations import prior_features

        history = self.lead_records(
            s,
            p,
            "call_attempts",
            lead.id,
            MODELS["call_attempts"].op_status.in_(TERMINAL),
            limit=21,
            newest=True,
        )
        calls = [doc(r) for r in history[:20]]
        model = MODELS["conversation_states"]
        memories = (
            {
                r.external_key: r.data
                for r in s.scalars(
                    select(model).where(
                        model.tenant_id == p["tenant_id"],
                        model.seller_id == p["seller_id"],
                        model.external_key.in_([r["id"] for r in calls]),
                    )
                )
            }
            if calls
            else {}
        )
        campaign = self.required(s, "campaigns", lead.data["campaign_id"], p)
        features = prior_features(
            {
                **doc(lead),
                "conversation_feature_sources": [],
                "conversation_vocabulary": campaign.data.get("conversation_vocabulary", {"version": 1}),
            },
            [
                {**call, "observations": memories.get(call["id"], {}).get("observations", [])}
                for call in calls
            ],
            now=self.clock(),
        )
        source = {
            "conversation_feature_sources": features["conversation_sources"],
            "conversation_feature_scope": {
                "calls_inspected": len(calls),
                "older_calls_omitted": len(history) > 20,
                "observations_omitted": features["conversation_features_omitted"],
            },
        }
        if any(lead.data.get(k) != value for k, value in source.items()):
            update(lead, **source)

    def _sync_callback(self, s, p, lead, tasks=None):
        tasks = self.lead_records(s, p, "followup_tasks", lead.id) if tasks is None else tasks
        active = [t for t in tasks if t.data.get("kind") == "callback" and t.data["status"] in ACTIVE_TASKS]
        dated = [t for t in active if t.data.get("due_at") and t.data["status"] != "needs_confirmation"]
        next_task = min(dated, key=lambda t: dt(t.data["due_at"]), default=None)
        undated = [t for t in active if t not in dated]
        stated = [t for t in undated if t.data.get("source") != "inferred" or t.data.get("human_edited")]
        suggested = [
            dt(
                t.data.get("suggested_at")
                or t.created_at.replace(tzinfo=t.created_at.tzinfo or UTC).isoformat()
            )
            + SUGGESTION_HOLD
            for t in undated
            if t not in stated
        ]
        until = max([u for u in suggested if u > self.clock()], default=None)
        hold = lead.data.get("outreach_hold")
        if hold in {None, "callback_time_confirmation"}:
            hold = "callback_time_confirmation" if stated and not next_task else None
        previous = {lead.data.get("callback_due_at"), lead.data.get("callback_suggestion_until")} - {None}
        due = next_task.data["due_at"] if next_task else None
        deferral = until.isoformat() if until and not due and not hold else None
        next_at = lead.data.get("next_attempt_at")
        if due:
            next_at = due
        elif deferral:
            # A model suggestion defers automatic outreach for a bounded time only.
            next_at = max(dt(next_at) or self.clock(), until).isoformat()
        elif next_at in previous:
            next_at = self.clock().isoformat()
        update(
            lead,
            callback_due_at=due,
            callback_owner_id=next_task.data.get("owner_id") if next_task else None,
            callback_suggestion_until=deferral,
            next_attempt_at=next_at,
            outreach_hold=hold,
        )

    def _callback_by_key(self, s, p, lead, key, tasks=None):
        task = self._by_key(s, p, "followup_tasks", key)
        if task:
            return (
                self.required(s, "followup_tasks", task.data["alias_of"], p)
                if task.data.get("alias_of")
                else task
            )
        tasks = self.lead_records(s, p, "followup_tasks", lead.id) if tasks is None else tasks
        return next((t for t in tasks if t.data.get("wrapup_key") == key), None)

    def _callback_task(
        self,
        s,
        p,
        lead,
        *,
        key,
        call_id,
        owner_id,
        due_at,
        timezone,
        note="",
        source="rep_reported",
        explicit_replacement=False,
        tasks=None,
    ):
        """Returns (task, review). A rep's save never fails here: an ambiguous or
        protected existing callback keeps its values and is returned as needs_review."""
        tasks = self.lead_records(s, p, "followup_tasks", lead.id) if tasks is None else tasks
        task = self._callback_by_key(s, p, lead, key, tasks)
        suggestion = {"due_at": due_at, "timezone": timezone or lead.data.get("timezone"), "source": "wrapup"}
        if not task and call_id and key == "wrapup-callback:" + call_id:
            inferred = [
                t
                for t in tasks
                if t.data.get("call_id") == call_id
                and t.data.get("kind") == "callback"
                and t.data.get("source") == "inferred"
            ]
            if len(inferred) == 1:
                task = inferred[0]
                update(task, wrapup_key=key)
            elif len(inferred) > 1:
                ids = sorted(t.id for t in inferred)
                for other in inferred:
                    if other.data["status"] in ACTIVE_TASKS:
                        update(
                            other,
                            review_required=True,
                            confirmation_state="needs_review",
                            source_conflicts=[i for i in ids if i != other.id],
                            suggested_update=suggestion,
                        )
                        emit(s, p["tenant_id"], "task.updated", other, self.clock())
                return None, {
                    "status": "needs_review",
                    "reason": "This call already has several follow-ups; choose which one the callback updates.",
                    "task_ids": ids,
                }
        values = {
            "lead_id": lead.id,
            "call_id": call_id,
            "owner_id": owner_id,
            "kind": "callback",
            "action": "Call back" if due_at else "Confirm callback time",
            "due_at": due_at,
            "timezone": timezone or lead.data.get("timezone"),
            "note": note,
            "status": "open" if due_at else "needs_confirmation",
            "confirmation_state": "confirmed" if due_at else "suggested",
            "source": source,
        }
        review = None
        if task:
            # Repeated machine extraction or wrap-up cannot overwrite an independently edited task.
            if explicit_replacement or (
                not task.data.get("human_edited") and task.data.get("status") in ACTIVE_TASKS
            ):
                update(task, **values)
            elif task.data.get("due_at") != due_at:
                review = {
                    "status": "needs_review",
                    "reason": "A person already edited or closed this callback; review the new time.",
                    "task_ids": [task.id],
                }
                if task.data.get("status") in ACTIVE_TASKS:
                    update(
                        task,
                        review_required=True,
                        confirmation_state="needs_review",
                        suggested_update=suggestion,
                    )
        else:
            task = put(s, "followup_tasks", p["tenant_id"], p["seller_id"], values, key=key, now=self.clock())
            tasks.append(task)
        self._sync_callback(s, p, lead, tasks)
        emit(s, p["tenant_id"], "task.updated", task, self.clock())
        return task, review

    def edit_task(self, p, task_id, data):
        with self.db.transaction() as s:
            # The seller's authorization stream comes before any row lock, as in every writer;
            # taking it after the rows deadlocked against a stream holder waiting for the call.
            self.db.authorize_stream(s, p)
            task = self.required(s, "followup_tasks", task_id, p)
            if task.data.get("alias_of"):
                task = self.required(s, "followup_tasks", task.data["alias_of"], p)
            # Lock in the call writers' order (source call, lead, then the task and its
            # promise) so post-call work and packet refresh wait instead of going stale.
            if task.data.get("call_id"):
                get(s, "call_attempts", task.data["call_id"], p["tenant_id"], lock=True)
            if task.data.get("lead_id"):
                get(s, "leads", task.data["lead_id"], p["tenant_id"], lock=True)
            s.refresh(task, with_for_update=True)
            identity = self.local_edit_in(s, p, "task", task_id, task, data, "owner_id")
            if identity[2]:
                return {**doc(task), "edit_receipt": identity[2]}
            self.check_edit_version(task, data)
            if task.data.get("inbound") and task.data.get("inbound_state") not in {None, "ended"}:
                fail(409, "This return call is still active; finish call routing before editing the task")
            edits = {
                k: v
                for k, v in data.items()
                if k not in {"expected_version", "request_id", "due_offset_minutes"}
            }
            zone = edits.get("timezone") or task.data.get("timezone")
            if edits.get("due_at") and data.get("due_offset_minutes") and zone:
                # A non-UTC offset is a local reading; it must be the task zone's offset then.
                local = dt(edits["due_at"]).astimezone(ZoneInfo(zone)).utcoffset()
                if data["due_offset_minutes"] != int(local.total_seconds() // 60):
                    fail(422, "The time's UTC offset does not match the task timezone; choose the time again")
            if "owner_id" in edits:
                owner = self.required(s, "users", edits["owner_id"] or "", p)
                if owner.data.get("disabled"):
                    fail(422, "Task owner is disabled")
            candidate = {**task.data, **edits}
            if candidate["status"] in {"open", "snoozed"} and not candidate.get("due_at"):
                fail(422, "Confirm a dated time before opening or snoozing this task")
            if candidate.get("window_end") and (
                not candidate.get("due_at") or dt(candidate["window_end"]) <= dt(candidate["due_at"])
            ):
                fail(422, "Callback window must end after its start")
            if candidate["status"] == "snoozed" and (
                not candidate.get("snoozed_until") or dt(candidate["snoozed_until"]) <= self.clock()
            ):
                fail(422, "Choose a future reminder time without changing the promised callback")
            if edits.get("due_at") and task.data["status"] == "needs_confirmation" and "status" not in edits:
                edits["status"] = "open"
            resolving = edits.get("confirmation_state") == "confirmed" or edits.get("status") in {
                "completed",
                "cancelled",
            }
            conflict = task.data.get("review_required", False)
            if conflict and resolving and not edits.get("confirmation_reason"):
                fail(422, "Provide confirmation_reason to resolve changed source evidence")
            if not conflict or resolving:
                edits["confirmation_state"] = (
                    "suggested"
                    if edits.get("status", task.data["status"]) == "needs_confirmation"
                    else "confirmed"
                )
                edits["review_required"] = False
                edits["reviewed_source_version"] = task.data.get("source_version")
            if edits.get("status") in {"open", "completed", "cancelled"}:
                edits["snoozed_until"] = None
            update(
                task,
                **edits,
                human_edited=True,
                edited_at=self.clock().isoformat(),
                edited_by=p["user_id"],
            )
            if task.data.get("commitment_id"):
                # A person's decision on the task is the promise's state in every brief.
                commitment = self.required(s, "commitments", task.data["commitment_id"], p, lock=True)
                update(
                    commitment,
                    task_status=task.data["status"],
                    status=PROMISE_STATUS.get(task.data["status"], commitment.data.get("status")),
                )
            if task.data.get("lead_id"):
                lead = self.required(s, "leads", task.data["lead_id"], p)
                self._sync_callback(s, p, lead)
                self.refresh_packet_in(s, p, lead)
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "task.updated",
                task.id,
                self.clock(),
                {"fields": sorted(edits)},
            )
            emit(s, p["tenant_id"], "task.updated", task, self.clock())
            return {**doc(task), "edit_receipt": self.local_edit_receipt(s, p, identity, task)}

    def _meeting_in(
        self,
        s,
        p,
        lead,
        status,
        *,
        due_at=None,
        timezone=None,
        reference="",
        next_action="keep_hold",
        callback_at=None,
        source_call_id=None,
    ):
        if next_action == "callback" and not callback_at:
            fail(422, "A confirmed callback time is required")
        previous = lead.data.get("meeting", {})
        meeting = {
            "status": status,
            "source": "rep_reported",
            "source_call_id": source_call_id,
            "reported_at": self.clock().isoformat(),
            "reported_by": p["user_id"],
            "due_at": due_at or previous.get("due_at"),
            "timezone": timezone or previous.get("timezone") or lead.data.get("timezone"),
            "reference": reference or previous.get("reference", ""),
            "held": status == "held",
            "invitation_sent": False,
            "next_action": next_action,
        }
        hold = (
            "meeting_booked"
            if status in {"booked", "rescheduled"}
            else "meeting_next_action"
            if next_action == "keep_hold"
            else None
        )
        update(lead, meeting=meeting, outreach_hold=hold)
        if status in {"booked", "rescheduled", "held"}:
            for task in self.lead_records(s, p, "followup_tasks", lead.id):
                if task.data.get("kind") != "callback" or task.data.get("status") not in ACTIVE_TASKS:
                    continue
                if task.data.get("human_edited"):
                    # A person's own callback decision is reviewed, never cancelled implicitly.
                    update(
                        task,
                        review_required=True,
                        confirmation_state="needs_review",
                        source_conflicts=["A meeting was reported after this callback was edited"],
                    )
                else:
                    update(task, status="cancelled", reason="rep_reported_meeting", human_edited=True)
                emit(s, p["tenant_id"], "task.updated", task, self.clock())
        elif next_action == "callback":
            self._callback_task(
                s,
                p,
                lead,
                key="meeting-callback:" + lead.id,
                call_id=None,
                owner_id=p["user_id"],
                due_at=callback_at,
                timezone=timezone,
                explicit_replacement=True,
            )
        self._sync_callback(s, p, lead)
        # Explicit later meeting reports retain occurrence/observation provenance for learning.
        event = self._by_key(s, p, "conversion_events", "rep-meeting:" + lead.id)
        if status == "held":
            values = {
                "lead_id": lead.id,
                "kind": "meeting_held",
                "occurred_at": self.clock().isoformat(),
                "observed_at": self.clock().isoformat(),
                "label_available_at": self.clock().isoformat(),
                "source": "rep_reported",
                "retracted": False,
                "call_id": source_call_id,
            }
            if event:
                update(
                    event,
                    **values,
                    retracted_at=None,
                    history=[
                        *event.data.get("history", []),
                        {key: value for key, value in event.data.items() if key != "history"},
                    ],
                )
            else:
                put(
                    s,
                    "conversion_events",
                    p["tenant_id"],
                    p["seller_id"],
                    values,
                    key="rep-meeting:" + lead.id,
                    now=self.clock(),
                )
        elif event and not event.data.get("retracted"):
            update(event, retracted=True, retracted_at=self.clock().isoformat())
        self.reconcile_goal_status(s, p, lead)
        from app.optimization.conversations import invalidate_report

        invalidate_report(self, s, p, lead.data["campaign_id"])
        emit(s, p["tenant_id"], "lead.meeting_updated", lead, self.clock())

    def report_meeting(self, p, lead_id, data):
        with self.db.transaction() as s:
            lead = self.required(s, "leads", lead_id, p)
            identity = self.local_edit_in(s, p, "meeting", lead_id, lead, data, "rep_id")
            if not identity[2]:
                self.check_prospect_review(s, p, lead, data)
                if data["status"] in {"no_show", "cancelled"} and not data.get("next_action"):
                    fail(422, "Choose next_action: keep_hold, callback or resume")
                self._meeting_in(
                    s,
                    p,
                    lead,
                    data["status"],
                    **{
                        k: data[k]
                        for k in ("due_at", "timezone", "reference", "next_action", "callback_at")
                        if data.get(k) is not None
                    },
                )
                self.refresh_packet_in(s, p, lead)
                audit(
                    s,
                    p["tenant_id"],
                    p["seller_id"],
                    p["user_id"],
                    "meeting.reported",
                    lead.id,
                    self.clock(),
                    {"status": data["status"], "next_action": data.get("next_action", "keep_hold")},
                )
            event = self._by_key(s, p, "conversion_events", "rep-meeting:" + lead.id)
            return {
                **doc(lead),
                "edit_receipt": identity[2] or self.local_edit_receipt(s, p, identity, lead),
                "outcome_id": event.id if event else None,
                "packet_version": lead.data.get("preparation_revision", 0),
            }

    def auto_disposition(self, p, call_id):
        """Execute the bounded save-outcome tool without overwriting any human edit."""
        from fastapi import HTTPException

        from app.postcall.analysis import suggested_outcome

        with self.db.transaction(False) as s:
            call = self.required(s, "call_attempts", call_id, p)
            if (
                call.data["state"] not in TERMINAL
                or call.data.get("edit_version", 0)
                or call.data.get("outcome")
            ):
                return
            outcome = call.data.get("carrier_outcome")
            source, role = "carrier", "unknown"
            if not outcome:
                memory = self._by_key(s, p, "conversation_states", call.id)
                suggestion = suggested_outcome(memory.data if memory else {})
                if not suggestion or suggestion["code"] not in {
                    "gatekeeper",
                    "not_interested",
                    "right_party",
                    "screening",
                    "ivr",
                }:
                    return
                outcome, role, source = suggestion["code"], suggestion["contact_role"], "ai_reviewed"
            body = {
                "outcome": outcome,
                "contact_role": role,
                "request_id": uid(),
                "expected_version": call.version,
                "expected_edit_version": 0,
                "callback_note": "",
                "callback_at": None,
            }
        try:
            self.save_disposition(p, call_id, body, source=source)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise

    def save_disposition(self, p, call_id, data, *, source="rep_reported"):
        outcome = data["outcome"]
        due = zoned_time(data.get("callback_at"))
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)  # Stream first, then call, then lead.
            call = self.required(s, "call_attempts", call_id, p)
            # Serialize with telephony/maintenance writers so a concurrent bump cannot raise
            # StaleDataError mid-save; conflicts are decided by the rep edit counter below.
            s.refresh(call, with_for_update=True)
            identity = self.local_edit_in(s, p, "disposition", call_id, call, data, "rep_id")
            if call.data["state"] not in TERMINAL:
                fail(409, "End the call before wrap-up")
            lead = self.required(s, "leads", call.data["lead_id"], p, lock=True)  # Call, then lead.
            canonical = {
                "outcome": outcome,
                "callback_at": due,
                "callback_note": data.get("callback_note", ""),
                "contact_role": data.get("contact_role", "unknown"),
            }
            duplicate = (
                call.data.get("disposition_input") == canonical and call.data.get("outcome_source") == source
            )
            task = callback_review = tasks = None
            edit_version = call.data.get("edit_version", 0)
            if not identity[2]:
                if data.get("expected_edit_version") is not None:
                    if data["expected_edit_version"] != edit_version:
                        fail(
                            409,
                            {
                                "code": "version_conflict",
                                "current_version": call.version,
                                "current_edit_version": edit_version,
                                "message": "This call's outcome changed; reload before saving",
                            },
                        )
                else:
                    self.check_edit_version(call, data)  # Older clients: whole-row version.
            if source != "rep_reported" and (edit_version or call.data.get("outcome")):
                fail(409, "A human or previous tool already saved this outcome")
            previous_outcome = call.data.get("outcome")
            correcting_confirmed = call.data["state"] == "FINALIZED"
            correcting_meeting = (
                not identity[2]
                and not duplicate
                and previous_outcome in {"meeting_booked", "meeting_held"}
                and outcome not in {"meeting_booked", "meeting_held"}
            )
            meeting = lead.data.get("meeting", {})
            if (
                correcting_meeting
                and "source_call_id" not in meeting
                and outcome
                not in {
                    "opt_out",
                    "wrong_party",
                    "invalid_endpoint",
                }
            ):
                fail(
                    409,
                    {
                        "code": "meeting_source_review_required",
                        "message": "Record the current meeting outcome and next outreach action first; this legacy meeting is not linked to a specific call.",
                    },
                )
            if outcome in {"wrong_party", "invalid_endpoint", "opt_out"}:
                # Journal the denial before any wrap-up mutation or receipt can
                # commit. A rollback retains the journal and recovery hold.
                self.suppress(
                    p, lead.data["phone"], outcome, event_id=f"disposition:{call_id}:{outcome}", session=s
                )
            if not identity[2] and not duplicate:
                if canonical["callback_note"] and canonical["callback_note"] != call.data.get(
                    "disposition_input", {}
                ).get("callback_note"):
                    note = self._by_key(s, p, "notes", call_id)
                    previous = note.data.get("human_text") if note else None
                    text = (previous + "\n\nWrap-up: " if previous else "") + canonical["callback_note"]
                    if len(text) > 20000:
                        fail(422, "The human note is full; review it before adding another wrap-up note")
                    values = {
                        "human_text": text,
                        "status": "human_confirmed",
                        "editor_id": p["user_id"],
                        "human_edit_version": (note.data.get("human_edit_version", 0) if note else 0) + 1,
                    }
                    if note:
                        update(note, **values)
                    else:
                        put(
                            s,
                            "notes",
                            p["tenant_id"],
                            p["seller_id"],
                            {
                                **values,
                                "call_id": call_id,
                                "privacy_contract": 1,
                                "notes": {
                                    "call_id": call_id,
                                    "transcript_version": call.data["transcript_version"],
                                    "outcome": outcome,
                                },
                                "derived_state_revision": 0,
                            },
                            key=call_id,
                            now=self.clock(),
                        )
                update(
                    call,
                    outcome=outcome,
                    state="FINALIZED",
                    callback_at=due,
                    disposition_input=canonical,
                    edit_version=edit_version + 1,
                    outcome_available_at=self.clock().isoformat(),
                    outcome_source=source,
                    contact_role=canonical["contact_role"],
                    outcome_history=[
                        *call.data.get("outcome_history", []),
                        *(
                            [
                                {
                                    "outcome": call.data["outcome"],
                                    "outcome_available_at": call.data.get("outcome_available_at"),
                                    "outcome_source": call.data.get("outcome_source", "legacy_unknown"),
                                    "contact_role": call.data.get("contact_role"),
                                }
                            ]
                            if call.data.get("outcome")
                            else []
                        ),
                    ],
                )
                self.record_attempt_work_in(s, p, call, closing=True)
                update(lead, last_outcome=outcome, last_outcome_at=self.clock().isoformat())
                if outcome in {"wrong_party", "invalid_endpoint"}:
                    update(lead, status="needs_correction")
                    endpoint = self.required(s, "phone_endpoints", lead.data["endpoint_id"], p)
                    update(
                        endpoint,
                        **(
                            {"ownership_status": "wrong_party"}
                            if outcome == "wrong_party"
                            else {"reachability_status": "invalid_endpoint"}
                        ),
                    )
                callback_key = "wrapup-callback:" + call_id
                # One scan of the lead's tasks serves the whole save.
                tasks = self.lead_records(s, p, "followup_tasks", lead.id)
                if outcome == "callback_requested":
                    task, callback_review = self._callback_task(
                        s,
                        p,
                        lead,
                        key=callback_key,
                        call_id=call_id,
                        owner_id=call.data["rep_id"],
                        due_at=due,
                        timezone=lead.data.get("timezone"),
                        note=canonical["callback_note"],
                        tasks=tasks,
                    )
                elif source == "rep_reported":
                    # The rep's outcome says no callback: close this call's untouched callbacks,
                    # the wrap-up one and model-inferred ones alike. Human edits are kept.
                    wrapup = self._callback_by_key(s, p, lead, callback_key, tasks)
                    closed = [
                        t
                        for t in tasks
                        if (
                            t is wrapup
                            or t.data.get("call_id") == call.id
                            and t.data.get("source") == "inferred"
                        )
                        and t.data.get("kind") == "callback"
                        and t.data["status"] in ACTIVE_TASKS
                        and not t.data.get("human_edited")
                    ]
                    for old_task in closed:
                        update(old_task, status="cancelled", reason="corrected_disposition")
                        if old_task.data.get("commitment_id"):
                            update(
                                self.required(s, "commitments", old_task.data["commitment_id"], p),
                                status="cancelled",
                                task_status="cancelled",
                            )
                        emit(s, p["tenant_id"], "task.updated", old_task, self.clock())
                    if closed:
                        self._sync_callback(s, p, lead, tasks)
                independent_meeting = (
                    correcting_confirmed
                    and "source_call_id" in meeting
                    and meeting["source_call_id"] != call.id
                )
                if outcome in {"meeting_booked", "meeting_held"} and not independent_meeting:
                    self._meeting_in(
                        s,
                        p,
                        lead,
                        "booked" if outcome == "meeting_booked" else "held",
                        source_call_id=call.id,
                    )
                elif correcting_meeting:
                    event = self._by_key(s, p, "conversion_events", "rep-meeting:" + lead.id)
                    if event and event.data.get("call_id") == call.id and not event.data.get("retracted"):
                        update(
                            event,
                            retracted=True,
                            retracted_at=self.clock().isoformat(),
                            retraction_reason="corrected_disposition",
                        )
                    if meeting.get("source_call_id") == call.id:
                        update(
                            lead,
                            meeting={
                                **meeting,
                                "status": "needs_review",
                                "held": False,
                                "correction_reason": "Call outcome corrected. Confirm the meeting status and next outreach action.",
                                "corrected_at": self.clock().isoformat(),
                            },
                            outreach_hold="meeting_next_action",
                        )
                record = self._by_key(s, p, "outcomes", "disposition:" + call_id)
                values = {
                    "call_id": call_id,
                    "lead_id": lead.id,
                    "outcome": outcome,
                    "at": self.clock().isoformat(),
                    "window_status": "pending",
                    "source": source,
                    "contact_role": canonical["contact_role"],
                }
                if record:
                    audit(
                        s,
                        p["tenant_id"],
                        p["seller_id"],
                        p["user_id"],
                        "outcome.corrected",
                        call_id,
                        self.clock(),
                        {"previous": record.data["outcome"], "new": outcome},
                    )
                    update(record, **values)
                else:
                    put(
                        s,
                        "outcomes",
                        p["tenant_id"],
                        p["seller_id"],
                        values,
                        key="disposition:" + call_id,
                        now=self.clock(),
                    )
                if {outcome, previous_outcome} & GOAL_OUTCOMES or lead.data.get("status") == "converted":
                    # Only an outcome that can change goal completion is reconciled in the
                    # save; the packet refresh job reconciles every other change.
                    self.reconcile_goal_status(s, p, lead)
                from app.optimization.conversations import invalidate_report

                invalidate_report(self, s, p, lead.data["campaign_id"])
                self.refresh_packet_in(s, p, lead)
                self.enqueue_finalize(s, p, call, "wrapup")
                emit(s, p["tenant_id"], "call.updated", call, self.clock())
            receipt = identity[2] or self.local_edit_receipt(s, p, identity, call)
            if task is None and callback_review is None:
                task = next(
                    (
                        t
                        for t in (tasks or self.lead_records(s, p, "followup_tasks", lead.id))
                        if t.data.get("call_id") == call.id and t.data.get("kind") == "callback"
                    ),
                    None,
                )
            memory = self._by_key(s, p, "conversation_states", call.id)
            from app.postcall.analysis import suggested_outcome

            result = {
                **doc(call),
                "edit_receipt": receipt,
                "canonical_task_id": task.id if task else None,
                # An existing callback a person edited, closed or duplicated is not
                # overwritten; the save succeeds and names what needs review.
                "callback_review": callback_review,
                "rundown_status": (
                    memory.data.get("finalization", {}).get("status", "pending") if memory else "pending"
                ),
                "suggested_outcome": suggested_outcome(memory.data if memory else {}),
            }
        return result
