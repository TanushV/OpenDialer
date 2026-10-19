"""One bounded forecast over immutable inputs; never permission to place a call.

Integer occupancy masks keep the 30-second grid small. The same assignment
routine serves existing inventory, conditional readiness and added capacity.
"""

import hashlib
import itertools
import json
import math
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import RuntimeLimits
from app.optimization.numbering import number_ready, timestamp, wilson

_LIMITS = RuntimeLimits()
MAX_SLOTS = _LIMITS.planning_time_buckets
MAX_SEARCH_NODES = _LIMITS.planning_nodes
MAX_PLANNING_STEPS = _LIMITS.planning_operations


@dataclass(frozen=True)
class Availability:
    rep_id: str | None
    start_at: datetime
    end_at: datetime
    anonymous_capacity: int = 0
    pool_id: str = "default"


@dataclass(frozen=True)
class AttemptWork:
    lead_id: str
    endpoint_id: str
    next_attempt_index: int
    priority_class: int
    earliest_at: datetime
    deadline: datetime
    allowed_local_windows: tuple
    owner_id: str | None
    bound_number_id: str | None
    predicted_work_seconds: float
    outcome_estimates: tuple
    timezone: str
    region: str
    segment: str
    source_version: int = 1
    reassignment_approved: bool = False
    previous_attempt_index: int | None = None

    @property
    def value(self):
        return max((v for _, v in self.outcome_estimates), default=0.0)


@dataclass(frozen=True)
class NumberCapacity:
    number_id: str
    region: str
    available_at: datetime | None
    lifecycle: str
    daily_limits: tuple
    consumed_attempts: int
    pending_reservations: int
    concurrent_limit: int
    active_leg_intervals: tuple
    health_version: int
    usable: bool
    proposed: bool = False
    quote_usd: float | None = None
    source_version: int = 1
    burst_limit: int | None = None
    burst_seconds: int = 60

    def limit(self, day):
        values = dict(self.daily_limits)
        return values.get(day, values.get("default", 0))


@dataclass(frozen=True)
class PlanningInput:
    version: int
    as_of: datetime
    horizon_end: datetime
    policy_version: str
    goal_version: str
    availability: tuple[Availability, ...]
    work: tuple[AttemptWork, ...]
    inventory: tuple[NumberCapacity, ...]
    obligations: tuple
    estimate_version: str
    policy_json: str
    rep_seconds: float
    proposal_policy: tuple
    retry_evidence_digest: str


@dataclass(frozen=True)
class PlanningResult:
    as_of: str
    input_fingerprint: str
    demand_definition: str
    known_next_attempts: int
    contingent_retry_low_mean_high: tuple
    existing_schedule: tuple
    hypothetical_added_schedule: tuple
    minimum_additions: int | None
    best_found_additions: int
    lower_bound: int
    proven_on_declared_grid: bool
    search_complete: bool
    shortfall_by_reason: tuple
    omitted_rows: tuple
    assumptions: tuple
    source_versions: tuple


@dataclass
class SearchBudget:
    operations: int = 0
    nodes: int = 0
    exhausted: bool = False

    def spend(self, *, node=False):
        if self.operations >= MAX_PLANNING_STEPS or node and self.nodes >= MAX_SEARCH_NODES:
            self.exhausted = True
            return False
        self.operations += 1
        self.nodes += int(node)
        return True


def priority_class(lead, now):
    if lead.get("callback_due_at"):
        return 0
    if lead.get("requested_contact"):
        return 1
    if timestamp(lead.get("coverage_due_at")) and timestamp(lead["coverage_due_at"]) <= now:
        return 2
    return 3


