"""Export calls for analysis anywhere (call insights plan phase 7).

One ZIP written to this computer's Exports folder: a calls spreadsheet with every dimension and number, one folder
per call with its audio (stereo Ogg Opus, rep left, prospect right) and transcript (times aligned to the audio), the
script events, structures and stage statistics, and a README explaining the columns. Only what each call kept is in
it; what was kept and why travels with every call. Filters narrow it by date, list, outcome, variant and more.
"""

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from sqlalchemy import select

from app import call_records, ops
from app.call_statistics import CALLS, EVENTS, STATS, STRUCTURE, collect, csv_cell
from app.calling.context import PERSON
from app.platform.db import MODELS
from app.scheduling import prime_time

METRICS = (
    "duration_ms",
    "rep_talk_ms",
    "prospect_talk_ms",
    "talk_share",
    "longest_rep_monologue_ms",
    "longest_prospect_monologue_ms",
    "median_response_ms",
    "rep_interruptions",
    "prospect_interruptions",
    "time_to_pitch_ms",
    "rep_words_per_minute",
    "rep_questions",
    "rep_pitch_range_semitones",
    "rep_loudness_dbfs",
    "rep_loudness_variation_db",
    "prospect_pitch_range_semitones",
)
LEAD_COLUMNS = ("lead_name", "company", "title", "phone", "state", "list_name", "trigger_type", "zone")
CALL_COLUMNS = (
    *CALLS,
    *LEAD_COLUMNS,
    "caller_id",
    "local_time",
    "local_band",
    "connected_seconds",
    "recording_reason",
    "recording_status",
    "rep_side_kept",
    "prospect_side_kept",
    "consent_at",
    "audio_file",
    "transcript_file",
    "metrics_source",
    *METRICS,
    "part_ms",
)
README = """OpenDialer call export
=======================

calls.csv               One row per finished call: outcome, script, variant, lead, local time band, what was kept
                        and why, and the call's numbers (talk share, monologues, response gap, pitch range ...).
calls/<call_id>/        audio.ogg (stereo Ogg Opus, 16 kHz: left = rep, right = prospect; silence where a side was
                        not kept), transcript.jsonl (one line per segment: side, start_ms, end_ms, text, script part;
                        times are milliseconds from the start of audio.ogg) and transcript.txt.
script-events.csv       Each script part a call reached, in order. script-structure.csv: the parts of each script
                        version. stage-statistics.csv: how many answered calls reached and ended in each part.

recording_reason        one_party (both sides kept), a state code such as CA (only the rep's side kept unless the
                        rep pressed R), toll_free, unknown or outside_us.
variant_in_test         True when the call counts in its script test's comparison.

This file contains prospects' names, numbers and, where kept, their voices. Handle it as personal data.
"""


def _filters(row, lead, options):
    data = row["_data"]
    started = data.get("started_at") or ""
    if options.get("since") and started < options["since"]:
        return False
    if options.get("until") and started > options["until"]:
        return False
    if options.get("list_name") and options["list_name"] not in ((lead or {}).get("list_names") or []):
        return False
    if options.get("outcome") and (data.get("outcome") or data.get("carrier_outcome")) != options["outcome"]:
        return False
    if options.get("variant") and row.get("script_variant") != options["variant"]:
        return False
    return not (options.get("conversations_only") and data.get("outcome") not in PERSON)


def _lead_rows(service, p, ids):
    model = MODELS["leads"]
    if not ids:
        return {}
    with service.db.transaction(False) as s:
        return {
            row.id: (row.data, row.op_zone)
            for row in s.scalars(
                select(model).where(model.tenant_id == p["tenant_id"], model.id.in_(list(ids)))
            )
        }


def _numbers(service, p, ids):
    model = MODELS["number_inventory"]
    with service.db.transaction(False) as s:
        return {
            row.id: row.data.get("phone")
            for row in s.scalars(select(model).where(model.tenant_id == p["tenant_id"]))
            if row.id in ids
        }


def _transcript(service, p, call_id):
    with service.db.transaction(False) as s:
        rows = [
            row
            for row in ops.transcript_rows(service, s, p, call_id)
            if row.get("is_final") and row.get("text") and not row.get("bleed")
        ]
    return sorted(rows, key=lambda r: (r.get("start_ms") is None, r.get("start_ms") or 0, r["segment_id"]))


