"""Local commitments and rep-reported outcomes. No external scheduling effects."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from app import ops
from app.aftercall.tasks import ACTIVE as ACTIVE_TASKS
from app.calling.state import TERMINAL, moved
from app.optimization.goals import review_token
from app.platform.config import TEST_LINES
from app.platform.db import MODELS, audit, doc, emit, get, put, uid, update
from app.service import dt, fail

# An AI-suggested callback time defers automatic outreach this long at most; only a
# rep-stated callback holds outreach until the rep confirms a time.
SUGGESTION_HOLD = timedelta(days=7)
GOAL_OUTCOMES = {"qualified_conversation", "meeting_booked", "meeting_held", "sale_won"}


def zoned_time(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Include a UTC offset; an unzoned time is ambiguous")
    return parsed.astimezone(UTC).isoformat()


MEETINGS = {"meeting_booked", "meeting_held"}
# Outcomes that stop dialing this number.
NUMBER_STOPS = {"opt_out", "wrong_party", "invalid_endpoint"}


def disposition_input(call, data, outcome, due):
    """The saved form of a wrap-up: optional flags not resent keep their previous values."""
    canonical = {
        "outcome": outcome,
        "callback_at": due,
        "callback_note": data.get("callback_note", ""),
        "contact_role": data.get("contact_role", "unknown"),
    }
    previous = call.get("disposition_input") or {}
    for key in ("audio_result", "screened", "delivery_test"):
        if data.get(key) is not None:
            canonical[key] = data[key]
        elif key in previous:
            canonical[key] = previous[key]
    return canonical


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


def local_edit_in(service, s, p, kind, requested_id, row, data, owner_field):
    """Authorize before replay; the receipt is metadata, never a historical object view."""
    service.db.authorize_stream(s, p)
    s.refresh(row, with_for_update=True)
    ops.required(service, s, "users", p["user_id"], p)
    # R3.6: one writer, so the last save is the record; the request ID makes a retried save land once.
    if data.get("request_id") is None:
        fail(
            409,
            {"code": "client_upgrade_required", "message": "Update the client to save identified edits"},
        )
    try:
        request_id = str(UUID(str(data["request_id"])))
    except (ValueError, TypeError, AttributeError):
        fail(422, "request_id must be a UUID")
    key = f"edit:{kind}:{request_id}"
    digest = hashlib.sha256(
        json.dumps(
            [requested_id, p["user_id"], data], sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()
    receipt = ops._by_key(service, s, p, "outcomes", key)
    if receipt:
        if receipt.data["digest"] != digest or receipt.data["object_id"] != row.id:
            fail(409, "Request identity was already used for a different edit")
        return key, digest, receipt.data["receipt"]
    return key, digest, None


def local_edit_receipt(service, s, p, identity, row, *, object_version=None):
    key, digest, _ = identity
    receipt = {
        "request_id": key.rsplit(":", 1)[1],
        "object_id": row.id,
        "version": row.version if object_version is None else object_version,
        "at": service.clock().isoformat(),
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
        now=service.clock(),
    )
    return receipt


def resolve_goal_review(service, p, lead_id, data):
    with service.db.transaction() as s:
        lead = ops.required(service, s, "leads", lead_id, p)
        identity = ops.local_edit_in(service, s, p, "outreach", lead_id, lead, data, "rep_id")
        if identity[2]:
            return {**doc(lead), "edit_receipt": identity[2]}
        if lead.data.get("outreach_hold") != "goal_changed":
            fail(409, "Refresh the prospect; only a goal-change review can be resolved here")
        if ops.suppressed(service, s, p, lead.data["phone"]):
            fail(403, "An opt-out cannot be cleared by a next-action review")
        if data["next_action"] == "callback":
            if not data.get("due_at") or not data.get("timezone"):
                fail(422, "Confirm a callback time and timezone")
            ops._callback_task(
                service,
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
        ops.refresh_packet_in(service, s, p, lead)
        audit(
            s,
            p["tenant_id"],
            p["seller_id"],
            p["user_id"],
            "lead.goal_reviewed",
            lead.id,
            service.clock(),
            {"next_action": data["next_action"], "reason": data["reason"]},
        )
        emit(s, p["tenant_id"], "lead.goal_updated", lead, service.clock())
        return {**doc(lead), "edit_receipt": ops.local_edit_receipt(service, s, p, identity, lead)}


def prospect_context_in(service, s, p, lead, calls, tasks, *, decision=None):
    campaign = ops.required(service, s, "campaigns", lead.data["campaign_id"], p)
    recent = sorted(calls, key=lambda c: c.get("started_at", ""))[-10:]
    # Earlier calls as the rep and the next script see them: the after-call summary and the rep's own note.
    earlier = []
    for call in reversed(recent):
        summary = ops.call_summary(service, s, p, call["id"]) or {}
        note = ops._by_key(service, s, p, "notes", call["id"])
        earlier.append(
            {
                "call_id": call["id"],
                "at": call.get("started_at"),
                "outcome": call.get("outcome"),
                "summary": summary.get("summary"),
                "next_step": summary.get("next_step") or None,
                "note": note.data.get("human_text") if note else None,
            }
        )
    last = next((c for c in earlier if c["summary"]), {})
    preparation = ops.preparation_state(service, s, p, lead)
    if lead.data.get("outreach_hold"):
        why = "Outreach paused: " + lead.data["outreach_hold"].replace("_", " ")
    elif lead.data.get("callback_due_at"):
        why = "A requested callback takes priority at the confirmed time."
    elif lead.data.get("requested_contact"):
        why = "This prospect requested contact."
    elif dt(lead.data.get("coverage_due_at")) and dt(lead.data["coverage_due_at"]) <= service.clock():
        why = "This lead is due for another try."
    elif not lead.data.get("attempt_count"):
        why = "Never called; new leads go first."
    else:
        why = "Waited longest since the last try."
    return {
        "review_token": review_token(lead, campaign),
        "tasks": tasks,
        "prospect_brief": {
            "version": lead.version,
            "evidence_status": "sparse" if not recent else "observed",
            "summary": last.get("summary") or "No earlier call summary yet.",
            "next_step": last.get("next_step"),
            "earlier_calls": earlier[:5],
            "freshness": {
                "state": preparation["status"],
                "preparation_revision": lead.data.get("preparation_revision", 0),
            },
            "human_notes": [{"call_id": c["call_id"], "text": c["note"]} for c in earlier if c["note"]],
            "last_outcome": lead.data.get("last_outcome"),
            "last_outcome_at": lead.data.get("last_outcome_at"),
            "meeting": lead.data.get("meeting"),
            "why_queued": why,
            "next_question": "Confirm the agreed next step."
            if any(t.get("status") in ACTIVE_TASKS for t in tasks)
            else last.get("next_step") or "What would make this conversation useful for you?",
        },
    }


def lead_records(service, s, p, kind, lead_id, *conditions, limit=None, newest=False):
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
            .order_by(model.op_at.desc() if newest else model.op_at, model.id.desc() if newest else model.id)
            .limit(limit)
        )
    )


def refresh_packet_in(service, s, p, lead):
    """Mark the brief dirty and queue its refresh; features and goal state are
    recomputed by that background job (refresh_continuity), not in the caller's save."""
    update(lead, preparation_revision=lead.data.get("preparation_revision", 0) + 1)
    ops.queue_packet_refresh_in(service, s, p, lead)
    emit(s, p["tenant_id"], "packet.stale", lead, service.clock())


