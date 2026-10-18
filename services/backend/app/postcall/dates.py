"""Check extracted callback times without inventing appointments.

The interpreter supplies the commitment and source phrase. This deliberately small
calendar grammar verifies common exact dates; unsupported expressions stay drafts.
It does not decide whether somebody agreed to a meeting or book one.
"""

import calendar
import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ZONES = {
    "pacific": "America/Los_Angeles",
    "mountain": "America/Denver",
    "central": "America/Chicago",
    "eastern": "America/New_York",
    "hawaii": "Pacific/Honolulu",
    "alaska": "America/Anchorage",
}
UNCERTAIN = re.compile(
    r"\b(maybe|perhaps|possibly|around|about|approximately|before|after|between|or|ish)\b", re.I
)
MONTHS = {name.casefold(): index for index, name in enumerate(calendar.month_name) if name}
WEEKDAYS = {name.casefold(): index for index, name in enumerate(calendar.day_name)}


def _zone(expression, fallback):
    named = re.findall(r"\b([A-Za-z]+)\s+(?:standard\s+|daylight\s+)?time\b", expression, re.I)
    if any(name.casefold() not in ZONES for name in named):
        return None
    candidates = [value for name, value in ZONES.items() if re.search(rf"\b{name}\b", expression, re.I)]
    candidates += re.findall(r"\b[A-Z][A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?\b", expression)
    if re.search(r"\b(?:UTC|GMT)\b", expression):
        candidates.append("UTC")
    # Abbreviations such as CST are ambiguous internationally; don't guess from area code.
    if re.search(r"\b[PMCE][SD]T\b", expression):
        return None
    names = set(candidates)
    if len(names) > 1:
        return None
    name = next(iter(names), fallback)
    try:
        return ZoneInfo(name) if name else None
    except (ValueError, ZoneInfoNotFoundError):
        return None


def _date(expression, local_now):
    dates = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", expression)
    if dates:
        return date.fromisoformat(dates[0]) if len(dates) == 1 else None
    matches = list(
        re.finditer(
            r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
            expression,
            re.I,
        )
    )
    if len(matches) > 1:
        return None
    match = matches[0] if matches else None
    if match:
        year = int(match[3]) if match[3] else local_now.year if local_now else None
        if not year:
            return None
        result = date(year, MONTHS[match[1].casefold()], int(match[2]))
        # A missing year close to New Year needs confirmation, not an invented year rollover.
        return result if match[3] or result >= local_now.date() else None
    if not local_now:
        return None
    for label, offset in (("today", 0), ("tomorrow", 1)):
        if re.search(rf"\b{label}\b", expression, re.I):
            return local_now.date() + timedelta(days=offset)
    matches = [day for name, day in WEEKDAYS.items() if re.search(rf"\b{name}\b", expression, re.I)]
    if len(matches) == 1:
        distance = (matches[0] - local_now.weekday()) % 7
        # Same-weekday requests need an explicit date/today; "next" clearly means a week later.
        if distance == 0:
            if not re.search(r"\bnext\b", expression, re.I):
                return None
            distance = 7
        return local_now.date() + timedelta(days=distance)
    return None


def _time(expression):
    matches = list(re.finditer(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", expression, re.I))
    if matches:
        if len(matches) != 1:
            return None
        match = matches[0]
        hour, minute = int(match[1]), int(match[2] or 0)
        if not 1 <= hour <= 12:
            return None
        return time(hour % 12 + (12 if match[3].casefold() == "p" else 0), minute)
    matches = re.findall(r"\b(\d{1,2}):(\d{2})\b", expression)
    return time(*map(int, matches[0])) if len(matches) == 1 else None


def resolve_time(item, *, call_at=None, timezone=None):
    expression = item.get("verbatim_time_expression", "")
    zone = _zone(expression, timezone)
    local_now = call_at.astimezone(zone) if call_at and call_at.tzinfo and zone else None
    result = {
        **item,
        "due_at": None,
        "needs_time_confirmation": True,
        "timezone": zone.key if zone else None,
        "time_window": None,
        "time_resolution": "needs_confirmation",
    }
    try:
        day, clock_time = _date(expression, local_now), _time(expression)
    except ValueError:
        return result
    if day:
        periods = re.findall(r"\b(morning|afternoon|evening)\b", expression, re.I)
        result["time_window"] = {
            "date": day.isoformat(),
            "period": periods[0].casefold() if len(periods) == 1 else None,
            "timezone": result["timezone"],
        }
        if clock_time:
            result["time_window"]["local_time"] = clock_time.isoformat(timespec="minutes")
    if (
        not day
        or not clock_time
        or not zone
        or item.get("needs_time_confirmation")
        or UNCERTAIN.search(expression)
    ):
        return result
    naive = datetime.combine(day, clock_time)
    # Round-trips exclude DST gaps; two distinct instants expose an ambiguous fold.
    instants = {
        naive.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        for fold in (0, 1)
        if naive.replace(tzinfo=zone, fold=fold).astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        == naive
    }
    if len(instants) != 1:
        return result
    exact = instants.pop()
    if call_at and exact < call_at:
        return result
    proposed = item.get("due_at")
    if proposed:
        proposed = datetime.fromisoformat(proposed.replace("Z", "+00:00"))
        if not proposed.tzinfo or proposed.astimezone(UTC) != exact:
            raise ValueError("Proposed timestamp does not match the evidenced date and timezone")
    result.update(due_at=exact.isoformat(), needs_time_confirmation=False, time_resolution="exact")
    return result
