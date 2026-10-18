"""A bounded, in-process assignment of the next due attempt to declared staffed slots.

This is a forecast, never a calling authorization. It uses the existing number
identity, reviewed daily workload and pooled staffed time; no external solver.
"""

import math
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import UTC, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.optimization.numbering import number_ready, timestamp

MAX_SLOTS = 10000
MAX_SEARCH_NODES = 20000
MAX_PLANNING_STEPS = 200000


def work_items(leads, now, end, policy):
    work, unknown = [], 0
    for lead in leads:
        if (
            lead.get("status", "eligible") != "eligible"
            or lead.get("suppressed")
            or lead.get("calling_permission") == "denied"
            or lead.get("outreach_hold")
            or lead.get("goal_completed")
            or lead.get("attempt_count", 0) >= policy.get("max_attempts", 4)
        ):
            continue
        try:
            zone = ZoneInfo(lead.get("timezone") or "unknown")
        except (ZoneInfoNotFoundError, ValueError):
            unknown += 1
            continue
        due = max(
            now, timestamp(lead.get("next_attempt_at")) or now, timestamp(lead.get("callback_due_at")) or now
        )
        if lead.get("last_attempt_at"):
            due = max(
                due,
                timestamp(lead["last_attempt_at"]) + timedelta(seconds=policy.get("min_retry_seconds", 3600)),
            )
        if due < end:
            work.append(
                {
                    **lead,
                    "zone": zone,
                    "due": due,
                    "deadline": timestamp(lead.get("callback_window_end")) or end,
                }
            )
    return sorted(
        work,
        key=lambda w: (
            not bool(w.get("callback_due_at")),
            w["deadline"],
            not bool(w.get("bound_number_id")),
            timestamp(w.get("coverage_due_at")) or end,
            -float(w.get("expected_conversion") or 0),
            w["id"],
        ),
    ), unknown


def staffed_slots(windows, now, end, seconds):
    slots = []
    duration = timedelta(seconds=seconds)
    for window in windows:
        start = max(now, timestamp(window["start_at"]))
        finish = min(end, timestamp(window["end_at"]))
        while start + duration <= finish:
            if len(slots) >= MAX_SLOTS:
                return slots, True
            slots.append((start, int(window["reps"])))
            start += duration
    return sorted(slots), False