def work_items(leads, now, end, policy, handling_seconds=120):
    work, unknown = [], 0
    for lead in sorted(leads, key=lambda row: row["id"]):
        if (
            lead.get("status", "eligible") != "eligible"
            or lead.get("suppressed")
            or lead.get("calling_permission") == "denied"
            or lead.get("outreach_hold")
            or lead.get("goal_completed")
            or lead.get("active_obligation")
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
        if due >= end:
            continue
        duration = lead.get("predicted_work_seconds", handling_seconds)
        if not isinstance(duration, (float, int)) or not math.isfinite(duration) or duration <= 0:
            duration = handling_seconds
        value = lead.get("expected_conversion")
        if value is None:
            estimates = lead.get("outcome_estimates") or {}
            value = math.prod(
                estimates.get(k, 0)
                for k in ("p_human", "p_right_party_given_human", "p_conversion_given_right_party")
            )
        value = float(value) if isinstance(value, (float, int)) and math.isfinite(value) else 0.0
        work.append(
            AttemptWork(
                lead["id"],
                lead.get("endpoint_id", lead["id"]),
                lead.get("attempt_count", 0) + 1,
                priority_class(lead, now),
                due,
                min(end, timestamp(lead.get("callback_window_end")) or end),
                (
                    policy.get("local_start_hour", 9),
                    policy.get("local_end_hour", 20),
                    tuple(policy.get("calling_weekdays", range(7))),
                    tuple(sorted(policy.get("closed_dates", []))),
                ),
                lead.get("callback_owner_id") if lead.get("callback_due_at") else None,
                lead.get("bound_number_id"),
                float(duration),
                (("conversion", max(0.0, min(1.0, value))),)
                + tuple(
                    sorted(
                        ("slot:" + key, float(v))
                        for key, v in lead.get("slot_estimates", {}).items()
                        if isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 1
                    )
                ),
                zone.key,
                lead.get("region") or "general",
                str(lead.get("segment") or "unknown"),
                lead.get("version", 1),
                lead.get("reassignment_approved") is True,
            )
        )
    return sorted(
        work,
        key=lambda w: (
            w.priority_class,
            w.deadline,
            -w.value / w.predicted_work_seconds,
            w.earliest_at,
            w.lead_id,
        ),
    ), unknown


def normalize_availability(windows, now, end, rep_minutes, policy):
    if not windows:
        zone = ZoneInfo(policy.get("staffing_timezone", "UTC"))
        day = now.astimezone(zone).date()
        remaining = rep_minutes * 60
        windows = []
        while day <= end.astimezone(zone).date() and remaining > 0:
            if day.weekday() in policy.get("staffing_weekdays", policy.get("calling_weekdays", range(7))):
                start = datetime.combine(day, time(), zone) + timedelta(
                    hours=policy.get("staffing_start_hour", policy.get("local_start_hour", 9))
                )
                finish = datetime.combine(day, time(), zone) + timedelta(
                    hours=policy.get("staffing_end_hour", policy.get("local_end_hour", 20))
                )
                start, finish = max(now, start.astimezone(UTC)), min(end, finish.astimezone(UTC))
                count = policy.get("staffing_reps", 1)
                if finish > start:
                    finish = min(finish, start + timedelta(seconds=remaining / count))
                    windows.append({"start_at": start, "end_at": finish, "reps": count})
                    remaining -= (finish - start).total_seconds() * count
            day += timedelta(days=1)
    result = []
    for w in windows:
        if isinstance(w, Availability):
            result.append(w)
            continue
        start, finish = max(now, timestamp(w["start_at"])), min(end, timestamp(w["end_at"]))
        if finish <= start:
            continue
        if "rep_ids" in w:
            result.extend(
                Availability(rep, start, finish, 0, w.get("pool_id", "default"))
                for rep in sorted(set(w["rep_ids"]))
            )
        else:
            result.append(Availability(None, start, finish, w["reps"], w.get("pool_id", "default")))
    return tuple(sorted(result, key=lambda w: (w.start_at, w.end_at, w.pool_id, w.rep_id or "")))


def staffed_slots(windows, now, end, seconds, rep_busy_until=None):
    periods = normalize_availability(windows, now, end, 0, {})
    busy = {rep: timestamp(at) for rep, at in (rep_busy_until or {}).items()}
    events = defaultdict(list)
    for w in periods:
        start = max(w.start_at, busy.get(w.rep_id) or w.start_at)
        first = math.ceil((start - now).total_seconds() / seconds)
        stop = math.floor((w.end_at - now).total_seconds() / seconds)
        if stop > first:
            key = (w.pool_id, w.rep_id, w.anonymous_capacity)
            events[first].append((key, 1))
            events[stop].append((key, -1))
    active, slots, owners = Counter(), [], []
    bounds = sorted(events)
    for first, stop in zip(bounds, bounds[1:], strict=False):
        for key, delta in events[first]:
            active[key] += delta
        named, pools = set(), defaultdict(lambda: [set(), 0])
        for (pool, rep, count), uses in active.items():
            if uses <= 0:
                continue
            if rep is not None:
                named.add(rep)
                pools[pool][0].add(rep)
            else:
                pools[pool][1] = max(pools[pool][1], count)
        for index in range(first, stop):
            at = now + timedelta(seconds=index * seconds)
            roster = set(named)
            for pool, (members, total) in pools.items():
                # Named members of a declared total pool are not additional headcount.
                occupied = sum(rep not in named and at < until for rep, until in busy.items())
                extra = max(0, total - len(members) - occupied)
                roster.update(f"@{pool}:{i}" for i in range(extra))
            if roster:
                if len(slots) == MAX_SLOTS:
                    return slots, owners, True
                slots.append((at, len(roster)))
                owners.append(frozenset(roster))
    return slots, owners, False


def number_input(number, now, end):
    intervals = tuple(
        (timestamp(v["start_at"]), timestamp(v["end_at"]), v.get("count", 1))
        for v in number.get("active_leg_intervals", [])
    )
    if number.get("active_obligations", 0):
        intervals += ((now, timestamp(number.get("busy_until")) or end, number["active_obligations"]),)
    return NumberCapacity(
        number["id"],
        number.get("region") or "general",
        timestamp(number.get("available_at") or number.get("ready_at")),
        number.get("lifecycle", "unknown"),
        tuple(
            sorted({"default": number.get("daily_limit", 0), **number.get("workload_schedule", {})}.items())
        ),
        number.get("used_today", 0),
        number.get("reserved_attempts", 0),
        number.get("capacity", 1),
        intervals,
        number.get("health", {}).get("version", 1),
        bool(number_ready(number)),
        number.get("proposed", False),
        number.get("quote_usd"),
        number.get("version", 1),
        number.get("burst_limit"),
        number.get("burst_seconds", 60),
    )


def run_starts(mask, length):
    """Start bits whose next length bits are all free (logarithmic shifts)."""
    result, covered = mask, 1
    while covered < length:
        shift = min(covered, length - covered)
        result &= result >> shift
        covered += shift
    return result


class Assignment:
    def __init__(
        self, work, slots, now, policy, seconds, rep_limit, owners=None, *, budget=None, work_budget=None
    ):
        self.work, self.slots, self.now, self.policy = work, slots, now, policy
        self.seconds, self.rep_limit = seconds, rep_limit
        self.budget = budget or SearchBudget()
        self.work_budget = work_budget if work_budget is not None else rep_limit * seconds
        self.owners = owners or [frozenset(f"@default:{j}" for j in range(count)) for _, count in slots]
        self.all_mask = (1 << len(slots)) - 1
        self.starts = [at for at, _ in slots]
        self.rep_masks, self.days = defaultdict(int), defaultdict(int)
        for i, (at, _) in enumerate(slots):
            for rep in self.owners[i]:
                self.rep_masks[rep] |= 1 << i
            self.days[at.astimezone(UTC).date().isoformat()] |= 1 << i
        self.allowed, self.lengths, cache = [], [], {}
        shared_estimates = {}
        self.estimates = []
        for item in work:
            if item.outcome_estimates not in shared_estimates:
                shared_estimates[item.outcome_estimates] = dict(item.outcome_estimates)
            self.estimates.append(shared_estimates[item.outcome_estimates])
        self.time_cells = {}
        self.continuing_leads = {item.lead_id for item in work if item.previous_attempt_index is not None}
        for item in work:
            length = max(1, math.ceil(item.predicted_work_seconds / seconds))
            first = bisect_left(self.starts, item.earliest_at)
            last = bisect_right(self.starts, item.deadline - timedelta(seconds=length * seconds))
            key = (item.timezone, item.allowed_local_windows, item.owner_id, length, first, last)
            if key not in cache:
                mask, zone = 0, ZoneInfo(item.timezone)
                start, finish, weekdays, closed = item.allowed_local_windows
                for index in range(first, last):
                    if not self.budget.spend():
                        break
                    at = self.starts[index]
                    if index + length > len(slots) or self.starts[index + length - 1] != at + timedelta(
                        seconds=(length - 1) * seconds
                    ):
                        continue
                    local, end = (
                        at.astimezone(zone),
                        (at + timedelta(seconds=length * seconds, microseconds=-1)).astimezone(zone),
                    )
                    if (
                        local.date() == end.date()
                        and start <= local.hour < finish
                        and start <= end.hour < finish
                        and local.weekday() in weekdays
                        and local.date().isoformat() not in closed
                        and (not item.owner_id or item.owner_id in self.owners[index])
                    ):
                        mask |= 1 << index
                cache[key] = mask
            self.allowed.append(cache[key])
            self.lengths.append(length)

    @property
    def steps(self):
        return self.budget.operations

    @property
    def exhausted(self):
        return self.budget.exhausted

    def objective(self, rows):
        counts = [0, 0, 0, 0, 0]
        value = 0
        for index, *choice in rows:
            item = self.work[index]
            counts[4 if item.previous_attempt_index is not None else item.priority_class] += 1
            predicted = (
                self.value_at(index, choice[0], self.number_regions[choice[1]])
                if choice
                else self.work[index].value
            )
            value += round(predicted * 1000000)
        return (*counts, value)

    def value_at(self, index, start, region):
        at = self.starts[start].astimezone(ZoneInfo(self.work[index].timezone))
        values = self.estimates[index]
        return values.get(
            f"slot:{at.weekday()}:{at.hour}:{region}",
            values.get(f"slot:*:{at.hour}:{region}", values.get("conversion", 0.0)),
        )

    def scored_starts(self, index, free, exact):
        if exact or len(self.estimates[index]) == 1:
            while free:
                first = (free & -free).bit_length() - 1
                yield first
                free &= free - 1
                if not exact:
                    return
            return
        zone = self.work[index].timezone
        if zone not in self.time_cells:
            cells = defaultdict(int)
            for i, at in enumerate(self.starts):
                if not self.budget.spend():
                    break
                local = at.astimezone(ZoneInfo(zone))
                cells[local.weekday(), local.hour] |= 1 << i
            self.time_cells[zone] = tuple(cells.values())
        for mask in self.time_cells[zone]:
            if not self.budget.spend():
                return
            available = free & mask
            if available:
                yield (available & -available).bit_length() - 1

    def solve(self, inventory, *, ignore_bindings=False, alternatives=0, fixed_schedule=()):
        end = self.starts[-1] + timedelta(seconds=self.seconds) if self.starts else self.now
        all_numbers = [
            n if isinstance(n, NumberCapacity) else number_input(n, self.now, end) for n in inventory
        ]
        numbers = {n.number_id: n for n in all_numbers if n.usable}
        self.number_regions = {identity: n.region for identity, n in numbers.items()}
        rep_used, number_used, day_used, endpoints = defaultdict(int), defaultdict(Counter), Counter(), {}
        number_blocked = {}
        account_count, account_blocked = Counter(), 0
        account_limit = self.policy.get(
            "account_channel_limit", max(1, sum(n.concurrent_limit for n in numbers.values()))
        )
        for n in numbers.values():
            counts = Counter()
            for start, finish, count in n.active_leg_intervals:
                for i in range(
                    bisect_left(self.starts, start - timedelta(seconds=self.seconds - 1)),
                    bisect_left(self.starts, finish),
                ):
                    counts[i] += count
                    account_count[i] += count
            number_used[n.number_id] = counts
            number_blocked[n.number_id] = sum(
                1 << i for i, count in counts.items() if count >= n.concurrent_limit
            )
        account_blocked = sum(1 << i for i, count in account_count.items() if count >= account_limit)
        assigned, work_used, start_times = [], 0, defaultdict(list)
        fixed_endpoints = set()
        for row in fixed_schedule:
            start, finish = timestamp(row["at"]), timestamp(row["end_at"])
            if finish <= self.now or start >= end:
                continue
            fixed_endpoints.add(row["endpoint_id"])
            identifier, rep = row["number_id"], row["rep_slot"]
            first = bisect_right(self.starts, start - timedelta(seconds=self.seconds))
            last = bisect_left(self.starts, finish)
            rep_used[rep] |= ((1 << max(0, last - first)) - 1) << first
            day_used[identifier, start.astimezone(UTC).date().isoformat()] += 1
            start_times[identifier].append(start)
            work_used += max(0, (min(end, finish) - max(self.now, start)).total_seconds())
            for i in range(first, last):
                if not self.budget.spend():
                    # Incomplete occupancy cannot authorize another assignment.
                    return [], False
                number_used[identifier][i] += 1
                account_count[i] += 1
        for identifier, n in numbers.items():
            number_blocked[identifier] = sum(
                1 << i for i, count in number_used[identifier].items() if count >= n.concurrent_limit
            )
        account_blocked = sum(1 << i for i, count in account_count.items() if count >= account_limit)
        compatible = []
        for w in self.work:
            if w.bound_number_id and not ignore_bindings and not w.reassignment_approved:
                choices = [numbers[w.bound_number_id]] if w.bound_number_id in numbers else []
            else:
                choices = sorted(
                    (n for n in numbers.values() if n.region in {w.region, "general"}),
                    key=lambda n: (n.region == "general", n.proposed, n.number_id),
                )
            compatible.append(choices)

        def options(index):
            w, length = self.work[index], self.lengths[index]
            if w.endpoint_id in fixed_endpoints:
                return
            previous = endpoints.get(w.endpoint_id)
            if w.previous_attempt_index is not None:
                if not previous or (
                    self.work[previous[-1][0]].lead_id != w.lead_id
                    or self.work[previous[-1][0]].next_attempt_index != w.previous_attempt_index
                ):
                    return
                parent, parent_start, parent_number = previous[-1]
                earliest = self.starts[parent_start] + timedelta(
                    seconds=max(
                        self.lengths[parent] * self.seconds, self.policy.get("min_retry_seconds", 3600)
                    )
                )
            else:
                if previous:
                    return
                earliest, parent_number = w.earliest_at, None
            if not self.allowed[index] or work_used + length * self.seconds > self.work_budget:
                return
            reps = [w.owner_id] if w.owner_id else sorted(self.rep_masks)
            candidates = []
            for n in compatible[index]:
                if parent_number and not ignore_bindings and n.number_id != parent_number:
                    continue
                if not self.budget.spend():
                    return
                available_days = 0
                for day, mask in self.days.items():
                    debit = (
                        n.consumed_attempts + n.pending_reservations
                        if day == self.now.astimezone(UTC).date().isoformat()
                        else 0
                    )
                    if day_used[n.number_id, day] + debit < n.limit(day):
                        available_days |= mask
                if not available_days:
                    continue
                mask = (
                    self.allowed[index]
                    & available_days
                    & run_starts(self.all_mask & ~number_blocked[n.number_id], length)
                )
                mask &= run_starts(self.all_mask & ~account_blocked, length)
                mask &= ~((1 << bisect_left(self.starts, earliest)) - 1)
                if n.available_at:
                    mask &= ~((1 << bisect_left(self.starts, n.available_at)) - 1)
                if n.burst_limit and start_times[n.number_id]:
                    edges = Counter()
                    for at in start_times[n.number_id]:
                        if not self.budget.spend():
                            return
                        edges[bisect_right(self.starts, at - timedelta(seconds=n.burst_seconds))] += 1
                        edges[bisect_left(self.starts, at + timedelta(seconds=n.burst_seconds))] -= 1
                    previous, count = 0, 0
                    for position in sorted(edges):
                        if count >= n.burst_limit:
                            mask &= ~(((1 << position) - 1) ^ ((1 << previous) - 1))
                        count += edges[position]
                        previous = position
                for rep in reps:
                    if not self.budget.spend():
                        return
                    free = mask & run_starts(self.rep_masks.get(rep, 0) & ~rep_used[rep], length)
                    for first in self.scored_starts(index, free, exact_branch):
                        candidates.append((first, n.number_id, rep))
            for candidate in sorted(
                candidates,
                key=lambda c: (
                    c[0] if w.priority_class == 0 else -1,
                    -self.value_at(index, c[0], numbers[c[1]].region),
                    c[0],
                    numbers[c[1]].region == "general",
                    -(
                        numbers[c[1]].limit(self.starts[c[0]].date().isoformat())
                        - day_used[c[1], self.starts[c[0]].date().isoformat()]
                        - (
                            numbers[c[1]].consumed_attempts + numbers[c[1]].pending_reservations
                            if self.starts[c[0]].date() == self.now.date()
                            else 0
                        )
                    )
                    if w.lead_id in self.continuing_leads and w.previous_attempt_index is None
                    else 0,
                    numbers[c[1]].proposed,
                    c[1],
                    c[2],
                ),
            ):
                if not self.budget.spend():
                    return
                yield candidate

        def change(index, option, delta):
            nonlocal work_used, account_blocked
            start, identifier, rep = option
            length = self.lengths[index]
            mask = ((1 << length) - 1) << start
            day = self.starts[start].astimezone(UTC).date().isoformat()
            if delta > 0:
                rep_used[rep] |= mask
                endpoints.setdefault(self.work[index].endpoint_id, []).append((index, start, identifier))
                assigned.append((index, start, identifier, rep))
                start_times[identifier].append(self.starts[start])
            else:
                rep_used[rep] &= ~mask
                chain = endpoints[self.work[index].endpoint_id]
                chain.pop()
                if not chain:
                    del endpoints[self.work[index].endpoint_id]
                assigned.pop()
                start_times[identifier].pop()
            work_used += delta * length * self.seconds
            day_used[identifier, day] += delta
            for i in range(start, start + length):
                number_used[identifier][i] += delta
                account_count[i] += delta
                bit = 1 << i
                if number_used[identifier][i] >= numbers[identifier].concurrent_limit:
                    number_blocked[identifier] |= bit
                else:
                    number_blocked[identifier] &= ~bit
                if account_count[i] >= account_limit:
                    account_blocked |= bit
                else:
                    account_blocked &= ~bit

        exact_branch = (
            len(self.work) <= 8
            and len(self.slots) <= 8
            and len({(n.region, n.concurrent_limit, n.daily_limits) for n in numbers.values()}) <= 4
        )
        if alternatives:
            # One lead's suggested choices use exactly the same availability,
            # resource and window tests as whole-list assignment.
            exact_branch = False
            rows = (
                [(0, *option) for option in itertools.islice(options(0), min(8, alternatives))]
                if self.work
                else []
            )
            return self.schedule_rows(rows, numbers), not self.exhausted
        feasible_cache, individually_feasible = {}, []
        for i, item in enumerate(self.work):
            key = (
                self.allowed[i],
                self.lengths[i],
                item.region,
                item.owner_id,
                item.bound_number_id if not ignore_bindings and not item.reassignment_approved else None,
            )
            if item.previous_attempt_index is not None:
                # Parent choice is not known yet. Treat this as an upper bound,
                # never exclude all future attempts when proving a count ceiling.
                if self.allowed[i] and compatible[i]:
                    individually_feasible.append(i)
                continue
            if key not in feasible_cache:
                feasible_cache[key] = next(options(i), None) is not None
            if feasible_cache[key]:
                individually_feasible.append(i)
        for i in range(len(self.work)):
            if self.exhausted or len(assigned) >= self.rep_limit:
                break
            option = next(options(i), None)
            if option is not None:
                change(i, option, 1)
        best = list(assigned)
        # An attainable upper bound is also a proof; otherwise the large branch is only feasible.
        minimum_duration = min((k * self.seconds for k in self.lengths), default=1)
        time_ceiling = min(
            int(self.work_budget // minimum_duration),
            sum(count for _, count in self.slots) * self.seconds // minimum_duration,
            account_limit * len(self.slots) * self.seconds // minimum_duration,
            self.rep_limit,
        )
        relevant_numbers = {n.number_id for i in individually_feasible for n in compatible[i]}
        inventory_ceiling = sum(
            max(
                0,
                n.limit(day)
                - (n.consumed_attempts + n.pending_reservations if day == self.now.date().isoformat() else 0),
            )
            for n in numbers.values()
            if n.number_id in relevant_numbers
            for day in self.days
        )
        if all(
            not w.bound_number_id
            or ignore_bindings
            or w.reassignment_approved
            or not numbers.get(w.bound_number_id)
            or numbers[w.bound_number_id].region in {w.region, "general"}
            for w in self.work
        ):
            region_work = Counter(self.work[i].region for i in individually_feasible)
            region_capacity = Counter()
            for n in numbers.values():
                region_capacity[n.region] += sum(
                    max(
                        0,
                        n.limit(day)
                        - (
                            n.consumed_attempts + n.pending_reservations
                            if day == self.now.date().isoformat()
                            else 0
                        ),
                    )
                    for day in self.days
                )
            inventory_ceiling = min(
                inventory_ceiling,
                region_capacity["general"]
                + sum(
                    min(count, region_capacity[region])
                    for region, count in region_work.items()
                    if region != "general"
                ),
            )
        bound = min(len(individually_feasible), time_ceiling, inventory_ceiling)
        top = sorted(
            individually_feasible,
            key=lambda i: (
                self.work[i].previous_attempt_index is not None,
                self.work[i].priority_class,
                -self.work[i].value,
            ),
        )[:bound]
        ceiling = self.objective([(i,) for i in top])
        exact = self.objective(best) == ceiling
        if exact_branch and not exact and not self.exhausted:
            while assigned:
                i, start, identifier, rep = assigned[-1]
                change(i, (start, identifier, rep), -1)

            def search(i):
                nonlocal best
                if not self.budget.spend(node=True):
                    return
                if self.objective(assigned) > self.objective(best):
                    best = list(assigned)
                if i == len(self.work) or self.objective(best) == ceiling or len(assigned) >= self.rep_limit:
                    return
                for option in options(i):
                    change(i, option, 1)
                    search(i + 1)
                    change(i, option, -1)
                    if self.exhausted or self.objective(best) == ceiling:
                        return
                search(i + 1)

            search(0)
            exact = not self.exhausted or self.objective(best) == ceiling
        return self.schedule_rows(best, numbers), exact

    def schedule_rows(self, selected, numbers):
        rows = [
            {
                "lead_id": self.work[i].lead_id,
                "endpoint_id": self.work[i].endpoint_id,
                "next_attempt_index": self.work[i].next_attempt_index,
                "region": self.work[i].region,
                "at": self.starts[start].isoformat(),
                "end_at": (
                    self.starts[start] + timedelta(seconds=self.lengths[i] * self.seconds)
                ).isoformat(),
                "number_id": identifier,
                "rep_id": None if rep.startswith("@") else rep,
                "rep_slot": rep,
                "proposed": numbers[identifier].proposed,
                "callback": self.work[i].priority_class == 0,
                "priority_class": self.work[i].priority_class,
                "contingent": self.work[i].previous_attempt_index is not None,
                "predicted_conversions": self.value_at(i, start, numbers[identifier].region),
            }
            for i, start, identifier, rep in selected
        ]
        return sorted(rows, key=lambda r: (r["at"], r["priority_class"], r["lead_id"]))


def schedule_value(rows):
    return (
        *(sum(r["priority_class"] == k and not r.get("contingent") for r in rows) for k in range(4)),
        sum(bool(r.get("contingent")) for r in rows),
        sum(round(r["predicted_conversions"] * 1000000) for r in rows),
    )


def contingent_retries(work, history, now, end, policy, budget):
    cells, pooled, accepted = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0]), 0
    for row in history:
        if (
            not row.get("matured")
            or not timestamp(row.get("available_at"))
            or timestamp(row["available_at"]) > now
        ):
            continue
        if type(row.get("needs_retry")) is bool:
            successes, count = int(row["needs_retry"]), 1
        else:
            successes, count = row.get("needs_retry_count"), row.get("classified_count")
            if (
                type(successes) is not int
                or type(count) is not int
                or not 0 <= successes <= count
                or count <= 0
            ):
                continue
        index, segment = row["attempt_index"], str(row.get("segment") or "unknown")
        for bucket in (cells[segment, index], pooled[index]):
            bucket[0] += successes
            bucket[1] += count
        accepted += count
    totals, by_day, omitted = [0.0, 0.0, 0.0], defaultdict(lambda: [0.0, 0.0, 0.0]), 0
    estimates = {}
    for w in work:
        if budget.exhausted:
            omitted += 1
            continue
        weights, due = [1.0, 1.0, 1.0], w.earliest_at
        for index in range(w.next_attempt_index, policy.get("max_attempts", 4)):
            if not budget.spend():
                omitted += 1
                break
            key = (w.segment, index)
            if key not in estimates:
                s, n = cells[key]
                ps, pn = pooled[index]
                outside = (ps - s + 1) / (pn - n + 2)
                mean = (s + 10 * outside) / (n + 10)
                low, high = wilson(s, n)
                estimates[key] = (min(low, mean), mean, max(high, mean))
            due += timedelta(seconds=max(w.predicted_work_seconds, policy.get("min_retry_seconds", 3600)))
            zone, (start, finish, weekdays, closed) = ZoneInfo(w.timezone), w.allowed_local_windows
            while due < end:
                local = due.astimezone(zone)
                if (
                    start <= local.hour < finish
                    and local.weekday() in weekdays
                    and local.date().isoformat() not in closed
                ):
                    break
                if not budget.spend():
                    break
                due += timedelta(minutes=30)
            if due >= end or budget.exhausted:
                break
            weights = [a * b for a, b in zip(weights, estimates[key], strict=True)]
            bucket = by_day[due.date().isoformat(), w.region]
            for i, value in enumerate(weights):
                totals[i] += value
                bucket[i] += value
    return {
        "low": totals[0],
        "mean": totals[1],
        "high": totals[2],
        "support": accepted,
        "prior": [1, 1],
        "shrinkage": 10,
        "omitted_leads": omitted,
        "by_day_region": [
            {"day": day, "region": region, "low": values[0], "mean": values[1], "high": values[2]}
            for (day, region), values in sorted(by_day.items())
        ],
        "definition": "Conditional later attempts after an observed outcome; never a reservation or permission",
        "estimates": [
            {
                "segment": segment,
                "attempt_index": index,
                "low": values[0],
                "mean": values[1],
                "high": values[2],
            }
            for (segment, index), values in sorted(estimates.items())
        ],
    }


