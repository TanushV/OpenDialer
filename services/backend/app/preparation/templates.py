"""One Markdown script: the seller's wording, options picked per lead, and blocks the AI writes per lead."""

import hashlib
import json
import re
from functools import lru_cache
from string import Formatter

from pydantic import BaseModel, ConfigDict, Field

# Heading -> (key, required, shape). "choice": plain text or bullets (one bullet is picked per lead);
# "fixed": plain text only; "list": bullets, up to `PICKS` are picked and ordered; "cards": one `##`
# per item; "bullets": plain lists that are not spoken (facts and banned claims).
HEADINGS = {
    "opening": ("opening", True, "choice"),
    "screening": ("screening", False, "fixed"),
    "gatekeeper": ("gatekeeper", False, "choice"),
    "discovery": ("discovery", False, "list"),
    "pitch": ("pitch", False, "choice"),
    "offers": ("offers", False, "cards"),
    "objections": ("objections", False, "cards"),
    "close": ("close", True, "choice"),
    "facts": ("facts", False, "bullets"),
    "never say": ("never_say", False, "bullets"),
    # Optional wording for calls after the first and for the final attempt's mailbox; not
    # picked per lead and not spoken on a first call.
    "follow-up": ("follow_up", False, "notes"),
    "voicemail": ("voicemail", False, "notes"),
}
TITLES = {
    "opening": "Opening",
    "screening": "Screening",
    "gatekeeper": "Gatekeeper",
    "discovery": "Discovery",
    "pitch": "Pitch",
    "offers": "Offers",
    "objections": "Objections",
    "close": "Close",
}
ALIASES = {
    "opener": "opening",
    "problem and outcome": "pitch",
    "problem & outcome": "pitch",
    "before you dial": "preparation",
    "delivery rules": "instructions",
    "common objections": "objections",
    "book the meeting": "close",
}
CONTEXT = {"goal", "instructions", "preparation"}
# `# Triggers` is read by code (section 9): the agent may only claim these, inside each window.
TRIGGERS = "triggers"
PATHS = "paths"
PATH_DEFINITION = re.compile(r"^([a-z][a-z0-9-]*)( \(default\))?:\s*(.+)$")
PATH_TAG = re.compile(r"\s*\[path:\s*([^\]]+)\]\s*$", re.I)
# Call insights plan P7: `# Variants` declares one script test; code assigns each lead (by company) at random.
VARIANTS = "variants"
VARIANT_DEFINITION = re.compile(r"^([a-z][a-z0-9-]*)\s*\((\d{1,3})%\):\s*(.+)$")
VARIANT_TAG = re.compile(r"\s*\[variant:\s*([^\]]+)\]\s*$", re.I)
WHEN = re.compile(r"^\s*when:\s*(.+)$", re.I)
# How many options of each section a lead's script uses: (minimum, maximum). Objections keep all.
PICKS = {
    "opening": (1, 1),
    "gatekeeper": (1, 1),
    "close": (1, 1),
    "pitch": (1, 1),
    "discovery": (1, 3),
    "offers": (1, 2),
}
# `{callback_number}` is the caller ID of the call, filled when it is dialed (work package 6.13). It is optional:
# a prospect who calls the caller ID back reaches the rep or leaves a missed callback (owner, November 23).
PLACEHOLDERS = {"name", "first_name", "company", "rep", "seller", "callback_number"}
VOICEMAIL_FIELDS = ("rep", "seller")
MAX_BYTES = 64000
MAX_OPTIONS = 20
MAX_BLOCKS = 20
MAX_WRITTEN = 400
BLOCK = re.compile(r"\{\{(.*?)\}\}", re.S)
TOKEN = re.compile(r"\[\[(ai-\d+)\]\]")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
COMMENT = re.compile(r"<!--.*?-->", re.S)

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
    """Generation identity excludes source formatting; words, cues and claims remain inputs.

    One variant's view of a master (plan P7) keeps the master's identity, so its scripts are current for that master.
    """
    if isinstance(template, dict) and template.get("master_version"):
        return template["master_version"]

    def canonical(value):
        if isinstance(value, str):
            return " ".join(value.split())
        if isinstance(value, list):
            return [canonical(item) for item in value]
        if isinstance(value, dict):
            return {
                k: canonical(v)
                for k, v in value.items()
                if k not in {"markdown", "line", "source_order", "spoken"} and v not in (None, {}, [], "")
            }
        return value

    return (
        hashlib.sha256(json.dumps(canonical(template), sort_keys=True).encode()).hexdigest()[:16]
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


def _bullet_groups(lines):
    """Use sibling bullets as options and keep their indented lines with them."""
    groups, shared_cues, loose_text = [], [], []
    bullet_indent = 0
    for number, line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        inside = bool(groups) and indent > bullet_indent
        if not inside and (match := BULLET.match(line)):
            groups.append([(number, match[1].strip())])
            bullet_indent = indent
        elif inside or (groups and not stripped):
            groups[-1].append((number, stripped))
        elif stripped.startswith(">"):
            shared_cues.append(stripped[1:].strip())
        elif stripped:
            loose_text.append(number)
    return groups, "\n".join(shared_cues).strip(), loose_text


def parse_markdown(markdown, *, finished=False):
    """Parse a Markdown playbook once; blockquotes are cues, never spoken wording."""
    if len(markdown.encode()) > MAX_BYTES:
        raise ScriptError([{"line": 0, "message": "Keep the script under 64 KB"}])
    errors, raw, context = [], {}, {}
    current, card = None, None
    # Keep source line numbers and the original editor text when omitting comments from speech.
    source = COMMENT.sub(lambda match: re.sub(r"[^\r\n]", "", match[0]), markdown)
    if "<!--" in source:
        number = source.count("\n", 0, source.index("<!--")) + 1
        raise ScriptError([{"line": number, "message": "Close the Markdown comment with -->"}])
    for number, line in enumerate(source.splitlines(), 1):
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading and len(heading[1]) == 1:
            name = " ".join(heading[2].casefold().split())
            name = ALIASES.get(name, name)
            key = HEADINGS.get(
                name,
                (
                    name
                    if name in CONTEXT or name in {TRIGGERS, PATHS, VARIANTS}
                    else "custom:" + re.sub(r"[^a-z0-9]+", "-", name).strip("-"),
                    False,
                    "fixed",
                ),
            )[0]
            if key in raw:
                errors.append({"line": number, "message": f"'# {heading[2]}' appears twice"})
                current = SKIP
                continue
            current, card = key, None
            raw[key] = {"line": number, "title": TITLES.get(key, heading[2]), "lines": [], "cards": []}
            continue
        if current == SKIP:
            continue
        if heading and current and current not in CONTEXT and current not in {TRIGGERS, PATHS, VARIANTS}:
            card = {"title": heading[2].strip(), "line": number, "lines": []}
            raw[current]["cards"].append(card)
            continue
        if current is None:
            if line.strip():
                errors.append({"line": number, "message": "Put this text under a # section heading"})
            continue
        (card["lines"] if card is not None else raw[current]["lines"]).append((number, line))
    for name, (key, required, _) in HEADINGS.items():
        if not finished and required and key not in raw:
            errors.append({"line": 0, "message": f"Add a '# {name.title()}' section"})
    blocks, sections, facts, never = [], [], [], []
    paths = {}
    default_path = None
    for number, line in raw.get(PATHS, {}).get("lines", []):
        if not line.strip():
            continue
        bullet = BULLET.match(line)
        definition = PATH_DEFINITION.fullmatch(bullet[1]) if bullet else None
        if not definition:
            errors.append({"line": number, "message": "Declare each path as - path-name: when to use it"})
            continue
        key, default, guidance = definition.groups()
        if key in paths or default and default_path:
            errors.append({"line": number, "message": "Use unique path names and at most one default"})
            continue
        paths[key] = guidance.strip()
        if default:
            default_path = key
    if PATHS in raw and not paths:
        errors.append({"line": raw[PATHS]["line"], "message": "Declare at least one path"})
    variants, when = {}, []
    for number, line in raw.get(VARIANTS, {}).get("lines", []):
        if not line.strip():
            continue
        condition = WHEN.match(line)
        if condition:
            when.extend(name.strip().casefold() for name in condition[1].split(",") if name.strip())
            continue
        bullet = BULLET.match(line)
        definition = VARIANT_DEFINITION.fullmatch(bullet[1].strip()) if bullet else None
        if not definition:
            errors.append(
                {
                    "line": number,
                    "message": "Declare each variant as - variant-name (50%): what you expect it to do",
                }
            )
            continue
        key, share, hypothesis = definition.groups()
        if key in variants:
            errors.append({"line": number, "message": "Use unique variant names"})
            continue
        variants[key] = {"share": int(share), "hypothesis": hypothesis.strip()[:300]}
    if VARIANTS in raw:
        if len(variants) < 2:
            errors.append(
                {"line": raw[VARIANTS]["line"], "message": "Declare at least two variants to compare"}
            )
        elif sum(v["share"] for v in variants.values()) != 100 or any(
            v["share"] < 1 for v in variants.values()
        ):
            errors.append(
                {
                    "line": raw[VARIANTS]["line"],
                    "message": "Variant shares must each be at least 1% and add up to 100%",
                }
            )
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]*", name) for name in when):
            errors.append(
                {
                    "line": raw[VARIANTS]["line"],
                    "message": "Write When: as trigger types, such as When: hiring_sellers",
                }
            )

    def ai(text, line, key, option):
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
        else:
            _placeholders(TOKEN.sub("", replaced), line, errors)
        return replaced

    def split_cues(lines):
        spoken, cues = [], []
        for number, line in lines:
            if line.lstrip().startswith(">"):
                cues.append(line.lstrip()[1:].strip())
            else:
                spoken.append((number, line))
        return spoken, "\n".join(cues).strip()

    def reference(key, value):
        entry = raw[key]
        return {"key": key, "title": entry["title"], "text": value, "spoken": False}

    def text(lines):
        return "\n".join(line for _, line in lines).strip()

    def option(lines, key, identity, title=None, inherited=""):
        path_ids, variant_ids = [], []
        while title:
            tag, kind = PATH_TAG.search(title), "path"
            if not tag:
                tag, kind = VARIANT_TAG.search(title), "variant"
            if not tag:
                break
            title = title[: tag.start()].strip()
            names = list(dict.fromkeys(name.strip().casefold() for name in tag[1].split(",")))
            known = paths if kind == "path" else variants
            unknown = [name or "(empty)" for name in names if name not in known]
            if unknown or key == "objections":
                errors.append(
                    {
                        "line": lines[0][0] if lines else raw[key]["line"],
                        "message": "Keep all objections shared"
                        if key == "objections"
                        else f"Unknown script {kind}s: " + ", ".join(unknown),
                    }
                )
            if kind == "path":
                path_ids = names
            else:
                variant_ids = names
        spoken, notes = split_cues(lines)
        value = text(spoken)
        if not value:
            errors.append(
                {
                    "line": lines[0][0] if lines else raw[key]["line"],
                    "message": f"'{title or TITLES.get(key, key)}' has no spoken text",
                }
            )
        if key == "screening" and not finished:
            if BLOCK.search(value):
                errors.append(
                    {"line": raw[key]["line"], "message": "Screening must be fixed text, without {{ }}"}
                )
            if "{seller}" not in value:
                errors.append(
                    {"line": raw[key]["line"], "message": "Screening must name your business with {seller}"}
                )
            if len(value) > 400:
                errors.append({"line": raw[key]["line"], "message": "Keep Screening under 400 characters"})
        result = {
            "id": identity,
            "title": title[:120] if title else title,
            "text": ai(value, raw[key]["line"], key, identity),
        }
        if path_ids:
            result["paths"] = path_ids
        if variant_ids:
            result["variants"] = variant_ids
        cues = "\n".join(v for v in (inherited, notes) if v)
        if cues:
            _placeholders(cues, raw[key]["line"], errors)
            result["notes"] = cues
        return result

    for key, entry in raw.items():
        if key in CONTEXT or key in {
            TRIGGERS,
            PATHS,
            VARIANTS,
            "facts",
            "never_say",
            "follow_up",
            "voicemail",
        }:
            continue
        shape = HEADINGS_BY_KEY.get(key, "fixed")
        options = []
        if entry["cards"]:
            prelude, cues = split_cues(entry["lines"])
            if text(prelude):
                errors.append(
                    {
                        "line": prelude[0][0],
                        "message": f"Put each item under its own ## heading in {entry['title']}",
                    }
                )
            if key == "screening":
                errors.append({"line": entry["line"], "message": "Screening must be plain text"})
            for index, item in enumerate(entry["cards"], 1):
                options.append(option(item["lines"], key, f"{key}-{index}", item["title"], cues))
                options[-1]["_line"] = item["line"]
        elif shape == "cards" and not finished:
            errors.append({"line": entry["line"], "message": f"Add at least one ## item to {entry['title']}"})
        else:
            items, shared, plain = (
                _bullet_groups(entry["lines"]) if shape in {"choice", "list"} else ([], "", [])
            )
            if items:
                if plain:
                    errors.append(
                        {
                            "line": plain[0],
                            "message": f"{entry['title']} mixes bullets and plain text. Indent a continuation or start it with a bullet",
                        }
                    )
                options = [
                    option(lines, key, f"{key}-{i}", inherited=shared) for i, lines in enumerate(items, 1)
                ]
            else:
                spoken, cues = split_cues(entry["lines"])
                if text(spoken):
                    options = [option(entry["lines"], key, f"{key}-1")]
                elif not key.startswith("custom:"):
                    errors.append({"line": entry["line"], "message": f"{entry['title']} is empty"})
                elif cues:
                    context[entry["title"]] = cues
                    sections.append(reference(key, cues))
        if paths and any(o.get("paths") for o in options):
            # A section that mixes path options with untagged ones: the untagged ones are shared, kept as written
            # on every path (work package 4.5). The model writes only inside the chosen path's options.
            for shared in (o for o in options if not o.get("paths") and TOKEN.search(o["text"])):
                errors.append(
                    {
                        "line": shared.get("_line") or entry["line"],
                        "message": f"Shared text in {entry['title']} cannot have {{{{ }}}}. Move the block into an "
                        "option tagged [path: ...], or tag this option with the paths it belongs to",
                    }
                )
        for o in options:
            o.pop("_line", None)
        if len(options) > MAX_OPTIONS:
            errors.append(
                {"line": entry["line"], "message": f"Use at most {MAX_OPTIONS} items in {entry['title']}"}
            )
        if (
            not finished
            and key == "opening"
            and any("{rep}" not in o["text"] or "{seller}" not in o["text"] for o in options)
        ):
            errors.append(
                {
                    "line": entry["line"],
                    "message": "Every Opening must say who is calling: use {rep} and {seller}",
                }
            )
        if options:
            sections.append(
                {
                    "key": key,
                    "title": entry["title"],
                    "shape": shape,
                    "spoken": True,
                    "options": options,
                    "pick": list(PICKS[key]) if key in PICKS and len(options) > 1 else None,
                }
            )
    for key, target in (("facts", facts), ("never_say", never)):
        if key not in raw:
            continue
        for number, line in raw[key]["lines"]:
            if not line.strip():
                continue
            match = BULLET.match(line)
            if not match:
                errors.append({"line": number, "message": "Write Facts and Never say as bullets"})
                continue
            if BLOCK.search(match[1]):
                errors.append({"line": number, "message": "Facts and Never say cannot contain {{ }}"})
            target.append(match[1][:400])
    triggers = None
    if TRIGGERS in raw:
        from app.prep.criteria import parse_rules

        triggers, problems = parse_rules(raw[TRIGGERS]["lines"])
        errors.extend(problems)
        sections.append(reference(TRIGGERS, text(raw[TRIGGERS]["lines"])))
    notes = {}
    for key in raw:
        if key in CONTEXT:
            context[key] = text(raw[key]["lines"])
            sections.append(reference(key, context[key]))
    for key in ("facts", "never_say"):
        if key in raw:
            sections.append(reference(key, text(raw[key]["lines"])))
    for key in ("follow_up", "voicemail"):
        if key not in raw:
            continue
        value = text(raw[key]["lines"])
        if BLOCK.search(value):
            errors.append(
                {"line": raw[key]["line"], "message": "Follow-up and Voicemail cannot contain {{ }}"}
            )
        elif value:
            spoken, cues = split_cues(raw[key]["lines"])
            value = text(spoken)
            _placeholders(value, raw[key]["line"], errors)
            missing = [f for f in VOICEMAIL_FIELDS if "{" + f + "}" not in value]
            if key == "voicemail" and missing and not finished:
                errors.append(
                    {
                        "line": raw[key]["line"],
                        "message": "Voicemail must say who is calling: add "
                        + ", ".join("{" + f + "}" for f in missing),
                    }
                )
            notes[key] = value[:1000]
            sections.append(reference(key, text(raw[key]["lines"])))
            if cues:
                context[key + " directions"] = cues
    if len(blocks) > MAX_BLOCKS:
        errors.append({"line": 0, "message": f"Use at most {MAX_BLOCKS} {{{{ }}}} blocks"})
    for section in sections:
        if section["key"] in {"opening", "close"} and any(
            not any(not o.get("paths") or path in o["paths"] for o in section.get("options", []))
            for path in paths
        ):
            errors.append({"line": raw[section["key"]]["line"], "message": "Every path needs this section"})
        if section["key"] in {"opening", "close"} and any(
            not any(not o.get("variants") or name in o["variants"] for o in section.get("options", []))
            for name in variants
        ):
            errors.append(
                {"line": raw[section["key"]]["line"], "message": "Every variant needs this section"}
            )
    if variants and not any(o.get("variants") for s in sections for o in s.get("options", [])):
        errors.append(
            {
                "line": raw[VARIANTS]["line"],
                "message": "Tag the options each variant uses: ## Title [variant: name]",
            }
        )
    if errors:
        raise ScriptError(errors)
    return {
        "format": 2,
        "source_order": True,
        "markdown": markdown,
        "sections": [
            {**section, "options": section.get("options", []), "pick": section.get("pick")}
            for section in sorted(sections, key=lambda section: raw[section["key"]]["line"])
        ],
        "blocks": blocks,
        "facts": facts,
        "never_say": never,
        "context": context,
        **({"paths": paths, "default_path": default_path or next(iter(paths))} if paths else {}),
        **({"variants": variant_test(variants, when)} if variants else {}),
        **({"triggers": triggers} if triggers is not None else {}),
        **notes,
    }