class Assignment:
    def __init__(self, work, slots, now, policy, seconds, rep_limit):
        self.work, self.slots, self.now = work, slots, now
        self.seconds, self.rep_limit = seconds, rep_limit
        self.steps, self.exhausted = 0, False
        duration = timedelta(seconds=seconds)
        self.allowed, by_zone = [], {}
        starts = [at for at, _ in slots]
        for item in work:
            key = item["zone"].key
            if key not in by_zone:
                by_zone[key] = tuple(
                    i
                    for i, (at, _) in enumerate(slots)
                    if policy.get("local_start_hour", 9)
                    <= at.astimezone(item["zone"]).hour
                    < policy.get("local_end_hour", 20)
                    and policy.get("local_start_hour", 9)
                    <= (at + duration - timedelta(microseconds=1)).astimezone(item["zone"]).hour
                    < policy.get("local_end_hour", 20)
                    and at.astimezone(item["zone"]).date()
                    == (at + duration - timedelta(microseconds=1)).astimezone(item["zone"]).date()
                    and at.astimezone(item["zone"]).weekday() in policy.get("calling_weekdays", range(7))
                    and at.astimezone(item["zone"]).date().isoformat() not in policy.get("closed_dates", [])
                )
            allowed = by_zone[key]
            first = bisect_left(allowed, bisect_left(starts, item["due"]))
            last = bisect_left(allowed, bisect_right(starts, item["deadline"] - duration))
            self.allowed.append((allowed, first, last))

    def solve(self, inventory, *, ignore_bindings=False):
        numbers = {n["id"]: n for n in inventory if number_ready(n)}
        compatible = []
        for work in self.work:
            bound = numbers.get(work.get("bound_number_id")) if not ignore_bindings else None
            compatible.append(
                [bound]
                if bound
                else sorted(
                    [
                        n
                        for n in numbers.values()
                        if n.get("region", "general") in {work.get("region", "general"), "general"}
                    ],
                    key=lambda n: (
                        n.get("region", "general") == "general",
                        n.get("proposed", False),
                        -(n.get("health", {}).get("score") or 0),
                        n["id"],
                    ),
                )
            )
        slot_use, number_use, daily_use = Counter(), Counter(), Counter()
        assigned = []

        def choices(index):
            allowed, first, last = self.allowed[index]
            for offset in range(first, last):
                if self.exhausted:
                    return
                self.steps += 1
                if self.steps > MAX_PLANNING_STEPS:
                    self.exhausted = True
                    return
                slot = allowed[offset]
                at, reps = self.slots[slot]
                if slot_use[slot] >= reps:
                    continue
                day = at.astimezone(UTC).date().isoformat()
                for number in compatible[index]:
                    self.steps += 1
                    if self.steps > MAX_PLANNING_STEPS:
                        self.exhausted = True
                        return
                    identity = number["id"]
                    if timestamp(number.get("ready_at")) and at < timestamp(number["ready_at"]):
                        continue
                    busy = number.get("active_obligations", 0)
                    if timestamp(number.get("busy_until")) and at >= timestamp(number["busy_until"]):
                        busy = 0
                    if number_use[(identity, slot)] + busy >= number.get("capacity", 1):
                        continue
                    limit = number.get("workload_schedule", {}).get(day, number.get("daily_limit", 0))
                    debit = (
                        (number.get("used_today", 0) + number.get("reserved_attempts", 0))
                        if day == self.now.astimezone(UTC).date().isoformat()
                        else 0
                    )
                    if daily_use[(identity, day)] + debit >= limit:
                        continue
                    yield slot, identity, day

        def change(index, option, delta):
            slot, identity, day = option
            slot_use[slot] += delta
            number_use[(identity, slot)] += delta
            daily_use[(identity, day)] += delta
            if delta > 0:
                assigned.append((index, slot, identity))
            else:
                assigned.pop()

        for index in range(len(self.work)):
            if self.exhausted or len(assigned) >= self.rep_limit:
                break
            option = next(choices(index), None)
            if option:
                change(index, option, 1)

        def value(items):
            return sum(bool(self.work[i].get("callback_due_at")) for i, _, _ in items), len(items)

        best = list(assigned)
        ceiling = min(len(self.work), self.rep_limit, sum(reps for _, reps in self.slots))
        maximum = (min(ceiling, sum(bool(w.get("callback_due_at")) for w in self.work)), ceiling)
        exact = value(best) == maximum
        # Exhaustive small fixtures; larger workloads retain an explicitly labelled feasible result.
        if (
            not exact
            and not self.exhausted
            and len(self.work) <= 8
            and len(self.slots) <= 32
            and len(numbers) <= 12
        ):
            slot_use.clear()
            number_use.clear()
            daily_use.clear()
            assigned.clear()
            nodes, stopped = 0, False

            def search(index):
                nonlocal best, nodes, stopped
                nodes += 1
                if nodes > MAX_SEARCH_NODES or self.exhausted:
                    stopped = True
                    return
                if value(assigned) > value(best):
                    best = list(assigned)
                if (
                    value(best) == maximum
                    or index == len(self.work)
                    or len(assigned) >= self.rep_limit
                    or (
                        value(assigned)[0] + sum(bool(w.get("callback_due_at")) for w in self.work[index:]),
                        len(assigned) + len(self.work) - index,
                    )
                    <= value(best)
                ):
                    return
                for option in choices(index):
                    change(index, option, 1)
                    search(index + 1)
                    change(index, option, -1)
                    if stopped or value(best) == maximum:
                        return
                search(index + 1)

            search(0)
            exact = not stopped or value(best) == maximum
        return sorted(
            [
                {
                    "lead_id": self.work[index]["id"],
                    "region": self.work[index].get("region") or "general",
                    "at": self.slots[slot][0].isoformat(),
                    "number_id": identity,
                    "proposed": bool(numbers[identity].get("proposed")),
                    "callback": bool(self.work[index].get("callback_due_at")),
                }
                for index, slot, identity in best
            ],
            key=lambda item: (item["at"], not item["callback"], item["lead_id"]),
        ), exact