def additional_capacity(
    assignment, base, initial, target, classes, ready_at, daily_capacity_per_number, *, existing_exact=False
):
    """The same bounded count search for known work and contingent scenarios."""
    work, slots, budget = assignment.work, assignment.slots, assignment.budget

    def candidate(kind, count):
        return {
            "lifecycle": "healthy",
            "capacity": 1,
            **kind,
            "id": f"proposed:{kind['id']}:{count}",
            "proposed": True,
            "ready_at": max(ready_at, timestamp(kind.get("ready_at")) or ready_at).isoformat(),
        }

    best, chosen, complete = initial, [], True
    if schedule_value(initial) >= schedule_value(target):
        return best, chosen, True
    exact = len(work) <= 8 and len(slots) <= 8 and len(classes) <= 4
    if exact:
        for count in range(1, len(work) + 1):
            found = []
            for mix in itertools.combinations_with_replacement(range(len(classes)), count):
                if not budget.spend(node=True):
                    return best, chosen, False
                extra = [candidate(classes[k], i) for i, k in enumerate(mix)]
                rows, proof = assignment.solve([*base, *extra])
                complete &= proof
                if schedule_value(rows) > schedule_value(best):
                    best, chosen = rows, extra
                if schedule_value(rows) >= schedule_value(target):
                    found.append((rows, extra))
            if found:
                best, chosen = min(
                    found,
                    key=lambda pair: (
                        -sum(r["predicted_conversions"] for r in pair[0]),
                        sum(c.get("quote_usd") is None for c in pair[1]),
                        sum(c.get("quote_usd") or 0 for c in pair[1]),
                        tuple(c["id"] for c in pair[1]),
                    ),
                )
                return best, chosen, complete
            if budget.exhausted:
                return best, chosen, False
        return best, chosen, False
    while schedule_value(best) < schedule_value(target) and len(chosen) < len(work) and not budget.exhausted:
        alternatives = []
        for kind in classes:
            extra = candidate(kind, len(chosen))
            rows, _ = assignment.solve([*base, *chosen, extra])
            if schedule_value(rows) > schedule_value(best):
                alternatives.append((rows, extra))
        if not alternatives:
            break
        rows, extra = min(
            alternatives,
            key=lambda pair: (
                tuple(-v for v in schedule_value(pair[0])),
                pair[1].get("quote_usd") is None,
                pair[1].get("quote_usd") or 0,
                pair[1]["id"],
            ),
        )
        best, chosen = rows, [*chosen, extra]
    # Remove redundant additions through the same feasibility engine.
    for i in range(len(chosen) - 1, -1, -1):
        if budget.exhausted:
            break
        if existing_exact:
            candidate_max = max(
                (
                    c.get("daily_limit", daily_capacity_per_number) * max(1, len(assignment.days))
                    for c in classes
                ),
                default=1,
            )
            cut = math.ceil(max(0, len(target) - len(initial)) / max(1, candidate_max))
            if len(chosen) - 1 < cut:
                continue  # Proven count cut prunes an impossible removal.
        reduced = chosen[:i] + chosen[i + 1 :]
        rows, _ = assignment.solve([*base, *reduced])
        if schedule_value(rows) >= schedule_value(target):
            best, chosen = rows, reduced
    return best, chosen, False


