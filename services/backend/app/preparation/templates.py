"""One Markdown script: the seller's wording, options picked per lead, and blocks the AI writes per lead."""

import hashlib
import json
import re
from string import Formatter

from pydantic import BaseModel, ConfigDict, Field

# Heading -> (key, required, shape). "choice": plain text or bullets (one bullet is picked per lead);
# "fixed": plain text only; "list": bullets, up to `PICKS` are picked and ordered; "cards": one `##`
# per item; "bullets": plain lists that are not spoken (facts and banned claims).
HEADINGS = {
    "opening": ("opening", True, "choice"),
    "screening": ("screening", True, "fixed"),
    "gatekeeper": ("gatekeeper", False, "choice"),
    "discovery": ("discovery", True, "list"),
    "offers": ("offers", False, "cards"),
    "objections": ("objections", False, "cards"),
    "close": ("close", True, "choice"),
    "facts": ("facts", False, "bullets"),
    "never say": ("never_say", False, "bullets"),
}
TITLES = {
    "opening": "Opening",
    "screening": "Screening",
    "gatekeeper": "Gatekeeper",
    "discovery": "Discovery",
    "offers": "Offers",
    "objections": "Objections",
    "close": "Close",
}
ORDER = ("opening", "screening", "gatekeeper", "discovery", "offers", "objections", "close")
# How many options of each section a lead's script uses: (minimum, maximum). Objections keep all.
PICKS = {"opening": (1, 1), "gatekeeper": (1, 1), "close": (1, 1), "discovery": (1, 3), "offers": (1, 2)}
PLACEHOLDERS = {"name", "first_name", "company", "rep", "seller"}
MAX_BYTES = 40000
MAX_OPTIONS = 20
MAX_BLOCKS = 20
MAX_WRITTEN = 400
BLOCK = re.compile(r"\{\{(.*?)\}\}", re.S)
TOKEN = re.compile(r"\[\[(ai-\d+)\]\]")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")

STARTER = """# Opening
Hi {first_name}, this is {rep} with {seller}. {{ one sentence on why we're calling {company} now, based on research }}

# Screening
{rep} from {seller}, calling about your current process.

# Gatekeeper
Who handles this for {company}?

# Discovery
- How are you handling this today?
- What would you change about it if you could?
- Who else weighs in on a decision like this?

# Offers
## Short review
A 15-minute review of how you handle this today, with two ideas you can use either way.

# Objections
## Not interested
Fair enough. Is that because it's handled, or because now isn't the time?

## Send me an email
Happy to. What should it cover so it's worth opening?

# Close
- Would a 15-minute call later this week be useful?
"""


class PreparationPolicy(BaseModel):
    """How many leads each agent thread takes and how many threads run at once."""

    # Older stores kept rolling-window settings here; they are ignored, not migrated.
    model_config = ConfigDict(extra="ignore")
    leads_per_agent: int = Field(default=5, ge=1, le=20)
    agents_at_once: int = Field(default=2, ge=1, le=4)


