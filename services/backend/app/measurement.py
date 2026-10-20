"""Shared outcome definitions and transparent, heartbeat-supported working intervals."""

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.db import MODELS, audit, doc, put, update
from app.operational_schema import call_work_bounds
from app.optimization.numbering import timestamp, wilson
from app.service import fail
from app.telephony.reducer import TERMINAL

WORK_LEASE_SECONDS = 90
WORK_NOTE = (
    "Working time is declared availability supported by heartbeats plus verified call occupancy; "
    "breaks and disconnected gaps are excluded. Correct your intervals when needed."
)


def work_summary(intervals, calls, *, now, start_at=None, end_at=None):
    by_rep, stale = {}, 0
    for row in intervals:
        start = timestamp(row.get("started_at"))
        end = timestamp(row.get("ended_at"))
        lease = timestamp(row.get("lease_until"))
        if not end:
            if not lease:
                continue
            stale += (
                lease < now and (start_at is None or lease >= start_at) and (end_at is None or lease < end_at)
            )
            end = min(now, lease)
        if start and start <= now and end >= start:
            by_rep.setdefault(row["rep_id"], []).append((start, min(now, end)))
    for row in calls:
        start, end = call_work_bounds(row)
        if start and end and end >= start and start <= now and row.get("rep_id"):
            by_rep.setdefault(row["rep_id"], []).append((start, min(now, end)))
    totals = {}
    for rep, spans in by_rep.items():
        end, total = None, 0.0
        for start, stop in sorted(spans):
            if start_at is not None:
                start = max(start, start_at)
            if end_at is not None:
                stop = min(stop, end_at)
            if (
                stop < start
                or (start_at is not None and stop == start_at)
                or (end_at is not None and start == end_at)
            ):
                continue
            if end is None or start > end:
                total += (stop - start).total_seconds()
            elif stop > end:
                total += (stop - end).total_seconds()
            end = max(end, stop) if end else stop
        if end is not None:
            totals[rep] = total
    return {
        "seconds": sum(totals.values()) if totals else None,
        "by_rep": totals,
        "stale_intervals": stale,
        "source": "declared_presence_heartbeat_and_verified_calls",
        "lease_seconds": WORK_LEASE_SECONDS,
        "note": WORK_NOTE,
    }


def connect_health_counts(values, *, min_support=30):
    count, base = values.get("connect_recent_n", 0), values.get("connect_reference_n", 0)
    successes, reference = values.get("connect_recent_s", 0), values.get("connect_reference_s", 0)
    unknown = values.get("connect_recent_unknown", 0)
    low, high = wilson(successes, count)
    ref_low, ref_high = wilson(reference, base)
    supported = count >= min_support and base >= min_support
    return {
        "status": "warning"
        if supported and high < ref_low
        else "observed"
        if supported
        else "insufficient_data",
        "samples": count,
        "unknown": unknown,
        "reference_samples": base,
        "rate": successes / count if count else None,
        "all_attempt_rate": successes / (count + unknown) if count + unknown else None,
        "confidence_low": low,
        "confidence_high": high,
        "reference_low": ref_low,
        "reference_high": ref_high,
        "reference_rate": reference / base if base else None,
        "comparison": "Last 24 hours versus preceding 27 days; 95% Wilson intervals. Descriptive, not adjusted for changes in the list or proof of a spam label.",
    }


class WorkEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    started_at: str
    ended_at: str
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("started_at", "ended_at")
    @classmethod
    def time_with_zone(cls, value):
        from datetime import datetime

        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Include a timezone offset")
        return parsed.isoformat()


