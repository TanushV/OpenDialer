"""Prefer prime time (call insights plan P5): call whoever is in the best local time band right now.

One curve in the lead's own local time, the same for every prospect and every rep, in half-hour bands of the
weekday calling window (9:00 to 17:00):

- Prime: 9:30 to 11:30 and 16:00 to 17:00.
- Good: 9:00 to 9:30, 11:30 to 12:00 and 13:30 to 16:00.
- Low: 12:00 to 13:30 (lunch).

It only ranks: the calling window, attempts, opt-outs and due callbacks stay hard rules ahead of it, and a lead is
never held back for prime time. After about 1,000 dials each band blends in this workspace's own answer rate. A lead
whose zone is unsure is scored by its worst candidate zone. Retries are moved into the lead's next prime window,
alternating the morning and afternoon blocks.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.compliance.policy import ALL_US_ZONES, outreach_open
from app.optimization.geography import planning_zones

PRIME, GOOD, LOW = 2.0, 1.0, 0.0
LABELS = {PRIME: "prime", GOOD: "good", LOW: "low"}
# (start, end) in minutes after midnight, local time.
BANDS = (
    (9 * 60, 9 * 60 + 30, GOOD),
    (9 * 60 + 30, 11 * 60 + 30, PRIME),
    (11 * 60 + 30, 12 * 60, GOOD),
    (12 * 60, 13 * 60 + 30, LOW),
    (13 * 60 + 30, 16 * 60, GOOD),
    (16 * 60, 17 * 60, PRIME),
)
MORNING, AFTERNOON = "morning", "afternoon"
LEARN_AFTER = 1000
LEARN_WEIGHT = 200
UNSURE = "*"
ZONE_NAMES = {
    "America/New_York": "Eastern",
    "America/Detroit": "Eastern",
    "America/Indiana/Indianapolis": "Eastern",
    "America/Kentucky/Louisville": "Eastern",
    "America/Chicago": "Central",
    "America/Indiana/Knox": "Central",
    "America/Menominee": "Central",
    "America/North_Dakota/Center": "Central",
    "America/Denver": "Mountain",
    "America/Boise": "Mountain",
    "America/Phoenix": "Arizona",
    "America/Los_Angeles": "Pacific",
    "America/Anchorage": "Alaska",
    "Pacific/Honolulu": "Hawaii",
}


def zone_key(lead):
    """What the queue stores for a lead's zone: its candidate zones, or `*` when unsure."""
    zones = planning_zones(lead)
    return "|".join(sorted(zones)) if zones else UNSURE


def zones_of(key):
    return ALL_US_ZONES if not key or key == UNSURE else tuple(key.split("|"))


def slot(local):
    """The half-hour slot index of a local time (0 = 9:00), or None outside the window."""
    minutes = local.hour * 60 + local.minute
    return (minutes - 9 * 60) // 30 if 9 * 60 <= minutes < 17 * 60 else None


def default_band(local):
    minutes = local.hour * 60 + local.minute
    return next((band for start, end, band in BANDS if start <= minutes < end), LOW)


def score(key, now, learned=None):
    """The worst band across the lead's candidate zones right now (higher is better)."""
    values = []
    for name in zones_of(key):
        try:
            local = now.astimezone(ZoneInfo(name))
        except (ZoneInfoNotFoundError, ValueError):
            continue
        index = slot(local)
        base = default_band(local)
        values.append(learned.get(index, base) if learned and index is not None else base)
    return min(values) if values else LOW


def label(value):
    return "prime" if value >= 1.5 else "good" if value >= 0.5 else "low"


def describe(key, now):
    """The Dialer's line for a lead's local time: ("Central", "10:14", "prime")."""
    zones = zones_of(key)
    name = zones[0] if len(zones) == 1 else None
    if name is None:
        return None
    local = now.astimezone(ZoneInfo(name))
    return {
        "zone": ZONE_NAMES.get(name, name.split("/")[-1].replace("_", " ")),
        "local_time": f"{local.hour % 12 or 12}:{local.minute:02d}",
        "band": label(score(key, now)),
    }


def learned_scores(samples):
    """Blend each half-hour slot's own answer rate into the default once there are enough dials.

    `samples` is {slot: (dials, answers)}. Returns {slot: score} or {} below `LEARN_AFTER` dials.
    """
    total = sum(dials for dials, _ in samples.values())
    if total < LEARN_AFTER:
        return {}
    answered = sum(answers for _, answers in samples.values())
    overall = answered / total if total else 0
    if not overall:
        return {}
    result = {}
    for index in range(16):
        minutes = 9 * 60 + 30 * index
        base = default_band(datetime(2026, 1, 5, minutes // 60, minutes % 60))
        dials, answers = samples.get(index, (0, 0))
        weight = dials / (dials + LEARN_WEIGHT)
        observed = max(LOW, min(PRIME, PRIME * (answers / dials) / (2 * overall))) if dials else base
        result[index] = round((1 - weight) * base + weight * observed, 3)
    return result


def block(local):
    return MORNING if local.hour < 12 else AFTERNOON


def next_prime(not_before, key, *, after_block=None, start_hour=9, end_hour=17):
    """The first moment at or after `not_before` when the lead is in a prime band in every candidate zone and inside
    the calling window, preferring the other block than `after_block`. Searches two weeks, else `not_before`."""
    zones = [ZoneInfo(name) for name in zones_of(key)]
    if not zones:
        return not_before
    start = not_before.replace(second=0, microsecond=0)
    if start < not_before:
        start += timedelta(minutes=1)
    # Step to the next half hour so moments line up with the bands.
    start += timedelta(minutes=(30 - start.minute % 30) % 30)
    first_day = not_before.astimezone(zones[0]).date()
    fallback = None
    moment = start
    for _ in range(14 * 48):
        locals_ = [moment.astimezone(zone) for zone in zones]
        if all(
            outreach_open(local, start_hour, end_hour) and default_band(local) == PRIME for local in locals_
        ):
            # The other block of the day than the last attempt if that day still has it, else the first prime time.
            if after_block is None or block(locals_[0]) != after_block:
                return moment
            fallback = fallback or moment
        if fallback and locals_[0].date() > first_day:
            return fallback
        moment += timedelta(minutes=30)
    return fallback or not_before


def backfill_zones(db):
    """Leads saved before the zone column get it once at startup, so the queue can order them by prime time."""
    import logging

    from sqlalchemy import select

    from app.platform.db import MODELS

    model = MODELS["leads"]
    changed = 0
    with db.transaction() as s:
        for lead in s.scalars(select(model).where(model.op_zone.is_(None))):
            lead.op_zone = zone_key(lead.data)
            changed += 1
    if changed:
        logging.getLogger(__name__).info("Filled the time zone column on %d earlier leads", changed)
    return changed


def prime_retry_at(lead, not_before, last_at=None):
    """When a retry is due: the lead's next prime window at or after the minimum spacing, in the other block of the
    day than the last attempt (plan P5)."""
    key = zone_key(lead)
    zones = zones_of(key)
    after = block(last_at.astimezone(ZoneInfo(zones[0]))) if last_at and len(zones) == 1 else None
    return next_prime(not_before, key, after_block=after)