class ScriptError(ValueError):
    """Every problem found in a pasted script, each with its line number (0 = whole script)."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(
            "; ".join(f"line {e['line']}: {e['message']}" if e["line"] else e["message"] for e in errors)
        )


def template_version(template):
    return (
        hashlib.sha256(json.dumps(template, sort_keys=True).encode()).hexdigest()[:16]
        if template
        else "no-script"
    )


def _placeholders(text, line, errors):
    try:
        parsed = list(Formatter().parse(text))
    except ValueError:
        errors.append(
            {"line": line, "message": "Unbalanced { or }; use {name} for fields and {{ }} for AI text"}
        )
        return
    for _, key, spec, conversion in parsed:
        if key is not None and (key not in PLACEHOLDERS or spec or conversion):
            errors.append(
                {
                    "line": line,
                    "message": f"Unknown field {{{key}}}. Use {', '.join('{' + p + '}' for p in sorted(PLACEHOLDERS))}",
                }
            )


def parse_markdown(markdown):
    """Parse a pasted script into the template every other part of the app reads.

    Raises ScriptError listing every problem, so the editor can show them all at once.
    """
    errors = []
    if len(markdown.encode()) > MAX_BYTES:
        raise ScriptError([{"line": 0, "message": "Keep the script under 40 KB"}])
    raw, current, card = {}, None, None
    for number, line in enumerate(markdown.splitlines(), 1):
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading and len(heading[1]) == 1:
            name = " ".join(heading[2].casefold().split())
            if name not in HEADINGS:
                errors.append(
                    {
                        "line": number,
                        "message": f"Unknown section '# {heading[2]}'. Use: "
                        + ", ".join(
                            "# " + h.title() if h != "never say" else "# Never say" for h in HEADINGS
                        ),
                    }
                )
                current = SKIP
                continue
            key = HEADINGS[name][0]
            if key in raw:
                errors.append({"line": number, "message": f"'# {heading[2]}' appears twice"})
                current = SKIP
                continue
            current, card = key, None
            raw[key] = {"line": number, "lines": [], "cards": []}
            continue
        if current == SKIP:
            continue
        if heading:
            if current is None:
                continue
            if HEADINGS_BY_KEY[current] != "cards":
                errors.append(
                    {"line": number, "message": "Use ## headings only under # Offers or # Objections"}
                )
                continue
            card = {"title": heading[2].strip(), "line": number, "lines": []}
            raw[current]["cards"].append(card)
            continue
        if current is None:
            if line.strip():
                errors.append({"line": number, "message": "Put this text under a # section heading"})
            continue
        if HEADINGS_BY_KEY[current] == "cards":
            if card is None:
                if line.strip():
                    errors.append(
                        {
                            "line": number,
                            "message": f"Put each item under its own ## heading in {TITLES[current]}",
                        }
                    )
                continue
            card["lines"].append((number, line))
        else:
            raw[current]["lines"].append((number, line))
    for name, (key, required, _) in HEADINGS.items():
        if required and key not in raw:
            errors.append({"line": 0, "message": f"Add a '# {name.title()}' section"})

    blocks, sections, facts, never = [], [], [], []

    def ai(text, line, key, option):
        """Replace each {{ instruction }} with a stable token and record the instruction."""

        def replace(match):
            instruction = " ".join(match[1].split())
            if not 3 <= len(instruction) <= 300:
                errors.append({"line": line, "message": "Each {{ }} instruction needs 3 to 300 characters"})
            blocks.append(
                {"id": f"ai-{len(blocks) + 1}", "instruction": instruction, "section": key, "option": option}
            )
            return f"[[ai-{len(blocks)}]]"

        replaced = BLOCK.sub(replace, text)
        if "{{" in replaced or "}}" in replaced:
            errors.append({"line": line, "message": "Close every {{ with }}"})
            return replaced
        _placeholders(TOKEN.sub("", replaced), line, errors)
        return replaced

    def paragraphs(lines):
        return "\n".join(line for _, line in lines).strip()

    def bullets(lines, key):
        items, plain = [], []
        for number, line in lines:
            match = BULLET.match(line)
            if match:
                items.append([number, match[1].strip()])
            elif line.strip():
                if items and line.startswith((" ", "\t")):
                    items[-1][1] += " " + line.strip()
                else:
                    plain.append(number)
        return items, plain

    for key in ORDER:
        if key not in raw:
            continue
        entry, shape = raw[key], HEADINGS_BY_KEY[key]
        title = TITLES[key]
        options = []
        if shape == "cards":
            for index, item in enumerate(entry["cards"], 1):
                text = paragraphs(item["lines"])
                if not text:
                    errors.append({"line": item["line"], "message": f"'## {item['title']}' has no text"})
                    continue
                option = f"{key}-{index}"
                options.append(
                    {"id": option, "title": item["title"][:120], "text": ai(text, item["line"], key, option)}
                )
            if not options:
                errors.append({"line": entry["line"], "message": f"Add at least one ## item to {title}"})
        else:
            items, plain = bullets(entry["lines"], key)
            if shape == "list" or (shape == "choice" and items):
                if plain:
                    errors.append(
                        {
                            "line": plain[0],
                            "message": f"{title} mixes bullets and plain text; make every line a bullet",
                        }
                    )
                for index, (number, text) in enumerate(items, 1):
                    option = f"{key}-{index}"
                    options.append({"id": option, "title": None, "text": ai(text, number, key, option)})
                if not options:
                    errors.append({"line": entry["line"], "message": f"Add at least one bullet to {title}"})
            else:
                text = paragraphs(entry["lines"])
                if not text:
                    errors.append({"line": entry["line"], "message": f"{title} is empty"})
                    continue
                if items:
                    errors.append(
                        {"line": items[0][0], "message": f"{title} must be plain text, not bullets"}
                    )
                if key == "screening":
                    if BLOCK.search(text):
                        errors.append(
                            {"line": entry["line"], "message": "Screening must be fixed text, without {{ }}"}
                        )
                    if "{seller}" not in text:
                        errors.append(
                            {
                                "line": entry["line"],
                                "message": "Screening must name your business with {seller}",
                            }
                        )
                    if len(text) > 400:
                        errors.append(
                            {"line": entry["line"], "message": "Keep Screening under 400 characters"}
                        )
                options.append(
                    {"id": f"{key}-1", "title": None, "text": ai(text, entry["line"], key, f"{key}-1")}
                )
        if len(options) > MAX_OPTIONS:
            errors.append({"line": entry["line"], "message": f"Use at most {MAX_OPTIONS} items in {title}"})
        if key == "opening":
            for option in options:
                if "{seller}" not in option["text"] or "{rep}" not in option["text"]:
                    errors.append(
                        {
                            "line": entry["line"],
                            "message": "Every Opening must say who is calling: use {rep} and {seller}",
                        }
                    )
                    break
        sections.append(
            {
                "key": key,
                "title": title,
                "shape": shape,
                "options": options,
                "pick": list(PICKS[key]) if key in PICKS and len(options) > 1 else None,
            }
        )
    for key, target in (("facts", facts), ("never_say", never)):
        if key not in raw:
            continue
        items, plain = bullets(raw[key]["lines"], key)
        if plain:
            errors.append({"line": plain[0], "message": "Write Facts and Never say as bullets"})
        for number, text in items:
            if BLOCK.search(text):
                errors.append({"line": number, "message": "Facts and Never say cannot contain {{ }}"})
            target.append(text[:400])
    if len(blocks) > MAX_BLOCKS:
        errors.append({"line": 0, "message": f"Use at most {MAX_BLOCKS} {{{{ }}}} blocks"})
    if errors:
        raise ScriptError(sorted(errors, key=lambda e: e["line"]))
    return {
        "format": 2,
        "markdown": markdown,
        "sections": sections,
        "blocks": blocks,
        "facts": facts,
        "never_say": never,
    }


HEADINGS_BY_KEY = {key: shape for key, _, shape in HEADINGS.values()}
SKIP = object()  # Lines under an unknown or repeated heading are reported once, at the heading.


def section_markdown(template, key):
    """The Markdown body of one section, for script-change proposals."""
    lines, inside = [], False
    for line in template["markdown"].splitlines():
        heading = re.match(r"^#\s+(.+?)\s*#*\s*$", line)
        if heading:
            inside = HEADINGS.get(" ".join(heading[1].casefold().split()), (None,))[0] == key
            continue
        if inside:
            lines.append(line)
    return "\n".join(lines).strip()


def replace_section(template, key, body):
    """Return the whole script Markdown with one section's body replaced."""
    out, inside, done = [], False, False
    for line in template["markdown"].splitlines():
        heading = re.match(r"^#\s+(.+?)\s*#*\s*$", line)
        if heading:
            inside = HEADINGS.get(" ".join(heading[1].casefold().split()), (None,))[0] == key
            out.append(line)
            if inside:
                out.extend(["", body.strip(), ""])
                done = True
            continue
        if not inside:
            out.append(line)
    if not done:
        raise ValueError("That section is not in the current script")
    return "\n".join(out).strip() + "\n"


