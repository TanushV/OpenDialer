"""Trigger rules from the script's `# Triggers` section, and the code checks on each lead's trigger record (section 9).

A trigger is the agent's claim about why to call now. Code, not the model, decides whether it stands: the type is on
the list, the date is inside its window, a page quote is on that page together with the company, and a signal whose
page cannot be read counts only as the row's word for it, capped at C and labelled "from the list, unverified".
"""

import re
from datetime import date

# Section 9 defaults, seeded from the master script's `# Before you dial`; the owner confirms windows and strengths.
DEFAULT_RULES = (
    {"type": "hiring_sellers", "label": "Hiring AEs, SDRs or BDRs", "window_days": 90, "strength": "A"},
    {"type": "new_sales_leader", "label": "New sales leader", "window_days": 180, "strength": "A"},
    {"type": "new_funding", "label": "New funding", "window_days": 180, "strength": "B"},
    {"type": "expansion", "label": "Expansion or new market", "window_days": 180, "strength": "B"},
    {"type": "leaner_team", "label": "Layoffs or a leaner team", "window_days": 180, "strength": "B"},
)
STRENGTHS = ("A", "B", "C")
RULE = re.compile(
    r"^(?:(?P<type>[a-z][a-z0-9_]{1,39})\s*:\s*)?(?P<label>.+?)\s*\(\s*(?P<days>\d{1,4})\s*days?\s*,\s*"
    r"(?P<strength>[ABC])\s*\)\s*$"
)
BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")


def norm(text):
    """Quote matching ignores case, spacing and quote marks, as fact quotes always have."""
    return " ".join(re.sub(r"[‘’“”\"'`]", "", str(text)).casefold().split())


def default_section():
    """The `# Triggers` section a script gets when it has none, in the form `parse_rules` reads."""
    lines = [f"- {r['type']}: {r['label']} ({r['window_days']} days, {r['strength']})" for r in DEFAULT_RULES]
    return "# Triggers\n> Why to call now. The agent may only claim these, inside each window.\n" + "\n".join(
        lines
    )


def parse_rules(lines):
    """Rules from the section's (line number, text) pairs. Returns (rules, errors); cues (`>`) and blanks are skipped."""
    rules, errors = [], []
    for number, line in lines:
        if not line.strip() or line.lstrip().startswith(">"):
            continue
        bullet = BULLET.match(line)
        found = RULE.match(bullet[1].strip()) if bullet else None
        if not found:
            errors.append({"line": number, "message": "Write each trigger as '- Label (90 days, A)'"})
            continue
        kind = found["type"] or re.sub(r"[^a-z0-9]+", "_", found["label"].casefold()).strip("_")[:40]
        days = int(found["days"])
        if not 1 <= days <= 730:
            errors.append({"line": number, "message": "A trigger window is 1 to 730 days"})
        elif any(rule["type"] == kind for rule in rules):
            errors.append({"line": number, "message": f"Trigger '{kind}' appears twice"})
        else:
            rules.append(
                {
                    "type": kind,
                    "label": found["label"][:120],
                    "window_days": days,
                    "strength": found["strength"],
                }
            )
    if not rules and not errors:
        errors.append({"line": 0, "message": "List at least one trigger under # Triggers"})
    return rules, errors


def none_record(bridge=None):
    return {
        "trigger": {
            "type": "none",
            "date": None,
            "source_url": None,
            "quote": None,
            "evidence": None,
            "strength": "none",
            "from_row": False,
        },
        "bridge": bridge,
    }


def _bridge(claim, errors):
    text = " ".join(str((claim.get("bridge") or {}).get("text") or "").split())
    if not 1 <= len(text) <= 300:
        errors.append("bridge: write one spoken line of 1 to 300 characters")
        return None
    return {"text": text, "is_inference": True}


def _day(value, errors):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        errors.append("trigger.date must be YYYY-MM-DD")
        return None


def check_trigger(claim, rules, *, today, row_urls=(), page_text=None, names_lead=False):
    """Check one lead's trigger claim. Returns (record, errors); a record is only returned when errors is empty.

    `page_text` is the text of `source_url` as the app read it (None when not read or unreadable), and
    `names_lead` says whether that page names the lead's company or person. Strength comes from the rule, never from
    the model.
    """
    errors = []
    trigger = claim.get("trigger") or {}
    bridge = _bridge(claim, errors)
    kind = str(trigger.get("type") or "").strip()
    if kind == "none":
        return (none_record(bridge), []) if not errors else (None, errors)
    rule = next((r for r in rules if r["type"] == kind), None)
    if rule is None:
        errors.append(
            f"trigger.type {kind!r} is not on the list; use one of {[r['type'] for r in rules]} or none"
        )
        return None, errors
    day = _day(trigger.get("date"), errors)
    if day and day > today:
        errors.append("trigger.date is in the future")
    elif day and (today - day).days > rule["window_days"]:
        errors.append(f"trigger.date is outside the {rule['window_days']}-day window for {kind}; use none")
    url = str(trigger.get("source_url") or "").strip() or None
    quote = " ".join(str(trigger.get("quote") or "").split()) or None
    evidence = trigger.get("evidence")
    from_row = bool(url and url in row_urls)
    if evidence == "page":
        if not url or not url.startswith(("http://", "https://")):
            errors.append("trigger.source_url must be the http(s) page you read")
        elif not quote or not 8 <= len(quote) <= 500:
            errors.append("page evidence needs an exact quote of 8 to 500 characters from that page")
        elif page_text is None:
            errors.append(
                "the page could not be read; use evidence row_only only if the signal came from the row"
            )
        elif norm(quote) not in norm(page_text):
            errors.append(f"the quote is not on {url}; copy it exactly")
        elif not names_lead:
            errors.append(f"{url} does not name this lead or their company")
        strength = rule["strength"]
    elif evidence == "row_only":
        if not row_urls and not from_row:
            errors.append("row_only evidence needs a signal in the lead's row")
        from_row, quote = True, None
        strength = max(rule["strength"], "C")  # A < B < C as strings: the weaker of the two.
    else:
        errors.append("trigger.evidence must be page or row_only")
        strength = None
    if errors:
        return None, errors
    record = {
        "type": kind,
        "date": day.isoformat(),
        "source_url": url,
        "quote": quote,
        "evidence": evidence,
        "strength": strength,
        "from_row": from_row,
    }
    return {"trigger": record, "bridge": bridge}, []
