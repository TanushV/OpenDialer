"""Finished scripts that travel between workspaces (docs/ONE_GENERATOR_PLAN.md).

The app's own preparation writes each lead's script; this module exports it with `{rep}` and `{seller}` left for the
receiving rep, plus the research behind it, and checks that research when another workspace imports it.
"""

import json
import re
from datetime import date
from urllib.parse import urlsplit

from sqlalchemy import select

from app.lead_lists import memberships
from app.platform.db import MODELS, doc
from app.preparation.templates import ScriptError, render, template_version

FORMAT = "1"  # Completed-CSV format (csv_scripts.FORMAT); research columns are optional within it.
RESEARCH_COLUMN = "script_research"
COLUMNS = (
    "script",
    "script_format",
    "script_path",
    "script_master_digest",
    "script_generator",
    "script_variant",
    "script_variant_test",
    RESEARCH_COLUMN,
)
# Generation used these to choose the path and write the blocks; a finished script does not repeat them.
PREP_ONLY = {"preparation", "triggers"}
MAX_RESEARCH_CHARS = 40000
MAX_FACTS = 32
STRENGTHS = {"A", "B", "C", "none"}


def finished_markdown(rendered):
    """One rendered lead script as Markdown that parses as a finished script."""
    lines = []
    for section in rendered["sections"]:
        if section["key"] in PREP_ONLY:
            continue
        lines.extend([f"# {section['title']}", ""])
        if section.get("spoken") is False:
            text = section["text"]
            if section["key"].startswith("custom:"):
                # A notes-only custom section renders without its "> ", which would make it spoken on import.
                text = "\n".join("> " + line if line.strip() else line for line in text.splitlines())
            lines.extend([text, ""])
            continue
        picked = [option for option in section["options"] if option["picked"]]
        notes = [option.get("notes", "").splitlines() for option in picked]
        shared_notes = []
        if len(picked) > 1:
            for note_lines in zip(*notes, strict=False):  # Stop at the shortest: only a shared prefix.
                if len(set(note_lines)) != 1:
                    break
                shared_notes.append(note_lines[0])
        lines.extend("> " + line for line in shared_notes)
        for option, option_notes in zip(picked, notes, strict=True):
            if title := option["title"]:
                lines.append(f"## {title}")
            body = option["text"].splitlines()
            if not title and len(picked) > 1:
                body = ["- " + body[0], *("  " + line for line in body[1:])]
            lines.extend(body)
            prefix = "  > " if not title and len(picked) > 1 else "> "
            lines.extend(prefix + line for line in option_notes[len(shared_notes) :])
            lines.append("")
    return "\n".join(lines).strip()


def research_record(script, facts):
    """The research behind one prepared script, as the importing workspace will store it."""
    bridge = script.get("bridge")
    return {
        "summary": (script.get("summary") or "")[:600],
        "trigger": script.get("trigger"),
        "bridge": (bridge or {}).get("text") if isinstance(bridge, dict) else bridge,
        "facts": [
            {
                "statement": fact["statement"],
                "quote": fact.get("evidence_quote") or "",
                "source_url": fact["source_url"],
            }
            for fact in facts[:MAX_FACTS]
        ],
        "route": script.get("route"),
    }