HEADINGS_BY_KEY = {key: shape for key, _, shape in HEADINGS.values()}
SKIP = object()  # Lines under an unknown or repeated heading are reported once, at the heading.


@lru_cache(maxsize=16)
def restore_source_order(markdown):
    try:
        return parse_markdown(markdown)
    except ScriptError:
        return None  # Keep the saved parsed form if an older template no longer passes validation.


def section_markdown(template, key):
    """The Markdown body of one section, for script-change proposals."""
    lines, inside = [], False
    for line in template["markdown"].splitlines():
        heading = re.match(r"^#\s+(.+?)\s*#*\s*$", line)
        if heading:
            inside = (
                HEADINGS.get(
                    ALIASES.get(
                        " ".join(heading[1].casefold().split()), " ".join(heading[1].casefold().split())
                    ),
                    (None,),
                )[0]
                == key
            )
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
            inside = (
                HEADINGS.get(
                    ALIASES.get(
                        " ".join(heading[1].casefold().split()), " ".join(heading[1].casefold().split())
                    ),
                    (None,),
                )[0]
                == key
            )
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


def sections_for_path(sections, path):
    if not path:
        return sections
    result = []
    for section in sections:
        if section.get("spoken") is False:
            result.append(section)
            continue
        options = [o for o in section["options"] if not o.get("paths") or path in o["paths"]]
        if any(o.get("paths") for o in section["options"]):
            # Untagged options beside path options are shared: kept on every path, never picked or rewritten.
            alternatives = [o["id"] for o in options if o.get("paths")]
            options = [o if o.get("paths") else {**o, "shared": True} for o in options]
        else:
            alternatives = [o["id"] for o in options]
        if options:
            pick = section["pick"]
            if pick and len(alternatives) <= 1:
                pick = None
            elif pick:
                pick = [min(limit, len(alternatives)) for limit in pick]
            result.append({**section, "options": options, "pick": pick, "alternatives": alternatives})
    return result