class Measurement:
    def record_attempt_work_in(self, s, p, call, *, closing=False):
        """A call-scoped wrap-up union, separate from call edit versions and ready time."""
        model = MODELS["outcomes"]
        key = "attempt-work:" + call.id
        row = s.scalar(
            select(model).where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.external_key == key,
            )
        )
        if closing and (not row or row.data.get("closed")):
            return
        now = self.clock()
        ended = timestamp(call.data.get("ended_at"))
        previous = timestamp(row.data.get("heartbeat_at")) if row else ended
        if row and row.data.get("closed"):
            previous = now
        spans = list(row.data.get("rep_work_intervals", [])) if row else []
        if (
            previous
            and ended
            and ended <= previous <= now
            and (now - previous).total_seconds() <= WORK_LEASE_SECONDS
        ):
            if spans and timestamp(spans[-1]["ended_at"]) == previous:
                spans[-1] = {**spans[-1], "ended_at": now.isoformat()}
            else:
                spans.append({"started_at": previous.isoformat(), "ended_at": now.isoformat()})
        values = {
            "kind": "attempt_work",
            "call_id": call.id,
            "rep_id": p["user_id"],
            "heartbeat_at": now.isoformat(),
            "rep_work_intervals": spans[:64],
            "rep_work_incomplete": len(spans) > 64 or bool(row and row.data.get("rep_work_incomplete")),
            "source": "wrapup_heartbeat",
            "closed": closing,
        }
        if row:
            update(row, **values)
        else:
            put(s, "outcomes", p["tenant_id"], p["seller_id"], values, key=key, now=now)

    def open_work_in(self, s, p):
        model = MODELS["work_intervals"]
        return list(
            s.scalars(
                select(model).where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.op_owner_id == p["user_id"],
                    model.external_key.is_not(None),
                )
            )
        )

    def pause_work_in(self, s, p):
        self.end_wrapup_work_in(s, p, self.required(s, "users", p["user_id"], p))
        for row in self.open_work_in(s, p):
            update(
                row,
                ended_at=min(self.clock(), timestamp(row.data["lease_until"])).isoformat(),
                close_reason="paused",
            )
            row.external_key = None

    def end_wrapup_work_in(self, s, p, user):
        call_id = user.data.get("presence", {}).get("wrapup_call_id")
        if call_id:
            call = self.required(s, "call_attempts", call_id, p)
            if call.data.get("rep_id") == p["user_id"]:
                self.record_attempt_work_in(s, p, call, closing=True)
            update(user, presence={**user.data.get("presence", {}), "wrapup_call_id": None})

    def record_work_in(self, s, p, active):
        now = self.clock()
        key = "work:" + p.get("session_id", p["user_id"])
        model = MODELS["work_intervals"]
        row = s.scalar(
            select(model).where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.external_key == key,
            )
        )
        if row and (not active or timestamp(row.data["lease_until"]) < now):
            update(
                row,
                ended_at=min(now, timestamp(row.data["lease_until"])).isoformat(),
                close_reason="paused" if not active else "heartbeat_expired",
            )
            row.external_key = None
            s.flush()
            row = None
        if active:
            if row:
                update(
                    row,
                    heartbeat_at=now.isoformat(),
                    lease_until=(now + timedelta(seconds=WORK_LEASE_SECONDS)).isoformat(),
                )
            else:
                row = put(
                    s,
                    "work_intervals",
                    p["tenant_id"],
                    p["seller_id"],
                    {
                        "rep_id": p["user_id"],
                        "started_at": now.isoformat(),
                        "ended_at": None,
                        "heartbeat_at": now.isoformat(),
                        "lease_until": (now + timedelta(seconds=WORK_LEASE_SECONDS)).isoformat(),
                        "source": "declared_presence",
                    },
                    key=key,
                    now=now,
                )
        return doc(row) if row else None

    def heartbeat(self, p, activity):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            user = self.required(s, "users", p["user_id"], p)
            presence = user.data.get("presence", {})
            if activity != "wrapup":
                self.end_wrapup_work_in(s, p, user)
            if activity == "ready" and presence.get("status") not in {"ready", "busy"}:
                fail(409, "Explicitly go ready before resuming availability")
            if activity in {"call", "wrapup"}:
                calls = MODELS["call_attempts"]
                state_filter = (
                    calls.op_status.not_in(TERMINAL)
                    if activity == "call"
                    else calls.op_status.in_(TERMINAL - {"FINALIZED"})
                )
                call = s.scalar(
                    select(calls)
                    .where(
                        calls.tenant_id == p["tenant_id"],
                        calls.seller_id == p["seller_id"],
                        calls.op_owner_id == p["user_id"],
                        state_filter,
                    )
                    .order_by(calls.op_at.desc())
                    .limit(1)
                )
                ended = timestamp(call.data.get("ended_at")) if call else None
                began = (
                    timestamp(call.data.get("submitted_at") or call.data.get("started_at")) if call else None
                )
                expired = bool(
                    call
                    and began
                    and self.clock()
                    >= began
                    + timedelta(seconds=call.data.get("max_call_seconds", self.settings.max_call_seconds))
                )
                if (
                    not call
                    or call.data.get("detached")
                    or not (call.data.get("submitted_at") or call.data.get("provider_id"))
                    or activity == "call"
                    and expired
                    or activity == "wrapup"
                    and (not ended or self.clock() - ended > timedelta(minutes=15))
                ):
                    fail(409, "No current owned call or recent unfinished wrap-up supports this heartbeat")
                if activity == "wrapup":
                    self.record_attempt_work_in(s, p, call)
            interval = self.record_work_in(s, p, activity != "idle")
            other_active = activity == "idle" and any(
                timestamp(row.data["lease_until"]) > self.clock() for row in self.open_work_in(s, p)
            )
            update(
                user,
                presence={
                    **presence,
                    "wrapup_call_id": call.id if activity == "wrapup" else None,
                    "at": self.clock().isoformat(),
                    **({"status": "paused"} if activity == "idle" and not other_active else {}),
                },
            )
            return {"interval": interval, "activity": activity, "presence": user.data["presence"]}

    def work_intervals(self, p):
        with self.db.transaction(False) as s:
            model = MODELS["work_intervals"]
            query = (
                select(model)
                .where(model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"])
                .order_by(model.created_at.desc())
                .limit(100)
            )
            if p["role"] == "rep":
                query = query.where(model.op_owner_id == p["user_id"])
            records = [doc(r) for r in s.scalars(query)]
            return {"intervals": records, "limit": 100, "lease_seconds": WORK_LEASE_SECONDS}

    def edit_work(self, p, identifier, data):
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            row = self.required(s, "work_intervals", identifier, p)
            if p["role"] == "rep" and row.data["rep_id"] != p["user_id"]:
                fail(403, "Working interval belongs to another rep")
            if row.version != data["expected_version"]:
                fail(409, "Working interval changed; refresh before correcting")
            start, end = timestamp(data["started_at"]), timestamp(data["ended_at"])
            if not row.data.get("ended_at") and timestamp(row.data["lease_until"]) >= self.clock():
                fail(409, "Pause before correcting an active working interval")
            if end < start or end > self.clock() or end - start > timedelta(hours=24):
                fail(422, "Use a past interval of at most 24 hours with its end after its start")
            before = {k: row.data.get(k) for k in ("started_at", "ended_at")}
            update(
                row,
                started_at=start.isoformat(),
                ended_at=end.isoformat(),
                corrected_by=p["user_id"],
                correction_reason=data["reason"],
            )
            row.external_key = None
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "work.corrected",
                row.id,
                self.clock(),
                {"before": before, "reason": data["reason"]},
            )
            return doc(row)


def call_timing(call, event_type, payload, *, now):
    """Source event time is independent of delivery order and never revives a leg."""
    try:
        at = timestamp(payload.get("occurred_at")) or now
        started, ended = timestamp(call.get("started_at")), timestamp(call.get("ended_at"))
    except (ValueError, TypeError):
        return {}
    if not started or at < started or at > now:
        return {}
    values = {}
    if event_type == "call.answered" and (not ended or at <= ended):
        answered = timestamp(call.get("answered_at"))
        values["answered_at"] = min(answered, at).isoformat() if answered else at.isoformat()
        values["answer_timing_source"] = payload.get("timing_source", "receipt")
    elif event_type == "call.hangup":
        values["ended_at"] = min(ended, at).isoformat() if ended else at.isoformat()
    answered = timestamp(values.get("answered_at") or call.get("answered_at"))
    ended = timestamp(values.get("ended_at") or call.get("ended_at"))
    if answered and ended and ended >= answered:
        values["connected_seconds"] = (ended - answered).total_seconds()
    return values