def default_picks(template):
    picks = {}
    for section in template["sections"]:
        ids = [option["id"] for option in section["options"]]
        if section["key"] == "objections":
            picks[section["key"]] = ids
        elif section["pick"]:
            picks[section["key"]] = (
                ids[: section["pick"][1]] if section["key"] in {"discovery", "offers"} else ids[:1]
            )
    return picks


def fill_fields(text, values):
    return text.format_map(values)


def render(template, *, lead, seller, rep, picks=None, written=None):
    """The script a rep reads for one lead. Without picks or written text, the seller's defaults."""
    name = (lead.get("name") or "").strip()
    values = {
        "name": name or "there",
        "first_name": name.split()[0] if name and name.casefold() != "unknown" else "there",
        "company": lead.get("company") or "your company",
        "seller": seller,
        "rep": rep,
    }
    written, chosen = written or {}, {**default_picks(template), **(picks or {})}

    def text_of(raw):
        filled = TOKEN.sub(lambda m: "\0" + m[1] + "\0", raw)
        filled = fill_fields(filled, values)
        return re.sub(r"\0(ai-\d+)\0", lambda m: written.get(m[1], {}).get("text", ""), filled)

    def complete(option):
        """An option whose AI text was not written is still usable only if text remains."""
        return all(block in written for block in TOKEN.findall(option["text"])) or bool(
            TOKEN.sub("", option["text"]).strip(" .,;:-")
        )

    sections = []
    for section in template["sections"]:
        order = chosen.get(section["key"]) or [option["id"] for option in section["options"]]
        rank = {identity: index for index, identity in enumerate(order)}
        options = [
            {
                "id": option["id"],
                "title": option["title"],
                "text": re.sub(r"[ \t]{2,}", " ", text_of(option["text"])).strip(),
                "picked": option["id"] in rank,
                "rank": rank.get(option["id"]),
            }
            for option in section["options"]
            if complete(option)
        ]
        options.sort(key=lambda o: (o["rank"] is None, o["rank"] if o["rank"] is not None else 0))
        if options:
            sections.append({"key": section["key"], "title": section["title"], "options": options})

    def first(key, default=""):
        section = next((s for s in sections if s["key"] == key), None)
        picked = [o for o in section["options"] if o["picked"]] if section else []
        return (picked or (section["options"] if section else [{}]))[0].get("text", default)

    def picked(key):
        section = next((s for s in sections if s["key"] == key), None)
        return [o for o in section["options"] if o["picked"]] if section else []

    return {
        "sections": sections,
        "picks": {key: list(value) for key, value in chosen.items()},
        "written": {key: dict(value) for key, value in written.items()},
        "opener": first("opening"),
        "screening_intro": first("screening"),
        "gatekeeper": first("gatekeeper"),
        "discovery_questions": [o["text"] for o in picked("discovery")],
        "closing_question": first("close"),
        "offers": [{"title": o["title"], "text": o["text"]} for o in picked("offers")],
        "objection_cards": [
            {"id": o["id"], "tags": [o["title"]], "text": o["text"]} for o in picked("objections")
        ],
    }