def variant_test(variants, when):
    """The script test a master declares, with an identity that changes whenever its arms, shares or rule change."""
    test = {"order": list(variants), "arms": variants, "when": sorted(set(when))}
    test["id"] = hashlib.sha256(json.dumps(test, sort_keys=True).encode()).hexdigest()[:12]
    return test


def assign_variant(template, lead):
    """The variant a lead gets, by a stable draw on its company (or the lead): code, not the AI, decides."""
    test = template.get("variants")
    if not test:
        return None
    unit = lead.get("company_key") or lead.get("id") or lead.get("phone") or ""
    draw = int(hashlib.sha256(f"{test['id']}:{unit}".encode()).hexdigest()[:8], 16) % 10000
    total = 0
    for name in test["order"]:
        total += test["arms"][name]["share"] * 100
        if draw < total:
            return name
    return test["order"][-1]


def variant_in_test(template, lead):
    """Whether a lead counts in the comparison: every lead, or only leads whose checked trigger is listed in When."""
    test = template.get("variants") or {}
    if not test:
        return False
    when = test.get("when") or []
    return not when or ((lead.get("trigger") or {}).get("type") or "none") in when


def with_variant(template, name):
    """The master as one variant sees it: options tagged for other variants are left out."""
    if not template.get("variants") or not name or template.get("script_variant"):
        return template
    sections = []
    for section in template["sections"]:
        if section.get("spoken") is False or not section.get("options"):
            sections.append(section)
            continue
        options = [
            {k: v for k, v in o.items() if k != "variants"}
            for o in section["options"]
            if not o.get("variants") or name in o["variants"]
        ]
        if not options:
            continue
        pick = section.get("pick")
        if pick and len(options) <= 1:
            pick = None
        elif pick:
            pick = [min(limit, len(options)) for limit in pick]
        sections.append({**section, "options": options, "pick": pick})
    used = {o["id"] for s in sections for o in s.get("options", [])}
    blocks = [
        block
        for block in template.get("blocks", [])
        if block.get("option") in used or not block.get("option")
    ]
    return {
        **template,
        "sections": sections,
        "blocks": blocks,
        "script_variant": name,
        "master_version": template_version(template),
    }


