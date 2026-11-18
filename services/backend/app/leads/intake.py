"""One intake path (section 9, D16): a CSV row or a HubSpot record becomes a row, and one column map reads it.

The map names which column plays each role and how its values split. CSV intake also keeps unmapped columns for
research. Mapping happens before the size limit, so a wide record cannot push a signal column out.
"""

import json
import re

# Role -> (header names it is recognised by, default delimiter for multi-value columns).
ROLES = {
    "title": (("title", "job title", "jobtitle"), None),
    "headline": (("contact headline", "headline", "linkedin headline"), None),
    "company_description": (("company description", "company about", "about", "description"), None),
    "industry": (("industry", "company industry", "account industry"), None),
    "size": (
        (
            "employees (estimate)",
            "employees",
            "company size",
            "employee count",
            "company headcount",
            "headcount",
            "numberofemployees",
        ),
        None,
    ),
    "number_source": (("finder", "number source", "phone source", "phone number source"), None),
    "live_status": (("network status", "live status", "line status", "phone status"), None),
    "signal_summaries": (("signal summaries", "account signals", "company signals", "signals"), "|"),
    "signal_dates": (("signal dates", "account signal dates"), ";"),
    "signal_urls": (("signal urls", "signal links", "account signal urls", "account signal links"), ";"),
    "linkedin_activity": (
        ("linkedin activity status", "linkedin activity", "recent linkedin activity"),
        None,
    ),
}
CSV_IDENTITY = {
    "phone": (("phone", "phone number", "mobile phone", "mobile", "mobile number", "direct phone"), None),
    "name": (("name", "full name", "contact name", "prospect name"), None),
    "first_name": (("first name", "given name"), None),
    "last_name": (("last name", "surname", "family name"), None),
    "company": (("company", "company name", "organization name", "account name", "organization"), None),
    "domain": (("domain", "company domain", "company website", "website", "account website"), None),
    "state": (("state", "contact state", "prospect state"), None),
    "timezone": (("timezone", "contact timezone", "prospect timezone"), None),
}
SIGNALS = ("signal_summaries", "signal_dates", "signal_urls")
# Read by import itself (who, where, which number), so they always reach the row.
IDENTITY = (
    "name",
    "first name",
    "last name",
    "firstname",
    "lastname",
    "company",
    "domain",
    "company website",
    "phone",
    "timezone",
    "state",
    "region",
    "city",
    "country",
    "jurisdiction",
    "segment",
)
VALUE_LIMIT = 2000
ROW_LIMIT = 24000
DEAD = {"dead", "disconnected", "not_in_service"}
SCRIPT_COLUMNS = {"script", "callscript", "finishedscript", "preparedscript", "scriptmarkdown", "scripttext"}


def _key(header):
    return re.sub(r"[\W_]+", "", str(header).casefold())


def is_script_column(header):
    return _key(header) in SCRIPT_COLUMNS


def guess_map(headers, *, csv_identity=False):
    """The column map a set of headers suggests: {role: {"column": header, "delimiter": d}}. Shown for correction."""
    by_key = {_key(h): h for h in headers if str(h).strip()}
    found = {}
    roles = {**CSV_IDENTITY, **ROLES} if csv_identity else ROLES
    for role, (names, delimiter) in roles.items():
        column = next((by_key[key] for name in names if (key := _key(name)) in by_key), None)
        if column is not None:
            found[role] = {"column": column, "delimiter": delimiter}
    return found


def check_map(column_map, headers):
    """Errors in a corrected map: unknown roles, columns not in the file, or a multi-value column without a delimiter."""
    present, errors = {_key(h) for h in headers}, []
    for role, entry in column_map.items():
        if role not in ROLES and role not in CSV_IDENTITY:
            errors.append(f"Unknown role {role!r}")
        elif _key(entry.get("column", "")) not in present:
            errors.append(f"{role}: column {entry.get('column')!r} is not in the file")
        elif role in SIGNALS and not entry.get("delimiter"):
            errors.append(f"{role}: say how values are separated")
    return errors