def queue_packet_refresh_in(service, s, p, lead):
    service.jobs.coalesce_live_in(
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
        available_at=service.clock(),
    )


def refresh_continuity(service, p, lead_ids):
    """Background half of a packet refresh: lead features and goal state.

    A rep's save only marks the brief dirty; this recomputation over the lead's
    history runs in the packet_refresh job, one short write per lead.
    """
    for lead_id in dict.fromkeys(lead_ids):
        with service.db.transaction() as s:
            lead = ops.required(service, s, "leads", lead_id, p)
            s.refresh(lead, with_for_update=True)
            ops.reconcile_goal_status(service, s, p, lead)


def _sync_callback(service, s, p, lead, tasks=None):
    tasks = ops.lead_records(service, s, p, "followup_tasks", lead.id) if tasks is None else tasks
    active = [t for t in tasks if t.data.get("kind") == "callback" and t.data["status"] in ACTIVE_TASKS]
    dated = [t for t in active if t.data.get("due_at") and t.data["status"] != "needs_confirmation"]
    next_task = min(dated, key=lambda t: dt(t.data["due_at"]), default=None)
    undated = [t for t in active if t not in dated]
    stated = [t for t in undated if t.data.get("source") != "inferred" or t.data.get("human_edited")]
    suggested = [
        dt(t.data.get("suggested_at") or t.created_at.replace(tzinfo=t.created_at.tzinfo or UTC).isoformat())
        + SUGGESTION_HOLD
        for t in undated
        if t not in stated
    ]
    until = max([u for u in suggested if u > service.clock()], default=None)
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
        next_at = max(dt(next_at) or service.clock(), until).isoformat()
    elif next_at in previous:
        next_at = service.clock().isoformat()
    update(
        lead,
        callback_due_at=due,
        callback_owner_id=next_task.data.get("owner_id") if next_task else None,
        callback_suggestion_until=deferral,
        next_attempt_at=next_at,
        outreach_hold=hold,
    )


