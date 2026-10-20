"""Check extracted callback times without inventing appointments.

The interpreter supplies the commitment and source phrase. This deliberately small
calendar grammar verifies common exact dates; unsupported expressions stay drafts.
It does not decide whether somebody agreed to a meeting or book one. Every reading
that admits two instants is a suggestion for the rep to confirm, never a guess:

- a bare H:MM needs am/pm or an explicit 24-hour form (13:00-23:59, or 0H:MM);
- a zone abbreviation overrides the lead's zone; an unknown one, or a standard/daylight
  abbreviation that disagrees with the time then in effect (PST in July), is ambiguous;
- a stated weekday must agree with a stated date;
- "next <weekday>" is that weekday when the coming one falls in the next Monday-start
  calendar week (or is today's weekday, a week later); within the current week it can
  mean either occurrence, so it needs confirmation.
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
# Common North American and European abbreviations: zone and whether the abbreviation
# names daylight time (True), standard time (False) or either (None).
ABBREVIATIONS = {
    "pt": ("America/Los_Angeles", None),
    "pst": ("America/Los_Angeles", False),
    "pdt": ("America/Los_Angeles", True),
    "mt": ("America/Denver", None),
    "mst": ("America/Denver", False),
    "mdt": ("America/Denver", True),
    "ct": ("America/Chicago", None),
    "cst": ("America/Chicago", False),
    "cdt": ("America/Chicago", True),
    "et": ("America/New_York", None),
    "est": ("America/New_York", False),
    "edt": ("America/New_York", True),
    "akst": ("America/Anchorage", False),
    "akdt": ("America/Anchorage", True),
    "hst": ("Pacific/Honolulu", False),
    "nst": ("America/St_Johns", False),
    "ndt": ("America/St_Johns", True),
    "utc": ("UTC", None),
    "gmt": ("UTC", None),
    "bst": ("Europe/London", True),
    "cet": ("Europe/Paris", False),
    "cest": ("Europe/Paris", True),
    "eet": ("Europe/Athens", False),
    "eest": ("Europe/Athens", True),
}
# Zone-like abbreviations (IST, JST, AEST) in capitals anywhere, or in any case right
# after a clock time. Unrecognized ones make the zone ambiguous instead of being ignored.
ABBREVIATION_LIKE = re.compile(
    r"\b([A-Z]{1,3}[SD]T)\b|(?i:\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?|\d{1,2}:\d{2})\s*\(?([A-Za-z]{1,3}[SDsd][Tt])\b"
)
UNCERTAIN = re.compile(
    r"\b(maybe|perhaps|possibly|around|about|approximately|before|after|between|or|ish)\b", re.I
)
MONTHS = {name.casefold(): index for index, name in enumerate(calendar.month_name) if name}
WEEKDAYS = {name.casefold(): index for index, name in enumerate(calendar.day_name)}


def _zone(expression, fallback):
    """Returns (zone, daylight flag) or (None, None) when the zone is unknown or ambiguous."""
    named = re.findall(r"\b([A-Za-z]+)\s+(?:standard\s+|daylight\s+)?time\b", expression, re.I)
    if any(name.casefold() not in ZONES for name in named):
        return None, None
    candidates = [value for name, value in ZONES.items() if re.search(rf"\b{name}\b", expression, re.I)]
    candidates += re.findall(r"\b[A-Z][A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?\b", expression)
    if any(
        (match[1] or match[2]).casefold() not in ABBREVIATIONS
        for match in ABBREVIATION_LIKE.finditer(expression)
    ):
        return None, None
    abbreviations = [
        ABBREVIATIONS[token.casefold()]
        for token in re.findall(r"\b[A-Za-z]{2,4}\b", expression)
        if token.casefold() in ABBREVIATIONS
    ]
    candidates += [zone for zone, _ in abbreviations]
    flags = {flag for _, flag in abbreviations if flag is not None}
    names = set(candidates)
    if len(names) > 1 or len(flags) > 1:
        return None, None
    name = next(iter(names), fallback)
    try:
        return (ZoneInfo(name) if name else None), next(iter(flags), None)
    except (ValueError, ZoneInfoNotFoundError):
        return None, None


def _weekdays(expression):
    return {day for name, day in WEEKDAYS.items() if re.search(rf"\b{name}\b", expression, re.I)}


def _date(expression, local_now):
    named = _weekdays(expression)
    dates = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", expression)
    matches = list(
        re.finditer(
            r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
            expression,
            re.I,
        )
    )
    result = None
    if dates:
        if len(dates) != 1:
            return None
        result = date.fromisoformat(dates[0])
    elif len(matches) > 1:
        return None
    elif matches:
        match = matches[0]
        year = int(match[3]) if match[3] else local_now.year if local_now else None
        if not year:
            return None
        result = date(year, MONTHS[match[1].casefold()], int(match[2]))
        # A missing year close to New Year needs confirmation, not an invented year rollover.
        if not match[3] and result < local_now.date():
            return None
    elif local_now:
        for label, offset in (("today", 0), ("tomorrow", 1)):
            if re.search(rf"\b{label}\b", expression, re.I):
                result = local_now.date() + timedelta(days=offset)
    if result is not None:
        # A stated weekday must name the same day as the stated date.
        return result if not named or named == {result.weekday()} else None
    if not local_now or len(named) != 1:
        return None
    weekday = next(iter(named))
    distance = (weekday - local_now.weekday()) % 7
    if re.search(r"\bnext\b", expression, re.I):
        if distance == 0:
            return local_now.date() + timedelta(days=7)
        # Within this Monday-start week, "next" can mean either occurrence.
        if local_now.weekday() + distance < 7:
            return None
    elif distance == 0:
        # Same-weekday requests need an explicit date/today.
        return None
    return local_now.date() + timedelta(days=distance)


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
    if len(matches) != 1:
        return None
    hour, minute = matches[0]
    # Only an explicit 24-hour form is a clock time; "2:00" may be morning or afternoon.
    if not (13 <= int(hour) <= 23 or len(hour) == 2 and hour.startswith("0")):
        return None
    return time(int(hour), int(minute))


def resolve_time(item, *, call_at=None, timezone=None):
    expression = item.get("verbatim_time_expression", "")
    zone, daylight = _zone(expression, timezone)
    result = {
        **item,
        "due_at": None,
        "needs_time_confirmation": True,
        "timezone": zone.key if zone else None,
        "time_window": None,
        "time_resolution": "needs_confirmation",
    }
    if call_at is not None and (call_at.tzinfo is None or call_at.utcoffset() is None):
        return result  # An unzoned call time cannot anchor or bound a callback.
    local_now = call_at.astimezone(zone) if call_at and zone else None
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
    if daylight is not None and bool(exact.astimezone(zone).dst()) != daylight:
        return result
    proposed = item.get("due_at")
    if proposed:
        try:
            parsed = datetime.fromisoformat(proposed.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            parsed = None
        if not parsed or not parsed.tzinfo or parsed.astimezone(UTC) != exact:
            # Keep the model's value visible for the rep; never pick one silently.
            return {**result, "proposed_due_at": proposed}
    result.update(due_at=exact.isoformat(), needs_time_confirmation=False, time_resolution="exact")
    return result