def retry_capacity(
    work, retries, snapshot, slots, owners, grid, policy, classes, ready_at, daily_capacity, budget
):
    """Deterministic count scenarios; shared ledgers and parent-before-retry rules.

    Counts round expected continuation within each segment/attempt cell. This is
    a planning scenario, not a claim that a particular prospect will retry.
    """
    result = {
        "status": "ready",
        "definition": "Known work plus conditional later attempts; not reservations",
        "rounding": "Nearest whole attempt per segment and generation; stable lead-ID order",
    }
    if not any(item.next_attempt_index < policy.get("max_attempts", 4) for item in work):
        return {**result, "status": "not_needed"}
    probabilities = {(r["segment"], r["attempt_index"]): r for r in retries["estimates"]}
    for scenario in ("low", "mean", "high"):
        if budget.exhausted:
            result["status"] = "limited"
            result[scenario] = {
                "status": "unavailable",
                "reason": "global_planning_budget",
                "purchase_allowed": False,
            }
            continue
        scenario_work, parents = list(work), list(work)
        while parents and not budget.exhausted:
            groups = defaultdict(list)
            for item in parents:
                if not budget.spend():
                    break
                if item.next_attempt_index < policy.get("max_attempts", 4):
                    groups[item.segment, item.next_attempt_index].append(item)
            parents = []
            for key, items in sorted(groups.items()):
                probability = probabilities.get(key, {}).get(scenario, 0)
                count = math.floor(len(items) * probability + 0.5)
                for item in sorted(items, key=lambda w: w.lead_id)[:count]:
                    due = item.earliest_at + timedelta(
                        seconds=max(item.predicted_work_seconds, policy.get("min_retry_seconds", 3600))
                    )
                    if due < snapshot.horizon_end:
                        parents.append(
                            replace(
                                item,
                                next_attempt_index=item.next_attempt_index + 1,
                                previous_attempt_index=item.next_attempt_index,
                                earliest_at=due,
                                owner_id=None,
                                priority_class=3,
                                deadline=snapshot.horizon_end,
                            )
                        )
            scenario_work.extend(parents)
        if budget.exhausted:
            result["status"] = "limited"
            result[scenario] = {
                "status": "unavailable",
                "reason": "global_planning_budget",
                "purchase_allowed": False,
            }
            continue
        assignment = Assignment(
            scenario_work,
            slots,
            snapshot.as_of,
            policy,
            grid,
            len(scenario_work),
            owners,
            budget=budget,
            work_budget=snapshot.rep_seconds,
        )
        existing, existing_exact = assignment.solve(snapshot.inventory)
        unlimited = {
            "id": "forecast-unlimited",
            "region": "general",
            "lifecycle": "healthy",
            "daily_limit": len(scenario_work),
            "capacity": max(1, len(scenario_work)),
        }
        staffed, _ = assignment.solve([unlimited], ignore_bindings=True)
        bound, _ = assignment.solve([*snapshot.inventory, unlimited])
        target, target_exact = assignment.solve(
            [*snapshot.inventory, {**unlimited, "ready_at": ready_at.isoformat()}]
        )
        target = max((existing, target), key=schedule_value)
        best, added, exact = additional_capacity(
            assignment,
            snapshot.inventory,
            existing,
            target,
            classes,
            ready_at,
            daily_capacity,
            existing_exact=existing_exact,
        )
        reached = schedule_value(best) >= schedule_value(target)
        complete = not budget.exhausted
        result[scenario] = {
            "status": "ready" if complete else "limited",
            "planned_attempts": len(scenario_work),
            "retry_attempts": len(scenario_work) - len(work),
            "existing_capacity": len(existing),
            "expanded_capacity": len(best),
            "additional_numbers": len(added) if reached and complete else None,
            "minimum_proven_on_grid": bool(exact and target_exact and reached and complete),
            "binding_shortfall": max(0, len(staffed) - len(bound)) if complete else None,
            "staffing_shortfall": max(0, len(scenario_work) - len(staffed)) if complete else None,
            "unserved_attempts": len(scenario_work) - len(best),
            "schedule": best[:1000],
            "schedule_rows_omitted": max(0, len(best) - 1000),
            "purchase_allowed": False,
        }
        if not complete:
            result["status"] = "limited"
    return result


