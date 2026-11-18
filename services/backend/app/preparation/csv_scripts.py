"""Finish the usual script intake when the author supplied the complete wording in a CSV."""

import re

from app import ops
from app.leads.intake import is_script_column
from app.platform.db import doc, emit, put, uid, update, workspace
from app.preparation.templates import ScriptError, parse_markdown, render, template_version


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
    row, scripts = {}, set()
    for key, value in raw.items():
        if not is_script_column(key):
            row[key] = value
        elif value is not None:
            if not isinstance(value, str):
                raise ScriptError([{"line": 0, "message": "The script cell must contain text"}])
            if text := value.strip():
                scripts.add(text)
    if len(scripts) > 1:
        raise ScriptError([{"line": 0, "message": "This row has more than one different script"}])
    return row, parse_finished_script(scripts.pop()) if scripts else None


def store_script_in(service, s, p, lead, template):
    now, version = service.clock(), uid()
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
