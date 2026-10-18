"""Local commitments and rep-reported outcomes. No external scheduling effects."""

from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.db import MODELS, audit, doc, emit, put, update
from app.optimization.goals import review_token
from app.service import dt, fail
from app.telephony.reducer import TERMINAL

ACTIVE_TASKS = {"open", "snoozed", "needs_confirmation"}


def zoned_time(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Include a UTC offset; an unzoned time is ambiguous")
    return parsed.astimezone(UTC).isoformat()


class DatedEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
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
    status: Literal["open", "snoozed", "needs_confirmation", "completed", "cancelled"] | None = None
    owner_id: str | None = None
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("status")
    @classmethod
    def status_cannot_be_null(cls, value):
        if value is None:
            raise ValueError("Task status cannot be null")
        return value


class MeetingEdit(DatedEdit):
    expected_review_token: str | None = Field(default=None, min_length=64, max_length=64)
    status: Literal["booked", "held", "no_show", "cancelled", "rescheduled"]
    next_action: Literal["keep_hold", "resume", "callback"] = "keep_hold"
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
            self.db.authorize_stream(s, p)
            lead = self.required(s, "leads", lead_id, p)
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
            return doc(lead)

    def prospect_context_in(self, s, p, lead, calls, commitments):
        from app.optimization.learning import freeze_features, funnel_predictions

        campaign = self.required(s, "campaigns", lead.data["campaign_id"], p)
        config = self.optimization_config(s, p, campaign.id)
        features = freeze_features(doc(lead), health={"score": None, "samples": 0}, now=self.clock())
        profile = campaign.data.get("selection_profile", {})
        if profile.get("goal") != config["conversion_goal"]:
            profile = {}
        prediction = (
            profile.get("predictions", {}).get(f"{features['segment']}:{features['local_hour']}")
            or profile.get("pooled")
            or funnel_predictions([], features)
        )
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
        current = next((n.data["notes"] for n in notes if not n.data.get("stale")), {})
        tasks = [doc(t) for t in self.lead_records(s, p, "followup_tasks", lead.id)]
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
                "commitments": commitments,
                "human_notes": [
                    {"call_id": n.data["call_id"], "text": n.data["human_text"]}
                    for n in notes
                    if n.data.get("human_text")
                ],
                "last_outcome": lead.data.get("last_outcome"),
                "meeting": lead.data.get("meeting"),
                "next_question": "Confirm the agreed next step."
                if any(t["status"] in ACTIVE_TASKS for t in tasks)
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
                "uncertainty": "Sparse evidence; pooled starting estimates, not individual guarantees."
                if samples < 30
                else "Pooled estimates; conditional outcomes have separate sample support.",
                "model_version": prediction["model_version"],
                "as_of": profile.get("as_of"),
                "why_now": why,
            },
        }

    def lead_records(self, s, p, kind, lead_id):
        model = MODELS[kind]
        return list(
            s.scalars(
                select(model)
                .where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.op_lead_id == lead_id,
                )
                .order_by(model.op_at, model.id)
            )
        )

    def _by_key(self, s, p, kind, key):
        model = MODELS[kind]
        return s.scalar(
            select(model).where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.external_key == key,
            )
        )

    def refresh_packet_in(self, s, p, lead):
        update(lead, preparation_revision=lead.data.get("preparation_revision", 0) + 1)
        self.jobs.enqueue_in(
            s,
            "research",
            {
                "lead_ids": [lead.id],
                "seller_id": p["seller_id"],
                "user_id": p["user_id"],
            },
            f"continuity:{lead.id}:{lead.version}",
            tenant_id=p["tenant_id"],
            priority=5,
        )

    def _sync_callback(self, s, p, lead):
        active = [
            t
            for t in self.lead_records(s, p, "followup_tasks", lead.id)
            if t.data.get("kind") == "callback" and t.data["status"] in ACTIVE_TASKS
        ]
        dated = [t for t in active if t.data.get("due_at") and t.data["status"] != "needs_confirmation"]
        next_task = min(dated, key=lambda t: dt(t.data["due_at"]), default=None)
        hold = lead.data.get("outreach_hold")
        if hold in {None, "callback_time_confirmation"}:
            hold = "callback_time_confirmation" if active and not next_task else None
        previous = lead.data.get("callback_due_at")
        due = next_task.data["due_at"] if next_task else None
        next_at = lead.data.get("next_attempt_at")
        if due:
            next_at = due
        elif previous and next_at == previous:
            next_at = self.clock().isoformat()
        update(
            lead,
            callback_due_at=due,
            callback_owner_id=next_task.data.get("owner_id") if next_task else None,
            next_attempt_at=next_at,
            outreach_hold=hold,
        )

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
    ):
        task = self._by_key(s, p, "followup_tasks", key)
        if not task and call_id and key == "wrapup-callback:" + call_id:
            inferred = [
                t
                for t in self.lead_records(s, p, "followup_tasks", lead.id)
                if t.data.get("call_id") == call_id
                and t.data.get("kind") == "callback"
                and t.data.get("source") == "inferred"
            ]
            if len(inferred) == 1:
                task = inferred[0]
                task.external_key = key
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
            "source": source,
        }
        if task:
            # Repeated machine extraction or wrap-up cannot overwrite an independently edited task.
            if explicit_replacement or (
                not task.data.get("human_edited") and task.data.get("status") in ACTIVE_TASKS
            ):
                update(task, **values)
        else:
            task = put(s, "followup_tasks", p["tenant_id"], p["seller_id"], values, key=key, now=self.clock())
        self._sync_callback(s, p, lead)
        emit(s, p["tenant_id"], "task.updated", task, self.clock())
        return task

    def edit_task(self, p, task_id, data):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            task = self.required(s, "followup_tasks", task_id, p)
            if p["role"] == "rep" and task.data.get("owner_id") != p["user_id"]:
                fail(403, "Task belongs to another rep")
            if task.version != data["expected_version"]:
                fail(409, "Task changed; refresh before editing")
            edits = {k: v for k, v in data.items() if k != "expected_version"}
            if "owner_id" in edits:
                owner = self.required(s, "users", edits["owner_id"] or "", p)
                if owner.data.get("disabled"):
                    fail(422, "Task owner is disabled")
            candidate = {**task.data, **edits}
            if candidate["status"] in {"open", "snoozed"} and not candidate.get("due_at"):
                fail(422, "Confirm a dated time before opening or snoozing this task")
            if edits.get("due_at") and task.data["status"] == "needs_confirmation" and "status" not in edits:
                edits["status"] = "open"
            update(
                task,
                **edits,
                human_edited=True,
                review_required=False,
                reviewed_source_version=task.data.get("source_version"),
                edited_at=self.clock().isoformat(),
                edited_by=p["user_id"],
            )
            if task.data.get("commitment_id"):
                commitment = self.required(s, "commitments", task.data["commitment_id"], p)
                update(commitment, task_status=task.data["status"])
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
            return doc(task)

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
    ):
        if next_action == "callback" and not callback_at:
            fail(422, "A confirmed callback time is required")
        previous = lead.data.get("meeting", {})
        meeting = {
            "status": status,
            "source": "rep_reported",
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
                if task.data.get("kind") == "callback" and task.data.get("status") in ACTIVE_TASKS:
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
                "call_id": None,
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
        emit(s, p["tenant_id"], "lead.meeting_updated", lead, self.clock())

    def report_meeting(self, p, lead_id, data):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            lead = self.required(s, "leads", lead_id, p)
            self.check_prospect_review(s, p, lead, data)
            self._meeting_in(
                s,
                p,
                lead,
                data["status"],
                **{
                    k: data[k]
                    for k in ("due_at", "timezone", "reference", "next_action", "callback_at")
                    if k in data
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
            return doc(lead)

    def save_disposition(self, p, call_id, data):
        outcome = data["outcome"]
        due = zoned_time(data.get("callback_at"))
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            call = self.required(s, "call_attempts", call_id, p)
            if p["role"] == "rep" and call.data["rep_id"] != p["user_id"]:
                fail(403, "Call belongs to another rep")
            if call.data["state"] not in TERMINAL:
                fail(409, "End the call before wrap-up")
            lead = self.required(s, "leads", call.data["lead_id"], p)
            canonical = {
                "outcome": outcome,
                "callback_at": due,
                "callback_note": data.get("callback_note", ""),
            }
            duplicate = call.data.get("disposition_input") == canonical
            if not duplicate:
                if data.get("expected_version") is not None and call.version != data["expected_version"]:
                    fail(409, "Call outcome changed; refresh before editing")
                update(
                    call,
                    outcome=outcome,
                    state="FINALIZED",
                    callback_at=due,
                    disposition_input=canonical,
                    outcome_available_at=self.clock().isoformat(),
                    outcome_source="rep_reported",
                    outcome_history=[
                        *call.data.get("outcome_history", []),
                        *(
                            [
                                {
                                    "outcome": call.data["outcome"],
                                    "outcome_available_at": call.data.get("outcome_available_at"),
                                    "outcome_source": call.data.get("outcome_source", "legacy_unknown"),
                                }
                            ]
                            if call.data.get("outcome")
                            else []
                        ),
                    ],
                )
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
                if outcome == "callback_requested":
                    self._callback_task(
                        s,
                        p,
                        lead,
                        key=callback_key,
                        call_id=call_id,
                        owner_id=call.data["rep_id"],
                        due_at=due,
                        timezone=lead.data.get("timezone"),
                        note=canonical["callback_note"],
                    )
                else:
                    old_task = self._by_key(s, p, "followup_tasks", callback_key)
                    if (
                        old_task
                        and old_task.data["status"] in ACTIVE_TASKS
                        and not old_task.data.get("human_edited")
                    ):
                        update(old_task, status="cancelled", reason="corrected_disposition")
                        self._sync_callback(s, p, lead)
                        emit(s, p["tenant_id"], "task.updated", old_task, self.clock())
                if outcome in {"meeting_booked", "meeting_held"}:
                    self._meeting_in(s, p, lead, "booked" if outcome == "meeting_booked" else "held")
                record = self._by_key(s, p, "outcomes", "disposition:" + call_id)
                values = {
                    "call_id": call_id,
                    "lead_id": lead.id,
                    "outcome": outcome,
                    "at": self.clock().isoformat(),
                    "window_status": "pending",
                    "source": "rep_reported",
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
                self.reconcile_goal_status(s, p, lead)
                self.refresh_packet_in(s, p, lead)
                self.jobs.enqueue_in(
                    s,
                    "postcall",
                    {"call_id": call_id, "seller_id": p["seller_id"], "user_id": p["user_id"]},
                    f"wrapup:{call_id}:{call.version}",
                    tenant_id=p["tenant_id"],
                )
                emit(s, p["tenant_id"], "call.updated", call, self.clock())
            result, phone = doc(call), lead.data["phone"]
        if outcome in {"wrong_party", "invalid_endpoint", "opt_out"}:
            self.suppress(p, phone, outcome, event_id=f"disposition:{call_id}:{outcome}")
        return result