def time_forecast(
    leads,
    inventory,
    *,
    now,
    horizon_hours,
    rep_minutes,
    handling_seconds,
    daily_capacity_per_number,
    policy,
    staffed_windows,
    additional_ready_after_hours,
):
    end = now + timedelta(hours=horizon_hours)
    work, unknown = work_items(leads, now, end, policy)
    slots, limited = staffed_slots(staffed_windows, now, end, handling_seconds)
    assignment = Assignment(
        work, slots, now, policy, handling_seconds, int(rep_minutes * 60 / handling_seconds)
    )
    existing, existing_exact = assignment.solve(inventory)
    relaxed = {
        "id": "forecast-unlimited",
        "region": "general",
        "lifecycle": "healthy",
        "daily_limit": len(work),
        "capacity": max(1, len(work)),
    }
    staffed, staffed_exact = assignment.solve([relaxed], ignore_bindings=True)
    bound, bound_exact = assignment.solve([*inventory, relaxed])
    delayed = {**relaxed, "ready_at": (now + timedelta(hours=additional_ready_after_hours)).isoformat()}
    target, target_exact = assignment.solve([*inventory, delayed])

    def objective(schedule):
        return sum(item["callback"] for item in schedule), len(schedule)

    target = max((existing, target), key=objective)
    target_count = len(target)

    def additions(base, initial):
        best, added, checked = initial, 0, True
        for count in range(1, min(len(work), 100) + 1):
            if assignment.exhausted or objective(best) >= objective(target):
                break
            candidates = [
                {
                    **delayed,
                    "id": f"proposed-{i + 1}",
                    "daily_limit": daily_capacity_per_number,
                    "capacity": 1,
                    "proposed": True,
                }
                for i in range(count)
            ]
            planned, exact = assignment.solve([*base, *candidates])
            checked = checked and exact
            if objective(planned) > objective(best):
                best, added = planned, count
        return best, added, checked

    best, added, checked = additions(inventory, existing)
    proven = checked and existing_exact and target_exact and staffed_exact and bound_exact
    ready = {n["id"]: n for n in inventory if number_ready(n)}
    waiting = [
        {
            **n,
            "lifecycle": "healthy",
            "identity_configured": True,
            "callback_tested": True,
            "daily_limit": n.get("daily_limit") or daily_capacity_per_number,
        }
        for n in inventory
        if not number_ready(n)
        and n.get("lifecycle")
        in {"purchased", "identity_configured", "callback_tested", "investigating", "paused_for_outbound"}
        and n.get("provider_present") is not False
        and not n.get("provider_recording_enabled")
        and n.get("region", "general") in {"general", *(w.get("region", "general") for w in work)}
    ]
    waiting_ids = {n["id"] for n in waiting}
    pending_schedule, proposed = [], added
    if waiting and not assignment.exhausted:
        conditional = [*ready.values(), *waiting]
        pending_schedule, _ = assignment.solve(conditional)
        _, proposed, _ = additions(conditional, pending_schedule)
    limited = limited or assignment.exhausted or objective(best) < objective(target)
    days = len({at.astimezone(UTC).date() for at, _ in slots})
    lower_bound = (
        math.ceil(max(0, target_count - len(existing)) / max(1, daily_capacity_per_number * days))
        if existing_exact
        else 0
    )
    regions = []
    for region in sorted({w.get("region") or "general" for w in work}):
        old = [s for s in existing if s["region"] == region]
        new = [s for s in best if s["region"] == region]
        regional = sum(ready[s["number_id"]].get("region", "general") != "general" for s in old)
        regions.append(
            {
                "region": region,
                "forecast_attempts": sum(w.get("region", "general") == region for w in work),
                "planned_attempts": len(new),
                "eligible_leads": sum(w.get("region", "general") == region for w in work),
                "healthy_existing": sum(n.get("region", "general") == region for n in ready.values()),
                "usable_existing_capacity": regional,
                "general_fallback_capacity": len(old) - regional,
                "capacity_waiting_for_review": sum(
                    item["region"] == region and item["number_id"] in waiting_ids for item in pending_schedule
                ),
                "outbound_capacity_shortfall": max(0, len(new) - len(old)),
                "callback_demand": sum(s["callback"] for s in new),
                "required": 0,
                "proposed_additional": None if limited else proposed if not regions else 0,
                "proposed_pool": "general",
                "incremental_capacity_used": len(new) - len(old),
                "reason": "Next-attempt schedule using declared staffed time and stable identities; all live permissions are rechecked.",
            }
        )
    return {
        "regions": regions,
        "horizon_hours": horizon_hours,
        "rep_capacity_attempts": min(
            int(rep_minutes * 60 / handling_seconds), sum(reps for _, reps in slots)
        ),
        "forecast_attempts": len(work),
        "existing_only_capacity": len(existing),
        "existing_plus_addition_capacity": len(best),
        "staffing_shortfall": None if limited else max(0, len(work) - len(staffed)),
        "binding_shortfall": None if limited else max(0, len(staffed) - len(bound)),
        "setup_shortfall": None if limited else max(0, len(bound) - target_count),
        "number_shortfall": None if limited else max(0, target_count - len(existing)),
        "capacity_shortfall": len(work) - len(existing) + unknown,
        "window_unknown_leads": unknown,
        "minimum_additional_numbers": None if limited else added,
        "proposed_additional_numbers": None if limited else proposed,
        "lower_bound": added if proven and not limited else lower_bound,
        "optimality": "proven_small_case" if proven and not limited else "best_feasible_found",
        "planning_limited": limited,
        "planning_steps": assignment.steps,
        "optimality_scope": "Next due attempts on the declared staffing grid; pooled rep availability, reviewed DID workload and setup assumptions. Future retries require observed outcomes and a new forecast.",
        "schedule_scope": "declared_staffed_windows",
        "existing_schedule": existing[:1000],
        "expanded_schedule": best[:1000],
        "schedule_rows_omitted": max(0, len(best) - 1000),
        "purchased": False,
        "approval_required": True,
        "purchase_allowed": False,
        "purchase_blocker": "New rentals are recurring commitments",
        "estimate_requires_provider_quote": True,
        "monthly_rental_estimate_usd": None if limited else proposed,
        "estimate_note": "Reference rental estimate only. New rental commitments remain blocked; resolve waiting inventory before any additions.",
        "cold_start": not any(n.get("health", {}).get("samples", 0) for n in inventory),
        "expected_conversion_lift": None,
        "assumptions": {
            "daily_slots_per_added_number": daily_capacity_per_number,
            "handling_seconds": handling_seconds,
            "rep_minutes": rep_minutes,
            "additional_ready_after_hours": additional_ready_after_hours,
            "retry_demand": "One next due attempt per eligible lead; later retries depend on its observed outcome",
            "future_readiness": "Proposed numbers require completed setup; waiting inventory is not usable",
            "rep_ownership": "Pooled declared staffing; callback owner availability is rechecked before calling",
        },
        "as_of": now.isoformat(),
    }
