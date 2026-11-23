"""Finish the usual script intake when the author supplied the complete wording in a CSV."""

import re

from app import ops
from app.leads.intake import is_script_column
from app.platform.db import doc, emit, put, uid, update, workspace
from app.preparation.portable import RESEARCH_COLUMN, parse_research, store_research_in
from app.preparation.templates import ScriptError, parse_markdown, render, template_version

# The completed-CSV format this app reads (work package 5.3). Older files without the column still work.
FORMAT = 1
# Optional provenance columns from the external generator: carried with the script, never read as script text.
META_COLUMNS = {
    "scriptformat": "format",
    "scriptmasterdigest": "master_digest",
    "scriptmasterversion": "master_version",
    "scriptpath": "path",
    "scriptgenerator": "generator",
}
MAX_META = 200
RESEARCH_KEY = re.sub(r"[\W_]+", "", RESEARCH_COLUMN)


def _meta(header):
    return META_COLUMNS.get(re.sub(r"[\W_]+", "", str(header).casefold()))


def _provenance(values):
    """Validated provenance, or a ScriptError that names the problem."""
    provenance = {}
    for name, value in values.items():
        text = str(value).strip()
        if len(text) > MAX_META:
            raise ScriptError([{"line": 0, "message": f"The script_{name} cell is too long"}])
        provenance[name] = text
    format_ = provenance.get("format")
    if format_ is not None:
        if not format_.isdigit():
            raise ScriptError([{"line": 0, "message": "script_format must be a whole number"}])
        if int(format_) > FORMAT:
            raise ScriptError(
                [
                    {
                        "line": 0,
                        "message": "A newer script generator made this row. Update OpenDialer to import it.",
                    }
                ]
            )
    return provenance


def parse_finished_script(text):
    markdown = text.strip()
    if not re.match(r"^#\s+", markdown):
        title = "Script" if re.search(r"(?m)^#\s+", markdown) else "Opening"
        markdown = f"# {title}\n{markdown}"
    template = parse_markdown(markdown, finished=True)
    if template["blocks"] or template.get("paths"):
        raise ScriptError([{"line": 0, "message": "Fill the script and choose its path before importing it"}])
    if not any(section["options"] for section in template["sections"]):
        raise ScriptError([{"line": 0, "message": "Include wording for the rep to say"}])
    return template


def split_script(raw):
    row, scripts, meta, research = {}, set(), {}, None
    for key, value in raw.items():
        if re.sub(r"[\W_]+", "", str(key).casefold()) == RESEARCH_KEY:
            # Research from the run that wrote the script (one-generator plan): stored beside it, never row data.
            if value is not None and str(value).strip():
                research = str(value)
        elif name := _meta(key):
            if value is not None and str(value).strip():
                meta[name] = value
        elif not is_script_column(key):
            row[key] = value
        elif value is not None:
            if not isinstance(value, str):
                raise ScriptError([{"line": 0, "message": "The script cell must contain text"}])
            if text := value.strip():
                scripts.add(text)
    if len(scripts) > 1:
        raise ScriptError([{"line": 0, "message": "This row has more than one different script"}])
    if not scripts:
        return row, None  # Provenance without a script has nothing to describe; the row prepares as usual.
    provenance = _provenance(meta)
    template = parse_finished_script(scripts.pop())
    if research is not None:
        template = {**template, "research": parse_research(research)}
    return row, {**template, "provenance": provenance} if provenance else template


def store_script_in(service, s, p, lead, template):
    now, version = service.clock(), uid()
    provenance, research = template.get("provenance"), template.get("research")
    template = {key: value for key, value in template.items() if key not in {"provenance", "research"}}
    picks = {
        section["key"]: [option["id"] for option in section["options"]]
        for section in template["sections"]
        if section["options"]
    }
    rendered = render(
        template,
        lead=doc(lead),
        seller=ops.required(service, s, "sellers", p["seller_id"], p).data["name"],
        rep=ops.required(service, s, "users", p["user_id"], p).data["name"],
        picks=picks,
    )
    rendered.update(
        lead_id=lead.id,
        source="csv",
        script_version=version,
        template_version=template_version(template),
        template={key: value for key, value in template.items() if key != "markdown"},
        fallback=False,
        ready_for_rep=True,
        fact_ids_used=[],
        claims=[],
        **({"provenance": provenance} if provenance else {}),
    )
    row = put(
        s,
        "script_versions",
        workspace(p),
        p["seller_id"],
        {
            "lead_id": lead.id,
            "source": "csv",
            "script_version": version,
            "template_version": rendered["template_version"],
            "rendered": rendered,
            "created_at": now.isoformat(),
        },
        now=now,
    )
    if research:
        store_research_in(service, s, p, lead, row, research)
    update(
        lead,
        preparation={"status": "ready", "source": "csv", "script_id": row.id, "as_of": now.isoformat()},
        preparation_lease=None,
        preparation_request_key=None,
        preparation_request_lists=[],
        status="eligible" if lead.data["status"] == "preparing" else lead.data["status"],
    )
    emit(
        s,
        workspace(p),
        "lead.updated",
        lead,
        now,
        {"script_status": "ready", "lead_status": lead.data["status"]},
    )