NUMBER = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
PROMISES = re.compile(r"\b(guarantee[sd]?|promise[sd]?|risk[- ]free|100%)\b", re.I)


def check_submission(template, picks, written, facts, row, *, required_blocks=None):
    """Close-out rules for one lead's script. Returns a list of plain errors the agent can fix.

    `facts` maps saved fact ids to statements for this lead; `row` is the lead's imported row,
    whose columns can be cited as `row:<column>`.
    """
    errors = []
    sections = {section["key"]: section for section in template["sections"]}
    for key, chosen in (picks or {}).items():
        section = sections.get(key)
        if not section or section["pick"] is None and key != "objections":
            errors.append(f"'{key}' has no options to pick; leave it out")
            continue
        ids = [option["id"] for option in section["options"]]
        if not isinstance(chosen, list) or len(set(chosen)) != len(chosen):
            errors.append(f"Picks for '{key}' must be a list of distinct option ids")
            continue
        unknown = [identity for identity in chosen if identity not in ids]
        if unknown:
            errors.append(f"Unknown {key} option ids {unknown}; valid ids are {ids}")
        if key == "objections":
            if sorted(chosen) != sorted(ids):
                errors.append(f"Order every objection exactly once: {ids}")
            continue
        low, high = section["pick"]
        if not low <= len(chosen) <= high:
            errors.append(f"Pick {low} to {high} '{key}' options (you picked {len(chosen)})")
    for section in template["sections"]:
        if section["pick"] and section["key"] not in (picks or {}):
            errors.append(f"Pick from '{section['key']}': {[o['id'] for o in section['options']]}")
    chosen = {**default_picks(template), **(picks or {})}
    needed = set()
    for section in template["sections"]:
        use = set(chosen.get(section["key"], [o["id"] for o in section["options"]]))
        for option in section["options"]:
            if option["id"] in use:
                needed.update(TOKEN.findall(option["text"]))
    if required_blocks is not None:
        needed &= set(required_blocks)
    supplied = set(written or {})
    missing = sorted(needed - supplied, key=lambda b: int(b.split("-")[1]))
    if missing:
        errors.append(f"Write text for {missing}")
    extra = sorted(supplied - needed)
    if extra:
        errors.append(f"{extra} are not in the options you picked; remove them")
    allowed_numbers = " ".join([*template.get("facts", []), *facts.values(), *map(str, row.values())])
    banned = [phrase.casefold() for phrase in template.get("never_say", [])]
    for block, entry in (written or {}).items():
        text, cited = (entry.get("text") or "").strip(), entry.get("fact_ids") or []
        if not text or len(text) > MAX_WRITTEN:
            errors.append(f"{block}: write 1 to {MAX_WRITTEN} characters")
            continue
        if "{" in text or "}" in text or "[[" in text:
            errors.append(f"{block}: plain text only, no braces")
        if not cited:
            errors.append(f"{block}: cite at least one saved fact id or row:<column>")
        for identity in cited:
            if identity.startswith("row:"):
                if not str(row.get(identity[4:], "")).strip():
                    errors.append(f"{block}: {identity} is not a filled column of this lead's row")
            elif identity not in facts:
                errors.append(f"{block}: {identity} is not a saved fact for this lead")
        for number in NUMBER.findall(text):
            if number.strip("$%,.") and number not in allowed_numbers:
                errors.append(f"{block}: '{number}' is not in a cited fact, the row or your Facts")
        if PROMISES.search(text) and not PROMISES.search(" ".join(template.get("facts", []))):
            errors.append(f"{block}: no guarantees or promises")
        lowered = text.casefold()
        for phrase in banned:
            if phrase and phrase in lowered:
                errors.append(f"{block}: uses a Never say phrase ('{phrase}')")
    return errors
