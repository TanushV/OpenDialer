"""Shared outcome definitions and transparent, heartbeat-supported working intervals."""

from collections import Counter, defaultdict
from datetime import UTC, timedelta
from math import ceil
from zoneinfo import ZoneInfo

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


def delivery_values(row):
    from app.scheduling.models import derive_labels

    outcome = row.get("outcome") or row.get("carrier_outcome")
    label = derive_labels({**row, "outcome": outcome})
    disposition = row.get("disposition_input") or {}
    audio = disposition.get("audio_result")
    human = label["human"] is True or (label["human"] is None and bool(row.get("human_answered_at")))
    screened = bool(disposition.get("screened") or row.get("screening_detected") or outcome == "screening")
    connect = human and audio == "two_way"
    answer = timestamp(row.get("answered_at"))
    bridge = timestamp(row.get("bridged_at"))
    latency = (
        (bridge - answer).total_seconds()
        if answer and bridge and bridge >= answer and row.get("answer_timing_source") == "provider"
        else None
    )
    cause, duration = row.get("provider_hangup_cause"), row.get("connected_seconds")
    ended = bool(row.get("ended_at"))
    return Counter(
        attempts=1,
        network_answers=int(bool(answer)),
        human_pickups=int(human),
        connects=int(connect),
        intended_prospect=int(connect and label["right_party"] is True),
        screened=int(screened),
        screened_to_human=int(screened and human),
        voicemail=int(outcome == "voicemail"),
        ivr=int(outcome == "ivr"),
        carrier_blocks=int(bool(row.get("sip_block_code"))),
        failures=int(
            outcome in {"temporary_failure", "invalid_endpoint"}
            or row.get("end_reason") == "provider_rejected"
        ),
        conversations=int(
            connect
            and outcome
            in {
                "right_party",
                "qualified_conversation",
                "callback_requested",
                "meeting_booked",
                "meeting_held",
                "sale_won",
            }
        ),
        audio_problems=int(audio == "problem"),
        audio_unconfirmed=int(human and audio not in {"two_way", "problem"}),
        timed_answered=int(duration is not None),
        short_answered=int(duration is not None and duration <= 6),
        ended_attempts=int(ended),
        pre_answer_drops=int(
            ended
            and not answer
            and cause not in {"timeout", "no_answer", "user_busy"}
            and row.get("provider_hangup_source") == "caller"
        ),
        ring_timeouts=int(cause == "timeout"),
        no_answer=int(outcome == "no_answer"),
    ), latency