def plan_changed(
    leads,
    inventory,
    *,
    now,
    horizon_hours,
    rep_minutes,
    handling_seconds,
    policy,
    staffed_windows,
    fixed_schedule=(),
    rep_busy_until=None,
):
    """Assign one changed page around saved work using the same feasibility engine.

    This preserves previous feasible allocations; a separately requested full
    forecast can rebalance the entire list and compute hypothetical additions.
    """
    if len(leads) > 100:
        raise ValueError("At most100 changed leads per planning page")
    if len(fixed_schedule) > 10000:
        raise ValueError("At most10000 saved allocations per horizon")
    end = now + timedelta(hours=horizon_hours)
    work, _ = work_items(leads, now, end, policy, handling_seconds)
    windows = normalize_availability(staffed_windows, now, end, rep_minutes, policy)
    grid = max(30, math.ceil((end - now).total_seconds() / (MAX_SLOTS * 30)) * 30)
    slots, owners, limited = staffed_slots(windows, now, end, grid, rep_busy_until)
    assignment = Assignment(work, slots, now, policy, grid, len(work), owners, work_budget=rep_minutes * 60)
    chosen, exact = assignment.solve(inventory, fixed_schedule=fixed_schedule)
    assigned = {row["lead_id"] for row in chosen}
    lowest_fixed_priority = max((row.get("priority_class", 3) for row in fixed_schedule), default=-1)
    rebalance = any(w.lead_id not in assigned and w.priority_class < lowest_fixed_priority for w in work)
    fingerprint = hashlib.sha256(
        json.dumps(
            [leads, inventory, now, policy, windows, fixed_schedule, rep_busy_until],
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    return {
        "existing_schedule": chosen,
        "planning_limited": limited or assignment.exhausted,
        "input_fingerprint": fingerprint,
        "schedule_rows_omitted": 0,
        "search_operations": assignment.steps,
        "proven_on_declared_grid": exact and not limited,
        "constraint_scope": "saved_allocations_and_changed_page",
        "rebalance_required": rebalance,
    }


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
    rep_busy_until=None,
    retry_observations=(),
    schedule_row_limit=1000,
):
    end = now + timedelta(hours=horizon_hours)
    work, unknown = work_items(leads, now, end, policy, handling_seconds)
    windows = normalize_availability(staffed_windows, now, end, rep_minutes, policy)
    snapshot = PlanningInput(
        1,
        now,
        end,
        str(policy.get("version", 1)),
        str(policy.get("goal_version", 1)),
        windows,
        tuple(work),
        tuple(number_input(n, now, end) for n in sorted(inventory, key=lambda n: n["id"])),
        tuple(sorted((rep_busy_until or {}).items())),
        str(policy.get("estimate_version", "pooled-v1")),
        json.dumps(policy, sort_keys=True),
        rep_minutes * 60,
        (("added_daily_capacity", daily_capacity_per_number), ("setup_hours", additional_ready_after_hours)),
        hashlib.sha256(
            json.dumps(
                sorted(retry_observations, key=lambda v: json.dumps(v, sort_keys=True)), sort_keys=True
            ).encode()
        ).hexdigest(),
    )
    fingerprint = hashlib.sha256(
        json.dumps(asdict(snapshot), sort_keys=True, default=str).encode()
    ).hexdigest()
    grid = max(30, math.ceil((end - now).total_seconds() / (MAX_SLOTS * 30)) * 30)
    slots, owners, limited = staffed_slots(windows, now, end, grid, rep_busy_until)
    budget = SearchBudget()
    assignment = Assignment(
        work, slots, now, policy, grid, len(work), owners, budget=budget, work_budget=snapshot.rep_seconds
    )
    existing, existing_exact = assignment.solve(snapshot.inventory)
    relaxed = {
        "id": "forecast-unlimited",
        "region": "general",
        "lifecycle": "healthy",
        "daily_limit": len(work),
        "capacity": max(1, len(work)),
    }
    staffed, staffed_exact = assignment.solve([relaxed], ignore_bindings=True)
    bound, bound_exact = assignment.solve([*snapshot.inventory, relaxed])
    ready_at = now + timedelta(hours=additional_ready_after_hours)
    delayed = {**relaxed, "ready_at": ready_at.isoformat()}
    target, target_exact = assignment.solve([*snapshot.inventory, delayed])
    target = max((existing, target), key=schedule_value)
    classes = policy.get("proposed_number_classes") or [
        {"id": r, "region": r, "daily_limit": daily_capacity_per_number, "capacity": 1}
        for r in sorted({"general", *(w.region for w in work)})
    ]
    classes = sorted(classes, key=lambda c: c["id"])

    best, added, exact_added = additional_capacity(
        assignment,
        snapshot.inventory,
        existing,
        target,
        classes,
        ready_at,
        daily_capacity_per_number,
        existing_exact=existing_exact,
    )
    days = max(1, len(assignment.days))
    max_added = max((c.get("daily_limit", daily_capacity_per_number) * days for c in classes), default=1)
    lower = math.ceil(max(0, len(target) - len(existing)) / max(1, max_added)) if existing_exact else 0
    reached = schedule_value(best) >= schedule_value(target)
    proven = bool(reached and target_exact and (exact_added or existing_exact and len(added) == lower))
    known_limited = limited or budget.exhausted or not reached
    pending = []
    for n in inventory:
        if (
            not number_ready(n)
            and n.get("lifecycle")
            in {"purchased", "identity_configured", "callback_tested", "investigating", "paused_for_outbound"}
            and n.get("provider_present") is not False
            and not n.get("provider_recording_enabled")
        ):
            pending.append(
                {
                    **n,
                    "lifecycle": "healthy",
                    "identity_configured": True,
                    "callback_tested": True,
                    "daily_limit": n.get("daily_limit") or daily_capacity_per_number,
                    "ready_at": n.get("available_at") or n.get("ready_at") or ready_at.isoformat(),
                }
            )
    pending_schedule, pending_add = [], added
    pending_complete = not pending
    if pending and not budget.exhausted:
        conditional = [n for n in snapshot.inventory if n.usable] + pending
        pending_schedule, _ = assignment.solve(conditional)
        pending_best, pending_add, _ = additional_capacity(
            assignment, conditional, pending_schedule, target, classes, ready_at, daily_capacity_per_number
        )
        pending_complete = not budget.exhausted and schedule_value(pending_best) >= schedule_value(target)
    retries = contingent_retries(work, retry_observations, now, end, policy, budget)
    later = retry_capacity(
        work,
        retries,
        snapshot,
        slots,
        owners,
        grid,
        policy,
        classes,
        ready_at,
        daily_capacity_per_number,
        budget,
    )
    limited = known_limited
    ready = {n.number_id: n for n in snapshot.inventory if n.usable}
    waiting_ids = {n["id"] for n in pending}
    regions = []
    for region in sorted({w.region for w in work}):
        old, new = [r for r in existing if r["region"] == region], [r for r in best if r["region"] == region]
        regional = sum(ready[r["number_id"]].region != "general" for r in old)
        regions.append(
            {
                "region": region,
                "forecast_attempts": sum(w.region == region for w in work),
                "eligible_leads": sum(w.region == region for w in work),
                "planned_attempts": len(new),
                "healthy_existing": sum(n.region == region for n in ready.values()),
                "usable_existing_capacity": regional,
                "general_fallback_capacity": len(old) - regional,
                "capacity_waiting_for_review": sum(
                    r["region"] == region and r["number_id"] in waiting_ids for r in pending_schedule
                ),
                "outbound_capacity_shortfall": max(0, len(new) - len(old)),
                "callback_demand": sum(r["callback"] for r in new),
                "required": 0,
                "proposed_additional": None
                if limited or not pending_complete
                else len(pending_add)
                if not regions
                else 0,
                "proposed_pool": "shared proposal",
                "incremental_capacity_used": len(new) - len(old),
                "reason": "Known next attempts on shared staffed, identity and workload constraints",
            }
        )
    assumptions = {
        "daily_slots_per_added_number": daily_capacity_per_number,
        "handling_seconds": handling_seconds,
        "rep_minutes": rep_minutes,
        "additional_ready_after_hours": additional_ready_after_hours,
        "staffing_grid_seconds": grid,
        "staffing_grid": "Consecutive occupied grid buckets; rounded up work duration",
        "retry_demand": "Contingent retries are separate; actual outcomes create the next eligible attempt",
        "staffing_timezone": policy.get("staffing_timezone", "UTC"),
        "staffing_start_hour": policy.get("staffing_start_hour", policy.get("local_start_hour", 9)),
        "staffing_end_hour": policy.get("staffing_end_hour", policy.get("local_end_hour", 20)),
        "future_readiness": "Conditional pending schedule requires the declared setup to succeed; undated inventory uses the explicitly shown setup-delay scenario",
        "rep_ownership": "Named callback owner required; pooled headcount cannot establish ownership",
        "account_channel_limit": policy.get("account_channel_limit"),
        "account_limits_verified": policy.get("account_channel_limit") is not None,
    }
    shortfalls = {
        "staffing": None if limited else max(0, len(work) - len(staffed)),
        "binding": None if limited else max(0, len(staffed) - len(bound)),
        "setup": None if limited else max(0, len(bound) - len(target)),
        "number": None if limited else max(0, len(target) - len(existing)),
    }
    result = PlanningResult(
        now.isoformat(),
        fingerprint,
        "known_next_attempts",
        len(work),
        tuple(retries.items()),
        tuple(existing[:schedule_row_limit]),
        tuple(best[:schedule_row_limit]),
        len(added) if proven and not limited else None,
        len(added),
        len(added) if proven and not limited else lower,
        proven and not limited,
        proven and not limited,
        tuple(shortfalls.items()),
        (
            ("work", 0),
            ("schedule", max(0, len(best) - (schedule_row_limit or len(best)))),
            ("retry", retries["omitted_leads"]),
        ),
        tuple(assumptions.items()),
        (
            ("policy", snapshot.policy_version),
            ("goal", snapshot.goal_version),
            ("estimates", snapshot.estimate_version),
        ),
    )
    return {
        **asdict(result),
        "contingent_retry_low_mean_high": retries,
        "contingent_capacity": later,
        "omitted_rows": dict(result.omitted_rows),
        "assumptions": assumptions,
        "source_versions": dict(result.source_versions),
        "shortfall_by_reason": shortfalls,
        "regions": regions,
        "horizon_hours": horizon_hours,
        "imported_leads": len(leads),
        "rep_capacity_attempts": len(staffed),
        "forecast_attempts": len(work),
        "existing_only_capacity": len(existing),
        "existing_plus_addition_capacity": len(best),
        **{k + "_shortfall": v for k, v in shortfalls.items()},
        "capacity_shortfall": len(work) - len(existing) + unknown,
        "window_unknown_leads": unknown,
        "owner_unavailable_attempts": None
        if limited
        else sum(bool(w.owner_id) and not mask for w, mask in zip(work, assignment.allowed, strict=True)),
        "minimum_additional_numbers": result.minimum_additions,
        "proposed_additional_numbers": None if limited or not pending_complete else len(pending_add),
        "pending_planning_complete": pending_complete,
        "best_found_additional_numbers": len(added),
        "optimality_gap": len(added) - result.lower_bound,
        "optimality": "proven_small_case" if proven and not limited else "best_feasible_found",
        "planning_limited": limited,
        "planning_steps": budget.operations,
        "search_operations": budget.operations,
        "search_nodes": budget.nodes,
        "optimality_scope": "Minimum only when proved on declared grid, known attempts, staffing and reviewed capacity; no conversion-lift claim",
        "schedule_scope": "declared_staffed_windows" if staffed_windows else "normalized_simple_windows",
        "existing_schedule": existing[:schedule_row_limit],
        "expanded_schedule": best[:schedule_row_limit],
        "conditional_pending_schedule": pending_schedule[:schedule_row_limit],
        "schedule_rows_omitted": max(0, len(best) - (schedule_row_limit or len(best))),
        "purchased": False,
        "approval_required": True,
        "purchase_allowed": False,
        "purchase_blocker": "New rentals are recurring commitments",
        "estimate_requires_provider_quote": True,
        "monthly_rental_estimate_usd": sum(c["quote_usd"] for c in pending_add)
        if not limited and pending_complete and all(c.get("quote_usd") is not None for c in pending_add)
        else None,
        "estimate_note": "Account quote required; unknown price is not zero. New recurring commitments remain blocked.",
        "cold_start": not any(n.get("health", {}).get("samples", 0) for n in inventory),
        "expected_conversion_lift": None,
    }