def _callback_by_key(service, s, p, lead, key, tasks=None):
    task = ops._by_key(service, s, p, "followup_tasks", key)
    if task:
        return (
            ops.required(service, s, "followup_tasks", task.data["alias_of"], p)
            if task.data.get("alias_of")
            else task
        )
    tasks = ops.lead_records(service, s, p, "followup_tasks", lead.id) if tasks is None else tasks
    return next((t for t in tasks if t.data.get("wrapup_key") == key), None)


def _callback_task(
    service,
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
    tasks = ops.lead_records(service, s, p, "followup_tasks", lead.id) if tasks is None else tasks
    task = ops._callback_by_key(service, s, p, lead, key, tasks)
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
                    emit(s, p["tenant_id"], "task.updated", other, service.clock())
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
        task = put(s, "followup_tasks", p["tenant_id"], p["seller_id"], values, key=key, now=service.clock())
        tasks.append(task)
    ops._sync_callback(service, s, p, lead, tasks)
    emit(s, p["tenant_id"], "task.updated", task, service.clock())
    return task, review


def edit_task(service, p, task_id, data):
    with service.db.transaction() as s:
        # The seller's authorization stream comes before any row lock, as in every writer;
        # taking it after the rows deadlocked against a stream holder waiting for the call.
        service.db.authorize_stream(s, p)
        task = ops.required(service, s, "followup_tasks", task_id, p)
        if task.data.get("alias_of"):
            task = ops.required(service, s, "followup_tasks", task.data["alias_of"], p)
        # Lock in the call writers' order (source call, lead, then the task and its
        # promise) so post-call work and packet refresh wait instead of going stale.
        if task.data.get("call_id"):
            get(s, "call_attempts", task.data["call_id"], p["tenant_id"], lock=True)
        if task.data.get("lead_id"):
            get(s, "leads", task.data["lead_id"], p["tenant_id"], lock=True)
        s.refresh(task, with_for_update=True)
        identity = ops.local_edit_in(service, s, p, "task", task_id, task, data, "owner_id")
        if identity[2]:
            return {**doc(task), "edit_receipt": identity[2]}
        if task.data.get("inbound") and task.data.get("inbound_state") not in {None, "ended"}:
            fail(409, "This return call is still active; finish call routing before editing the task")
        edits = {
            k: v for k, v in data.items() if k not in {"expected_version", "request_id", "due_offset_minutes"}
        }
        zone = edits.get("timezone") or task.data.get("timezone")
        if edits.get("due_at") and data.get("due_offset_minutes") and zone:
            # A non-UTC offset is a local reading; it must be the task zone's offset then.
            local = dt(edits["due_at"]).astimezone(ZoneInfo(zone)).utcoffset()
            if data["due_offset_minutes"] != int(local.total_seconds() // 60):
                fail(422, "The time's UTC offset does not match the task timezone; choose the time again")
        if "owner_id" in edits:
            ops.required(service, s, "users", edits["owner_id"] or "", p)
        candidate = {**task.data, **edits}
        if candidate["status"] in {"open", "snoozed"} and not candidate.get("due_at"):
            fail(422, "Confirm a dated time before opening or snoozing this task")
        if candidate.get("window_end") and (
            not candidate.get("due_at") or dt(candidate["window_end"]) <= dt(candidate["due_at"])
        ):
            fail(422, "Callback window must end after its start")
        if candidate["status"] == "snoozed" and (
            not candidate.get("snoozed_until") or dt(candidate["snoozed_until"]) <= service.clock()
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
            edited_at=service.clock().isoformat(),
            edited_by=p["user_id"],
        )
        if task.data.get("lead_id"):
            lead = ops.required(service, s, "leads", task.data["lead_id"], p)
            ops._sync_callback(service, s, p, lead)
            ops.refresh_packet_in(service, s, p, lead)
        audit(
            s,
            p["tenant_id"],
            p["seller_id"],
            p["user_id"],
            "task.updated",
            task.id,
            service.clock(),
            {"fields": sorted(edits)},
        )
        emit(s, p["tenant_id"], "task.updated", task, service.clock())
        return {**doc(task), "edit_receipt": ops.local_edit_receipt(service, s, p, identity, task)}


def company_hold_in(service, s, p, lead, *, booked):
    """A booking at one contact pauses calls to the others at that company; undoing it releases them."""
    key = lead.data.get("company_key")
    if not key:
        return
    model = MODELS["leads"]
    for other in s.scalars(
        select(model).where(
            model.tenant_id == p["tenant_id"],
            model.seller_id == p["seller_id"],
            model.op_lead_id == key,
            model.id != lead.id,
        )
    ):
        if booked and not other.data.get("outreach_hold"):
            update(other, outreach_hold="company_booked", company_booked_by=lead.id)
        elif not booked and other.data.get("company_booked_by") == lead.id:
            update(other, outreach_hold=None, company_booked_by=None)
        else:
            continue
        emit(s, p["tenant_id"], "lead.meeting_updated", other, service.clock())


def _booked_label(service, s, p, lead, *, booked, call_id):
    """R8.1: the meeting_booked label, known at wrap-up; a correction retracts it."""
    event = ops._by_key(service, s, p, "conversion_events", "rep-booked:" + lead.id)
    now = service.clock().isoformat()
    if booked and not (event and not event.data.get("retracted")):
        values = {
            "lead_id": lead.id,
            "kind": "meeting_booked",
            "occurred_at": now,
            "observed_at": now,
            "label_available_at": now,
            "source": "rep_reported",
            "retracted": False,
            "call_id": call_id,
        }
        if event:
            update(event, **values, retracted_at=None)
        else:
            put(
                s,
                "conversion_events",
                p["tenant_id"],
                p["seller_id"],
                values,
                key="rep-booked:" + lead.id,
                now=service.clock(),
            )
    elif not booked and event and not event.data.get("retracted"):
        update(event, retracted=True, retracted_at=now)


def _meeting_in(
    service,
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
        "reported_at": service.clock().isoformat(),
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
    ops.company_hold_in(service, s, p, lead, booked=status in {"booked", "rescheduled", "held"})
    if status in {"booked", "rescheduled", "held"}:
        for task in ops.lead_records(service, s, p, "followup_tasks", lead.id):
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
            emit(s, p["tenant_id"], "task.updated", task, service.clock())
    elif next_action == "callback":
        ops._callback_task(
            service,
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
    ops._sync_callback(service, s, p, lead)
    ops._booked_label(
        service, s, p, lead, booked=status in {"booked", "rescheduled", "held"}, call_id=source_call_id
    )
    # Explicit later meeting reports retain occurrence/observation provenance for learning.
    event = ops._by_key(service, s, p, "conversion_events", "rep-meeting:" + lead.id)
    if status == "held":
        values = {
            "lead_id": lead.id,
            "kind": "meeting_held",
            "occurred_at": service.clock().isoformat(),
            "observed_at": service.clock().isoformat(),
            "label_available_at": service.clock().isoformat(),
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
                now=service.clock(),
            )
    elif event and not event.data.get("retracted"):
        update(event, retracted=True, retracted_at=service.clock().isoformat())
    ops.reconcile_goal_status(service, s, p, lead)
    from app.optimization.conversations import invalidate_report

    invalidate_report(service, s, p, lead.data["campaign_id"])
    emit(s, p["tenant_id"], "lead.meeting_updated", lead, service.clock())


def report_meeting(service, p, lead_id, data):
    with service.db.transaction() as s:
        lead = ops.required(service, s, "leads", lead_id, p)
        identity = ops.local_edit_in(service, s, p, "meeting", lead_id, lead, data, "rep_id")
        if not identity[2]:
            if data["status"] in {"no_show", "cancelled"} and not data.get("next_action"):
                fail(422, "Choose next_action: keep_hold, callback or resume")
            ops._meeting_in(
                service,
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
            ops.refresh_packet_in(service, s, p, lead)
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "meeting.reported",
                lead.id,
                service.clock(),
                {"status": data["status"], "next_action": data.get("next_action", "keep_hold")},
            )
        event = ops._by_key(service, s, p, "conversion_events", "rep-meeting:" + lead.id)
        return {
            **doc(lead),
            "edit_receipt": identity[2] or ops.local_edit_receipt(service, s, p, identity, lead),
            "outcome_id": event.id if event else None,
            "packet_version": lead.data.get("preparation_revision", 0),
        }


def auto_disposition(service, p, call_id):
    """Save a carrier fact (no answer, busy, voicemail) as the outcome, never over a rep's own save."""
    from fastapi import HTTPException

    with service.db.transaction(False) as s:
        call = ops.required(service, s, "call_attempts", call_id, p)
        if call.data["state"] not in TERMINAL or call.data.get("edit_version", 0) or call.data.get("outcome"):
            return
        outcome = call.data.get("carrier_outcome")
        if call.data.get("machine_detected") and not call.data.get("human_answered_at"):
            outcome = "voicemail"
        if not outcome:
            return  # Only carrier facts save themselves; a person's answer waits for the rep (section 6).
        source, role = "carrier", "unknown"
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
        ops.save_disposition(service, p, call_id, body, source=source)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise


def save_disposition(service, p, call_id, data, *, source="rep_reported"):
    """Save the rep's (or a tool's) outcome for an ended call, with its effects, in one transaction."""
    outcome = data["outcome"]
    due = zoned_time(data.get("callback_at"))
    with service.db.transaction() as s:
        service.db.authorize_stream(s, p)  # Stream first, then call, then lead.
        call = ops.required(service, s, "call_attempts", call_id, p)
        # Serialize with telephony/maintenance writers so a concurrent bump cannot raise
        # StaleDataError mid-save; conflicts are decided by the rep edit counter below.
        s.refresh(call, with_for_update=True)
        identity = ops.local_edit_in(service, s, p, "disposition", call_id, call, data, "rep_id")
        if call.data["state"] not in TERMINAL:
            fail(409, "End the call before wrap-up")
        lead = ops.required(service, s, "leads", call.data["lead_id"], p, lock=True)  # Call, then lead.
        canonical = disposition_input(call.data, data, outcome, due)
        duplicate = (
            call.data.get("disposition_input") == canonical and call.data.get("outcome_source") == source
        )
        edit_version = call.data.get("edit_version", 0)
        replayed = bool(identity[2])
        if source != "rep_reported" and (edit_version or call.data.get("outcome")):
            fail(409, "A human or previous tool already saved this outcome")
        previous_outcome = call.data.get("outcome")
        correcting_confirmed = call.data["state"] == "FINALIZED"
        correcting_meeting = (
            not replayed and not duplicate and previous_outcome in MEETINGS and outcome not in MEETINGS
        )
        meeting = lead.data.get("meeting", {})
        if correcting_meeting and "source_call_id" not in meeting and outcome not in NUMBER_STOPS:
            fail(
                409,
                {
                    "code": "meeting_source_review_required",
                    "message": "Record the current meeting outcome and next outreach action first; this legacy meeting is not linked to a specific call.",
                },
            )
        endpoint = None
        test_run = bool(call.data.get("dial_scope", {}).get("test_run_id"))
        if outcome in NUMBER_STOPS and (not test_run or outcome == "opt_out"):
            # Journal the denial before any wrap-up mutation or receipt can
            # commit. A rollback retains the journal and recovery hold.
            endpoint_id = call.data.get("endpoint_id") or lead.data["endpoint_id"]
            endpoint = ops.required(service, s, "phone_endpoints", endpoint_id, p)
            ops.suppress(
                service,
                p,
                endpoint.data["phone"],
                outcome,
                event_id=f"disposition:{call_id}:{outcome}",
                session=s,
            )
            if outcome == "opt_out" and lead.data["phone"] and lead.data["endpoint_id"] != endpoint_id:
                ops.suppress(
                    service,
                    p,
                    lead.data["phone"],
                    outcome,
                    event_id=f"disposition:{call_id}:{outcome}:{lead.data['endpoint_id']}",
                    session=s,
                )
        task = callback_review = tasks = None
        if not replayed and not duplicate:
            ops._wrapup_note(service, s, p, call, canonical, outcome)
            ops._apply_outcome(service, s, p, call, lead, canonical, source, edit_version, endpoint=endpoint)
            tasks = ops.lead_records(service, s, p, "followup_tasks", lead.id)  # One scan serves the save.
            task, callback_review = ops._wrapup_callbacks(service, s, p, call, lead, canonical, source, tasks)
            if not test_run:
                ops._wrapup_meeting(
                    service, s, p, call, lead, outcome, meeting, correcting_confirmed, correcting_meeting
                )
            ops._outcome_record(service, s, p, call, lead, canonical, source)
            if {outcome, previous_outcome} & GOAL_OUTCOMES or lead.data.get("status") == "converted":
                # Only an outcome that can change goal completion is reconciled in the
                # save; the packet refresh job reconciles every other change.
                ops.reconcile_goal_status(service, s, p, lead)
            from app.optimization.conversations import invalidate_report

            invalidate_report(service, s, p, lead.data["campaign_id"])
            ops.refresh_packet_in(service, s, p, lead)
            ops.enqueue_aftercall(service, s, p, call)
            from app import crm

            crm.on_outcome(service, s, p, call, lead, outcome)
            if test_run:
                from app import test_list

                test_list.complete_call(service, s, p, call)
            emit(s, p["tenant_id"], "call.updated", call, service.clock())
        receipt = identity[2] or ops.local_edit_receipt(service, s, p, identity, call)
        result = ops._disposition_result(service, s, p, call, lead, receipt, task, callback_review, tasks)
    if (
        not correcting_confirmed
        and call.data.get("dial_scope", {}).get("test_only")
        and not test_run
        and ops.in_test_session(service, p)
    ):
        # Carrier outcomes and rep outcomes both end the one-call session.
        ops.presence(service, p, {"status": "paused", "telephony_ready": False})
        ops.end_test_session(service, p)
    return result


def _wrapup_note(service, s, p, call, canonical, outcome):
    """A wrap-up callback note is appended to the call's human note."""
    if not canonical["callback_note"] or canonical["callback_note"] == call.data.get(
        "disposition_input", {}
    ).get("callback_note"):
        return
    note = ops._by_key(service, s, p, "notes", call.id)
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
        return
    put(
        s,
        "notes",
        p["tenant_id"],
        p["seller_id"],
        {
            **values,
            "call_id": call.id,
            "privacy_contract": 1,
            "notes": {
                "call_id": call.id,
                "transcript_version": call.data["transcript_version"],
                "outcome": outcome,
            },
            "derived_state_revision": 0,
        },
        key=call.id,
        now=service.clock(),
    )


def _apply_outcome(service, s, p, call, lead, canonical, source, edit_version, *, endpoint):
    """The outcome on the call, the lead and, for a bad number, its endpoint."""
    outcome = canonical["outcome"]
    prior = (
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
    )
    update(
        call,
        outcome=outcome,
        **moved(call.data, "outcome.saved", service.clock()),
        callback_at=canonical["callback_at"],
        disposition_input=canonical,
        # A call placed as a test (the owner's line, or the rep's own test phone in a test session) stays one.
        is_test=bool(call.data.get("is_test"))
        or lead.data["phone"] in TEST_LINES
        or bool(canonical.get("delivery_test")),
        edit_version=edit_version + 1,
        outcome_available_at=service.clock().isoformat(),
        outcome_source=source,
        contact_role=canonical["contact_role"],
        outcome_history=[*call.data.get("outcome_history", []), *prior],
    )
    update(lead, last_outcome=outcome, last_outcome_at=service.clock().isoformat())
    if endpoint and outcome in {"wrong_party", "invalid_endpoint"}:
        if endpoint.id == lead.data["endpoint_id"]:
            update(lead, status="needs_correction")
        update(
            endpoint,
            **(
                {"ownership_status": "wrong_party"}
                if outcome == "wrong_party"
                else {"reachability_status": "invalid_endpoint"}
            ),
        )


def _wrapup_callbacks(service, s, p, call, lead, canonical, source, tasks):
    """(task, review): a requested callback is scheduled; any other rep outcome closes this call's open ones."""
    callback_key = "wrapup-callback:" + call.id
    if canonical["outcome"] == "callback_requested":
        return ops._callback_task(
            service,
            s,
            p,
            lead,
            key=callback_key,
            call_id=call.id,
            owner_id=call.data["rep_id"],
            due_at=canonical["callback_at"],
            timezone=lead.data.get("timezone"),
            note=canonical["callback_note"],
            tasks=tasks,
        )
    if source != "rep_reported":
        return None, None
    # The rep's outcome says no callback: close this call's untouched callbacks,
    # the wrap-up one and model-inferred ones alike. Human edits are kept.
    wrapup = ops._callback_by_key(service, s, p, lead, callback_key, tasks)
    closed = [
        t
        for t in tasks
        if (t is wrapup or t.data.get("call_id") == call.id and t.data.get("source") == "inferred")
        and t.data.get("kind") == "callback"
        and t.data["status"] in ACTIVE_TASKS
        and not t.data.get("human_edited")
    ]
    for old_task in closed:
        update(old_task, status="cancelled", reason="corrected_disposition")
        emit(s, p["tenant_id"], "task.updated", old_task, service.clock())
    if closed:
        ops._sync_callback(service, s, p, lead, tasks)
    return None, None


def _wrapup_meeting(service, s, p, call, lead, outcome, meeting, correcting_confirmed, correcting_meeting):
    """A booked or held meeting is recorded; a corrected one is retracted and put up for review."""
    independent = (
        correcting_confirmed and "source_call_id" in meeting and meeting["source_call_id"] != call.id
    )
    if outcome in MEETINGS and not independent:
        status = "booked" if outcome == "meeting_booked" else "held"
        ops._meeting_in(service, s, p, lead, status, source_call_id=call.id)
        return
    if not correcting_meeting:
        return
    event = ops._by_key(service, s, p, "conversion_events", "rep-meeting:" + lead.id)
    if event and event.data.get("call_id") == call.id and not event.data.get("retracted"):
        update(
            event,
            retracted=True,
            retracted_at=service.clock().isoformat(),
            retraction_reason="corrected_disposition",
        )
    if meeting.get("source_call_id") == call.id:
        ops._booked_label(service, s, p, lead, booked=False, call_id=call.id)
        update(
            lead,
            meeting={
                **meeting,
                "status": "needs_review",
                "held": False,
                "correction_reason": "Call outcome corrected. Confirm the meeting status and next outreach action.",
                "corrected_at": service.clock().isoformat(),
            },
            outreach_hold="meeting_next_action",
        )
        ops.company_hold_in(service, s, p, lead, booked=False)


def _outcome_record(service, s, p, call, lead, canonical, source):
    """One outcome record per call; a correction is audited."""
    record = ops._by_key(service, s, p, "outcomes", "disposition:" + call.id)
    values = {
        "call_id": call.id,
        "lead_id": lead.id,
        "outcome": canonical["outcome"],
        "at": service.clock().isoformat(),
        "window_status": "pending",
        "source": source,
        "contact_role": canonical["contact_role"],
    }
    if not record:
        put(
            s,
            "outcomes",
            p["tenant_id"],
            p["seller_id"],
            values,
            key="disposition:" + call.id,
            now=service.clock(),
        )
        return
    audit(
        s,
        p["tenant_id"],
        p["seller_id"],
        p["user_id"],
        "outcome.corrected",
        call.id,
        service.clock(),
        {"previous": record.data["outcome"], "new": canonical["outcome"]},
    )
    update(record, **values)


def _disposition_result(service, s, p, call, lead, receipt, task, callback_review, tasks):
    from app.aftercall.summary import suggestion

    if task is None and callback_review is None:
        task = next(
            (
                t
                for t in (tasks or ops.lead_records(service, s, p, "followup_tasks", lead.id))
                if t.data.get("call_id") == call.id and t.data.get("kind") == "callback"
            ),
            None,
        )
    return {
        **doc(call),
        "edit_receipt": receipt,
        "canonical_task_id": task.id if task else None,
        # An existing callback a person edited, closed or duplicated is not
        # overwritten; the save succeeds and names what needs review.
        "callback_review": callback_review,
        "suggested_outcome": suggestion(ops.call_summary(service, s, p, call.id)),
    }