def delivery_summary(counts, latencies):
    attempts = counts["attempts"]
    short = counts["short_answered"] / counts["timed_answered"] if counts["timed_answered"] else None
    drops = counts["pre_answer_drops"] / counts["ended_attempts"] if counts["ended_attempts"] else None
    p95 = sorted(latencies)[ceil(len(latencies) * 0.95) - 1] if latencies else None
    return {
        **counts,
        "attempts": attempts,
        "connects": counts["connects"],
        "inbound_callbacks": counts["inbound_callbacks"],
        "connect_rate": counts["connects"] / attempts if attempts else None,
        "short_answered_rate": short,
        "pre_answer_drop_rate": drops,
        "answer_to_bridge_p95_seconds": p95,
        "answer_to_bridge_samples": len(latencies),
        "warnings": [
            message
            for exceeded, message in (
                (
                    short is not None and short >= 0.12,
                    "Short answered calls reached the 12% warning threshold.",
                ),
                (drops is not None and drops >= 0.15, "Pre-answer drops reached the 15% warning threshold."),
                (p95 is not None and p95 >= 0.5, "Answer-to-bridge p95 is 0.5 seconds or more."),
            )
            if exceeded
        ],
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
    def connect_report(self, p, days):
        now = self.clock()
        start = now - timedelta(days=days)
        rolling = now - timedelta(days=28)
        zone = ZoneInfo("America/Los_Angeles")
        aggregates = defaultdict(Counter)
        latencies = defaultdict(list)
        with self.db.transaction(False) as s:
            calls, tasks = MODELS["call_attempts"], MODELS["followup_tasks"]
            numbers = {n.id: n.data["phone"] for n in self.scoped(s, "number_inventory", p)}
            attempts = list(
                s.scalars(
                    select(calls).where(
                        calls.tenant_id == p["tenant_id"],
                        calls.seller_id == p["seller_id"],
                        calls.op_at >= start,
                    )
                )
            )
            endpoint_ids = sorted({row.data.get("endpoint_id") for row in attempts} - {None})
            endpoints = MODELS["phone_endpoints"]
            carrier_by_endpoint = {}
            for offset in range(0, len(endpoint_ids), 500):
                batch = endpoint_ids[offset : offset + 500]
                for endpoint_id, data in s.execute(
                    select(endpoints.id, endpoints.data).where(
                        endpoints.tenant_id == p["tenant_id"],
                        endpoints.seller_id == p["seller_id"],
                        endpoints.id.in_(batch),
                    )
                ):
                    carrier_by_endpoint[endpoint_id] = (data.get("carrier_lookup") or {}).get("carrier")
            callbacks = list(
                s.execute(
                    select(tasks.created_at, tasks.data).where(
                        tasks.tenant_id == p["tenant_id"],
                        tasks.seller_id == p["seller_id"],
                        tasks.created_at >= start,
                    )
                )
            )
        test_attempts = set()
        for row in attempts:
            data = row.data
            disposition = data.get("disposition_input") or {}
            if disposition.get("delivery_test"):
                test_attempts.add(row.id)
                continue
            if data.get("synthetic") and not self.settings.simulation:
                continue
            at = timestamp(
                data.get("submitted_at") or (data.get("started_at") if data.get("provider_id") else None)
            )
            if not at or not start <= at <= now:
                continue
            number = numbers.get(data.get("number_id"), "Unknown")
            carrier = carrier_by_endpoint.get(data.get("endpoint_id")) or "Unknown"
            day = at.astimezone(zone).date()
            week = day - timedelta(days=day.weekday())
            keys = [("daily", str(day)), ("weekly", str(week)), ("overall", "total")]
            if at >= rolling:
                keys += [
                    ("rolling", "total"),
                    ("by_number", number),
                    ("by_carrier", carrier),
                    ("by_number_and_carrier", f"{number} · {carrier}"),
                ]
            counts, latency = delivery_values(data)
            for key in keys:
                aggregates[key].update(counts)
                if latency is not None:
                    latencies[key].append(latency)
        for created, data in callbacks:
            prior_attempt = data.get("previous_call_id")
            if (
                not data.get("inbound")
                or not prior_attempt
                or prior_attempt in test_attempts
                or not (data.get("inbound_verified") or self.settings.simulation)
            ):
                continue
            at = created if created.tzinfo else created.replace(tzinfo=UTC)
            if at > now:
                continue
            day = at.astimezone(zone).date()
            week = day - timedelta(days=day.weekday())
            keys = [("daily", str(day)), ("weekly", str(week)), ("overall", "total")]
            if at >= rolling:
                keys += [("rolling", "total"), ("by_number", numbers.get(data.get("number_id"), "Unknown"))]
            for key in keys:
                aggregates[key]["inbound_callbacks"] += 1

        def summary(key):
            return delivery_summary(aggregates[key], latencies[key])

        return {
            "simulation": self.settings.simulation,
            "overall": summary(("overall", "total")),
            "rolling_28_days": summary(("rolling", "total")),
            **{
                group: [
                    {"key": label, **summary((group, label))}
                    for kind, label in sorted(aggregates)
                    if kind == group
                ]
                for group in ("daily", "weekly", "by_number", "by_carrier", "by_number_and_carrier")
            },
        }

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
    elif event_type == "call.bridged" and (not ended or at <= ended):
        if payload.get("timing_source") == "provider":
            bridged = timestamp(call.get("bridged_at"))
            values["bridged_at"] = min(bridged, at).isoformat() if bridged else at.isoformat()
    elif event_type == "call.hangup":
        values["ended_at"] = min(ended, at).isoformat() if ended else at.isoformat()
    answered = timestamp(values.get("answered_at") or call.get("answered_at"))
    ended = timestamp(values.get("ended_at") or call.get("ended_at"))
    if answered and ended and ended >= answered:
        values["connected_seconds"] = (ended - answered).total_seconds()
    return values