def export_list(service, p, list_name):
    """Every lead in one list: its ready script with portable tokens and research, or why it is not ready."""
    from app.preparation.csv_scripts import parse_finished_script
    from app.preparation.workflows import (
        lead_script_base,
        preparation_state,
        script_evidence,
        script_row,
    )

    model, rows, not_ready = MODELS["leads"], [], []
    generator = f"opendialer {service.settings.version}"
    with service.db.transaction(False) as s:
        query = select(model).where(model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"])
        leads = [
            lead
            for lead in s.scalars(query.order_by(model.created_at, model.id))
            if list_name in memberships(lead.data) and not lead.data.get("test_list_contact")
        ]
        for lead in leads:
            state = preparation_state(service, s, p, lead)
            row = script_row(service, s, p, lead)
            base = lead_script_base(service, s, p, lead)
            template = base and base.get("template")
            reason = None
            if state["status"] != "ready" or row is None:
                reason = state.get("error") or state["status"]
            elif row.data.get("source") == "csv":
                reason = "imported with a finished script"
            elif not template or row.data.get("template_version") != template_version(template):
                reason = "stale: the master changed"
            if reason:
                not_ready.append(
                    {
                        "lead_id": lead.id,
                        "name": lead.data.get("name"),
                        "phone": lead.data["phone"],
                        "reason": reason,
                        "status": state["status"],
                        "attempts": state.get("attempts", 0),
                        "next_attempt_at": state.get("next_attempt_at"),
                    }
                )
                continue
            data = row.data
            rendered = render(
                template,
                lead=doc(lead),
                seller="{seller}",
                rep="{rep}",
                picks=data.get("picks"),
                written=data.get("written"),
                path=data.get("path"),
            )
            markdown = finished_markdown(rendered)
            parse_finished_script(markdown)  # What this file says is importable must import.
            facts = script_evidence(service, s, p, lead, data)
            rows.append(
                {
                    "lead_id": lead.id,
                    "name": lead.data.get("name"),
                    "phone": lead.data["phone"],
                    "csv": lead.data.get("csv_values") or lead.data.get("row") or {},
                    "script": markdown,
                    "script_format": FORMAT,
                    "script_path": data.get("path") or "",
                    "script_master_digest": data.get("template_version") or "",
                    "script_generator": generator,
                    "script_variant": data.get("script_variant") or rendered.get("script_variant") or "",
                    "script_variant_test": (
                        (data.get("variant_test") or rendered.get("variant_test") or "")
                        if (
                            data.get("variant_in_test")
                            if "variant_in_test" in data
                            else rendered.get("variant_in_test")
                        )
                        else ""
                    ),
                    RESEARCH_COLUMN: json.dumps(research_record(data, facts), ensure_ascii=False),
                    # AI lines the independent review rejected are left out of the script; the author should know.
                    "dropped_by_review": data.get("dropped_by_review") or [],
                    "dropped_text": data.get("dropped_text") or {},
                }
            )
    return {"columns": list(COLUMNS), "rows": rows, "ready": len(rows), "not_ready": not_ready}


def _web_url(value):
    parsed = urlsplit(str(value or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _text(value, limit, name, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        raise ValueError(f"{name} must be text of at most {limit} characters")
    return value.strip()


def parse_research(cell):
    """The research cell of a finished-script row, checked and bounded, or a ScriptError naming the problem."""
    try:
        if len(cell) > MAX_RESEARCH_CHARS:
            raise ValueError(f"keep it under {MAX_RESEARCH_CHARS} characters")
        value = json.loads(cell)
        if not isinstance(value, dict):
            raise ValueError("it must be a JSON object")
        facts = value.get("facts") or []
        if not isinstance(facts, list) or len(facts) > MAX_FACTS:
            raise ValueError(f"facts must be a list of at most {MAX_FACTS}")
        checked = []
        for fact in facts:
            if not isinstance(fact, dict) or not _web_url(fact.get("source_url")):
                raise ValueError("each fact needs an http(s) source_url")
            checked.append(
                {
                    "statement": _text(fact.get("statement"), 300, "statement", required=True),
                    "quote": _text(fact.get("quote") or "", 500, "quote"),
                    "source_url": fact["source_url"].strip()[:2000],
                }
            )
        trigger = value.get("trigger")
        if trigger is not None:
            if not isinstance(trigger, dict) or not re.fullmatch(
                r"[a-z][a-z0-9_]{0,39}", str(trigger.get("type"))
            ):
                raise ValueError("trigger needs a type")
            if trigger.get("source_url") and not _web_url(trigger["source_url"]):
                raise ValueError("trigger.source_url must be http(s)")
            if trigger.get("date"):
                date.fromisoformat(str(trigger["date"]))
            if trigger.get("strength") not in STRENGTHS | {None}:
                raise ValueError("trigger.strength must be A, B, C or none")
            if trigger.get("evidence") not in {"page", "row_only", None}:
                raise ValueError("trigger.evidence must be page or row_only")
            trigger = {
                "type": trigger["type"],
                "date": trigger.get("date"),
                "source_url": trigger.get("source_url"),
                "quote": _text(trigger.get("quote"), 500, "trigger.quote"),
                "evidence": trigger.get("evidence"),
                "strength": trigger.get("strength") or "none",
                "from_row": bool(trigger.get("from_row")),
            }
        bridge = value.get("bridge")
        if isinstance(bridge, dict):
            bridge = bridge.get("text")
        return {
            "summary": _text(value.get("summary") or "", 600, "summary"),
            "trigger": trigger,
            "bridge": _text(bridge, 300, "bridge"),
            "facts": checked,
            "route": _text(value.get("route"), 40, "route"),
        }
    except (ValueError, TypeError) as exc:
        raise ScriptError(
            [{"line": 0, "message": f"The {RESEARCH_COLUMN} cell is not usable: {exc}"}]
        ) from None


def store_research_in(service, s, p, lead, version, research):
    """Store imported research beside an imported script, labelled as from the run that wrote it."""
    from datetime import timedelta

    from app.platform.db import emit, put, uid, update
    from app.preparation.workflows import STABLE_DAYS

    now = service.clock()
    fact_ids = []
    for fact in research["facts"]:
        source = put(
            s,
            "research_sources",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead.id,
                "url": fact["source_url"],
                "title": "",
                "retrieved_at": now.isoformat(),
                "text": "",
                "imported": True,
                "synthetic": service.settings.simulation,
            },
            now=now,
        )
        fact_id = uid()
        put(
            s,
            "research_facts",
            p["tenant_id"],
            p["seller_id"],
            {
                "fact_id": fact_id,
                "lead_id": lead.id,
                "statement": fact["statement"],
                "source_id": source.id,
                "source_url": fact["source_url"],
                "evidence_quote": fact["quote"],
                "status": "supported",
                "stability": "stable",
                "expires_at": (now + timedelta(days=STABLE_DAYS)).isoformat(),
                "grounding": {"status": "imported"},
                "synthetic": service.settings.simulation,
            },
            object_id=fact_id,
            now=now,
        )
        fact_ids.append(fact_id)
    trigger = research["trigger"] and {**research["trigger"], "imported": True}
    bridge = {"text": research["bridge"], "is_inference": True} if research["bridge"] else None
    update(
        version,
        fact_ids_used=sorted(fact_ids),
        summary=research["summary"],
        trigger=trigger,
        bridge=bridge,
        research_route="imported",
    )
    update(lead, trigger=trigger, bridge=bridge, research_route="imported")
    emit(s, p["tenant_id"], "lead.updated", lead, now)