def _value(raw, column):
    if column in raw:
        found = raw[column]
    else:
        wanted = _key(column)
        found = next((v for k, v in raw.items() if _key(k) == wanted), None)
    return " ".join(str(found if found is not None else "").split())


def csv_identity(raw, column_map):
    aliases = guess_map(raw, csv_identity=True)
    fields = {
        role: _value(raw, entry["column"])
        for role in CSV_IDENTITY
        if (entry := column_map.get(role) or aliases.get(role))
    }
    if not fields.get("name"):
        fields["name"] = " ".join(fields.get(role, "") for role in ("first_name", "last_name")).strip()
    return fields


def csv_context(raw, column_map):
    full = {str(key): str(value if value is not None else "") for key, value in raw.items()}
    prompt = model_row(full, column_map, full_csv=True)
    return {"row": prompt, "csv_values": full if full != prompt else None}


def _split(raw, column_map, role):
    entry = column_map.get(role)
    if not entry:
        return []
    text = _value(raw, entry["column"])
    return [part.strip() for part in text.split(entry["delimiter"])] if text else []


def signals(raw, column_map):
    """Row signals. Paired by position only when every present column has the same count; otherwise one bundle."""
    lists = {role: _split(raw, column_map, role) for role in SIGNALS}
    present = [values for values in lists.values() if values]
    if not present:
        return {"aligned": True, "items": []}
    if len({len(values) for values in present}) == 1:
        count = len(present[0])
        pick = {role: values if values else [None] * count for role, values in lists.items()}
        items = [
            {"summary": s or None, "date": d or None, "url": u or None}
            for s, d, u in zip(
                pick["signal_summaries"], pick["signal_dates"], pick["signal_urls"], strict=True
            )
        ]
        return {"aligned": True, "items": items}
    return {
        "aligned": False,
        "summaries": lists["signal_summaries"],
        "dates": lists["signal_dates"],
        "urls": lists["signal_urls"],
    }


def signal_urls(lead_signals):
    """Every URL a lead's row offers as a signal, aligned or not."""
    if lead_signals.get("aligned"):
        return [item["url"] for item in lead_signals.get("items", []) if item.get("url")]
    return [url for url in lead_signals.get("urls", []) if url]


def model_row(raw, column_map, *, full_csv=False):
    """Keep every CSV column and bound values. CRM retains its identity and mapped columns."""
    mapped = {_key(entry["column"]) for entry in column_map.values()}
    relevant = {_key(header) for header in IDENTITY} | mapped
    row = {}
    for key, value in raw.items():
        text = str(value if value is not None else "")
        if full_csv:
            row[str(key)] = text[:VALUE_LIMIT]
        elif _key(key) in relevant and (text := " ".join(text.split())):
            row[str(key).strip()[:80]] = text[:VALUE_LIMIT]
    while len(json.dumps(row, ensure_ascii=False)) > ROW_LIMIT:
        longest = max(row, key=lambda k: len(row[k]))
        if not row[longest]:  # Headers alone can exceed the value budget. Keep their names.
            break
        row[longest] = row[longest][: len(row[longest]) // 2]
    return row


def read(raw, column_map, *, full_csv=False):
    """The lead fields one source row supplies through the map."""

    def one(role):
        entry = column_map.get(role)
        return _value(raw, entry["column"])[:VALUE_LIMIT] if entry else ""

    status = re.sub(r"[^a-z_]+", "_", one("live_status").casefold()).strip("_")
    return {
        "row": model_row(raw, column_map, full_csv=full_csv),
        "profile": {
            role: one(role)
            for role in ("title", "headline", "company_description", "industry", "size", "linkedin_activity")
            if one(role)
        },
        "signals": signals(raw, column_map),
        "number_source": one("number_source").casefold()[:40] or None,
        "live_status": "dead" if status in DEAD else status or None,
    }