def variant_fields(template, lead):
    """What a lead's script records about the test, for the call snapshot and the statistics export."""
    test = template.get("variants")
    if not test:
        return {}
    name = template.get("script_variant") or assign_variant(template, lead)
    return {
        "script_variant": name,
        "variant_test": test["id"],
        "variant_in_test": variant_in_test(template, lead),
        "variant_hypothesis": test["arms"].get(name, {}).get("hypothesis"),
    }


def pick_notes(template):
    """Plain sentences for the Script preview: what each lead's script keeps, picks or shares (work package 4.4)."""
    notes = []
    paths = template.get("paths") or {}
    if paths:
        default = template.get("default_path")
        names = ", ".join(f"{name} (default)" if name == default else name for name in paths)
        notes.append(f"Each lead gets one path: {names}. The AI writes only inside that path's options.")
    test = template.get("variants")
    if test:
        arms = ", ".join(f"{name} ({test['arms'][name]['share']}%)" for name in test["order"])
        scope = (
            f" Only leads with these triggers count: {', '.join(test['when'])}." if test.get("when") else ""
        )
        notes.append(
            f"Script test: {arms}. Each company gets one variant at random; its script keeps only that "
            f"variant's tagged options plus untagged ones.{scope}"
        )
    for section in template["sections"]:
        if section.get("spoken") is False or not section.get("options"):
            continue
        options = section["options"]
        title = section.get("title") or section["key"]
        if any(o.get("paths") for o in options):
            shared = [o.get("title") or "untitled text" for o in options if not o.get("paths")]
            if shared:
                notes.append(f"{title}: kept as written on every path: {', '.join(shared)}.")
        elif section.get("pick") and section["key"] != "objections":
            low, high = section["pick"]
            many = f"up to {high}" if high > 1 else "1"
            notes.append(
                f"{title}: the AI picks {many} of your {len(options)} per lead; the rest are left out."
            )
    return notes