def export(service, p, options):
    """Write the bundle; returns its path and what it holds."""
    calls, events, layout, stage_rows = collect(service, p, include_test=bool(options.get("include_test")))
    leads = _lead_rows(service, p, {row["_lead_id"] for row in calls if row["_lead_id"]})
    numbers = _numbers(service, p, {row["_data"].get("number_id") for row in calls})
    chosen = [
        row
        for row in calls
        if (options.get("include_test") or not row["test_call"])
        and _filters(row, leads.get(row["_lead_id"], (None, None))[0], options)
    ]
    folder = Path(service.settings.data_dir) / "Exports"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = service.clock().strftime("%Y%m%d-%H%M%S")
    path = folder / f"opendialer-calls-{stamp}.zip"
    written = {"calls": 0, "audio": 0, "transcripts": 0}
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", README)
        table = []
        for row in chosen:
            data = row["_data"]
            lead, zone = leads.get(row["_lead_id"], ({}, None))
            lead = lead or {}
            rules = data.get("recording") or {}
            metrics = data.get("metrics") or {}
            entry = {k: v for k, v in row.items() if not k.startswith("_")}
            started = data.get("submitted_at") or data.get("started_at")
            zones = prime_time.zones_of(zone)
            local = band = None
            if started and len(zones) == 1:
                moment = datetime.fromisoformat(started).astimezone(prime_time.ZoneInfo(zones[0]))
                local = moment.strftime("%Y-%m-%d %H:%M")
                band = (
                    prime_time.label(prime_time.default_band(moment))
                    if prime_time.slot(moment) is not None
                    else "outside"
                )
            base = f"calls/{row['call_id']}"
            audio_file = transcript_file = None
            if options.get("include_audio", True):
                audio = call_records.audio_ogg(service, data)
                if audio:
                    archive.writestr(f"{base}/audio.ogg", audio, compress_type=ZIP_STORED)
                    audio_file = f"{base}/audio.ogg"
                    written["audio"] += 1
            lines = _transcript(service, p, row["call_id"])
            if lines:
                archive.writestr(
                    f"{base}/transcript.jsonl",
                    "".join(
                        json.dumps(
                            {
                                k: line.get(k)
                                for k in ("side", "start_ms", "end_ms", "text", "script_section")
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                        for line in lines
                    ),
                )
                archive.writestr(
                    f"{base}/transcript.txt",
                    "".join(
                        f"[{(line.get('start_ms') or 0) / 1000:7.1f}s] "
                        f"{'Rep' if line['side'] == 'rep' else 'Prospect'}: {line['text']}\n"
                        for line in lines
                    ),
                )
                transcript_file = f"{base}/transcript.jsonl"
                written["transcripts"] += 1
            entry.update(
                lead_name=lead.get("name"),
                company=lead.get("company"),
                title=(lead.get("profile") or {}).get("title"),
                phone=lead.get("phone"),
                state=lead.get("state"),
                list_name=((lead.get("list_names") or [None])[0]),
                trigger_type=(lead.get("trigger") or {}).get("type"),
                zone=zone,
                caller_id=numbers.get(data.get("number_id")),
                local_time=local,
                local_band=band,
                connected_seconds=data.get("connected_seconds"),
                recording_reason=rules.get("reason"),
                recording_status=rules.get("status"),
                rep_side_kept=bool(rules.get("rep")),
                prospect_side_kept=bool(rules.get("prospect") or rules.get("consent_at")),
                consent_at=rules.get("consent_at"),
                audio_file=audio_file,
                transcript_file=transcript_file,
                metrics_source=metrics.get("source"),
                part_ms=json.dumps(metrics.get("part_ms") or {}),
                **{key: metrics.get(key) for key in METRICS},
            )
            table.append(entry)
        written["calls"] = len(table)
        ids = {row["call_id"] for row in chosen}
        for name, columns, rows in (
            ("calls.csv", CALL_COLUMNS, table),
            ("script-events.csv", EVENTS, [e for e in events if e["call_id"] in ids]),
            ("script-structure.csv", STRUCTURE, layout),
            ("stage-statistics.csv", STATS, stage_rows),
        ):
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer)
            writer.writerow(columns)
            writer.writerows([csv_cell(item.get(key)) for key in columns] for item in rows)
            archive.writestr(name, buffer.getvalue())
    return {"path": str(path), "bytes": path.stat().st_size, **written}
