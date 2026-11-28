"""Export local call paths alongside the script structures those calls used."""

import base64
import csv
import hashlib
import io
import json
from collections import Counter
from itertools import groupby
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import select

from app.calling.context import PERSON
from app.calling.state import TERMINAL
from app.platform.db import MODELS
from app.script_follow import sequence

CALLS = (
    "call_id",
    "rep_id",
    "started_at",
    "answered",
    "attempt",
    "outcome",
    "outcome_source",
    "test_call",
    "base_version",
    "script_version",
    "script_path",
    "structure_id",
    "transcript_status",
    "rep_segments",
    "matched_segments",
    "last_section_key",
    "last_option_id",
    "selected_options",
    # Call insights plan P7: the script test variant, its test and whether this call counts in the comparison.
    "script_variant",
    "variant_test",
    "variant_in_test",
)
EVENTS = (
    "call_id",
    "structure_id",
    "sequence",
    "section_key",
    "section_title",
    "option_id",
    "option_title",
    "audio_start_ms",
    "received_at",
    "match_score",
    "matcher_version",
)
STRUCTURE = (
    "structure_id",
    "section_order",
    "section_key",
    "section_title",
    "option_order",
    "option_id",
    "option_title",
    "spoken",
)
STATS = ("structure_id", "section_key", "section_title", "reached", "ended", "visits")


def structure_of(script):
    """Order and option identities, excluding per-lead wording and selections from the grouping key."""
    rows = []
    sections = [
        s for s in script.get("sections") or () if not (s.get("key") == "follow_up" and s.get("purpose"))
    ]
    for section_order, section in enumerate(sections, 1):
        for option_order, option in enumerate(section.get("options") or [{}], 1):
            rows.append(
                {
                    "section_order": section_order,
                    "section_key": section.get("key") or section["title"],
                    "section_title": section["title"],
                    "option_order": option_order if option else None,
                    "option_id": option.get("id"),
                    "option_title": option.get("title"),
                    "spoken": bool(option) and section.get("spoken", True),
                }
            )
    template = script.get("template_version")
    identity = hashlib.sha256(json.dumps([template, rows], sort_keys=True).encode()).hexdigest()[:20]
    return identity, rows


def csv_cell(value):
    # Section and option titles are user-authored. Export spreadsheet formulas as literal text.
    if isinstance(value, str) and (
        value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))
    ):
        return "'" + value
    return value


def csv_file(archive, filename, columns, rows):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(columns)
    writer.writerows([csv_cell(row.get(key)) for key in columns] for row in rows)
    archive.writestr(filename, buffer.getvalue())


def collect(service, p, *, include_test=False):
    """Every finished call's row, script events, the structures they used and per-stage counts."""
    calls, events, structures = [], [], {}
    reached, ended, visits = Counter(), Counter(), Counter()
    model = MODELS["call_attempts"]
    segments_model = MODELS["transcript_segments"]
    with service.db.snapshot() as s:
        records = s.scalars(
            select(model)
            .where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.op_status.in_(TERMINAL),
            )
            .order_by(model.op_at, model.id)
        )
        for record in records:
            data = record.data
            if not include_test and data.get("is_test"):
                continue
            script = data.get("script_snapshot") or {}
            identity, rows = structure_of(script)
            structures.setdefault(identity, rows)
            parts = {f"{row['section_key']}:{row['option_id']}": row for row in rows if row["spoken"]}
            segments = [
                row
                for row in sequence(
                    s.scalars(
                        select(segments_model.data).where(
                            segments_model.tenant_id == p["tenant_id"],
                            segments_model.seller_id == p["seller_id"],
                            segments_model.ctx_key == record.id,
                        )
                    )
                )
                if row["side"] == "rep" and row.get("word_count")
            ]
            matches = [row for row in segments if row.get("script_section") in parts]
            path = []
            for index, (key, consecutive) in enumerate(
                groupby(matches, key=lambda row: row["script_section"]), 1
            ):
                segment = next(consecutive)
                part = parts[key]
                path.append(part)
                events.append(
                    {
                        **part,
                        "call_id": record.id,
                        "structure_id": identity,
                        "sequence": index,
                        "audio_start_ms": segment.get("start_ms"),
                        "received_at": segment.get("server_received_at"),
                        "match_score": segment.get("section_score"),
                        "matcher_version": segment.get("script_match_version", 0),
                    }
                )
            last = path[-1] if path else {}
            human = data.get("outcome") in PERSON
            has_gap = bool(data.get("analysis_incomplete") or data.get("transcription_disabled"))
            calls.append(
                {
                    "call_id": record.id,
                    "structure_id": identity,
                    "rep_id": data.get("rep_id"),
                    "started_at": data.get("started_at"),
                    "attempt": data.get("attempt_ordinal"),
                    "answered": human,
                    "outcome": data.get("outcome") or data.get("carrier_outcome"),
                    "outcome_source": data.get("outcome_source"),
                    "test_call": bool(data.get("is_test")),
                    "base_version": script.get("base_version"),
                    "script_version": script.get("script_version"),
                    # Work package 5.3: the completed CSV's source master, path and generator, when supplied.
                    "script_provenance": script.get("provenance"),
                    "script_path": script.get("script_path"),
                    "transcript_status": "partial"
                    if has_gap and segments
                    else "missing"
                    if not segments
                    else "unmatched"
                    if not path
                    else "matched",
                    "last_section_key": last.get("section_key"),
                    "last_option_id": last.get("option_id"),
                    "selected_options": json.dumps(
                        [
                            option["id"]
                            for section in script.get("sections") or ()
                            for option in section.get("options") or ()
                            if option.get("picked", True)
                        ]
                    ),
                    "rep_segments": len(segments),
                    "matched_segments": len(matches),
                    "script_variant": script.get("script_variant"),
                    "variant_test": script.get("variant_test"),
                    "variant_in_test": bool(script.get("variant_in_test")),
                    # Not exported as such: the comparison reads these (plan P7).
                    "_stages": [key for key, _ in groupby(part["section_key"] for part in path)],
                    "_lead_id": data.get("lead_id"),
                    "_data": data,
                }
            )
            if human:
                stages = [key for key, _ in groupby(part["section_key"] for part in path)]
                reached.update((identity, key) for key in set(stages))
                visits.update((identity, key) for key in stages)
                if stages and not has_gap:
                    ended[identity, stages[-1]] += 1
    layout = [{**part, "structure_id": identity} for identity, rows in structures.items() for part in rows]
    stage_rows = []
    for (identity, key), same_stage in groupby(
        layout, key=lambda row: (row["structure_id"], row["section_key"])
    ):
        row = next(same_stage)
        stage_rows.append(
            {
                **row,
                "reached": reached[identity, key],
                "ended": ended[identity, key],
                "visits": visits[identity, key],
            }
        )
    return calls, events, layout, stage_rows


def export(service, p, *, include_test=False):
    calls, events, layout, stage_rows = collect(service, p, include_test=include_test)
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        csv_file(archive, "calls.csv", CALLS, calls)
        csv_file(archive, "script-events.csv", EVENTS, events)
        csv_file(archive, "script-structure.csv", STRUCTURE, layout)
        csv_file(archive, "stage-statistics.csv", STATS, stage_rows)
    return base64.b64encode(output.getvalue()).decode("ascii")