def default_picks(template):
    picks = {}
    for section in template["sections"]:
        ids = section.get("alternatives") or [option["id"] for option in section["options"]]
        if section["key"] == "objections":
            picks[section["key"]] = ids
        elif section["pick"]:
            picks[section["key"]] = (
                ids[: section["pick"][1]] if section["key"] in {"discovery", "offers"} else ids[:1]
            )
    return picks


def fill_fields(text, values):
    return text.format_map(values)


def render(template, *, lead, seller, rep, picks=None, written=None, path=None):
    """The script a rep reads for one lead. Without picks or written text, the seller's defaults."""
    name = (lead.get("name") or "").strip()
    values = {
        "name": name or "there",
        "first_name": name.split()[0] if name and name.casefold() != "unknown" else "there",
        "company": lead.get("company") or "your company",
        "seller": seller,
        "rep": rep,
        # Kept as a token until the call is dialed and its caller ID is known.
        "callback_number": "{callback_number}",
    }
    path = path if path in template.get("paths", {}) else template.get("default_path")
    if template.get("variants") and not template.get("script_variant"):
        template = with_variant(template, assign_variant(template, lead))
    tested = variant_fields(template, lead)
    template = {**template, "sections": sections_for_path(template["sections"], path)}
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
        if section.get("spoken") is False:
            entry = {key: section[key] for key in ("key", "title", "text", "spoken")}
            if section["key"] in {"voicemail", "follow_up"}:
                try:
                    entry["text"] = fill_fields(entry["text"], values)
                except (KeyError, ValueError, IndexError):
                    pass  # Directions with stray braces stay as written.
            sections.append(entry)
            continue
        source_ids = [option["id"] for option in section["options"]]
        selected = set(chosen.get(section["key"]) or source_ids)
        order = [identity for identity in source_ids if identity in selected]
        if section["key"] in chosen:
            chosen[section["key"]] = order
        rank = {identity: index for index, identity in enumerate(order)}
        options = [
            {
                "id": option["id"],
                "title": option["title"],
                "text": re.sub(r"[ \t]{2,}", " ", text_of(option["text"])).strip(),
                **({"notes": fill_fields(option["notes"], values)} if option.get("notes") else {}),
                "picked": option["id"] in rank or bool(option.get("shared")),
                "rank": rank.get(option["id"]),
                **({"shared": True} if option.get("shared") else {}),
            }
            for option in section["options"]
            if complete(option) and (not path or option["id"] in rank or option.get("shared"))
        ]
        if options:
            sections.append(
                {
                    "key": section["key"],
                    "title": section["title"],
                    "options": options,
                    **({"spoken": section["spoken"]} if "spoken" in section else {}),
                }
            )

    def first(key, default=""):
        section = next((s for s in sections if s["key"] == key), None)
        picked = [o for o in section["options"] if o["picked"]] if section else []
        return (picked or (section["options"] if section else [{}]))[0].get("text", default)

    def picked(key):
        section = next((s for s in sections if s["key"] == key), None)
        return [o for o in section["options"] if o["picked"]] if section else []

    return {
        "sections": sections,
        **({"script_path": path} if path else {}),
        **tested,
        "picks": {key: list(value) for key, value in chosen.items()},
        "written": {key: dict(value) for key, value in written.items()},
        "opener": first("opening"),
        "screening_intro": first("screening", f"{rep} from {seller}, calling with a business introduction."),
        "context": template.get("context", {}),
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


def check_submission(template, picks, written, facts, row, *, required_blocks=None, path=None):
    """Close-out rules for one lead's script. Returns a list of plain errors the agent can fix.

    `facts` maps saved fact ids to statements for this lead; `row` is the lead's imported row,
    whose columns can be cited as `row:<column>`.
    """
    paths = template.get("paths", {})
    if paths and (not isinstance(path, str) or path not in paths):
        return [f"Choose one declared path: {', '.join(paths)}"]
    if path and not paths:
        return ["This master has no named paths"]
    template = {**template, "sections": sections_for_path(template["sections"], path)}
    errors = []
    sections = {section["key"]: section for section in template["sections"]}
    for key, chosen in (picks or {}).items():
        section = sections.get(key)
        if (
            section
            and section["pick"] is None
            and key != "objections"
            and chosen == (section.get("alternatives") or [o["id"] for o in section["options"]])
        ):
            continue  # Picking the only version of a section (or of a path's own option) changes nothing.
        if not section or section["pick"] is None and key != "objections":
            errors.append(f"'{key}' has no options to pick; leave it out")
            continue
        ids = section.get("alternatives") or [option["id"] for option in section["options"]]
        if not isinstance(chosen, list) or len(set(chosen)) != len(chosen):
            errors.append(f"Picks for '{key}' must be a list of distinct option ids")
            continue
        unknown = [identity for identity in chosen if identity not in ids]
        if unknown:
            errors.append(f"Unknown {key} option ids {unknown}; valid ids are {ids}")
        if key == "objections":
            if sorted(chosen) != sorted(ids):
                errors.append(f"Include every objection exactly once: {ids}")
            continue
        low, high = section["pick"]
        if not low <= len(chosen) <= high:
            errors.append(f"Pick {low} to {high} '{key}' options (you picked {len(chosen)})")
    for section in template["sections"]:
        if section["pick"] and section["key"] not in (picks or {}):
            errors.append(f"Pick from '{section['key']}': {section.get('alternatives') or []}")
    chosen = {**default_picks(template), **(picks or {})}
    needed = set()
    for section in template["sections"]:
        use = set(
            chosen.get(section["key"], section.get("alternatives") or [o["id"] for o in section["options"]])
        )
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
