"""How prospects react to each part of a campaign's script, and what that says about closing.

Pure functions over persisted metadata (segment timing, word counts, matched script sections and
reviewed observations). Raw speech is never needed after it expires, and nothing here stores it.
"""

import hashlib
import math
import re
from collections import Counter, defaultdict
from copy import deepcopy

from app.optimization.conversations import association_patterns, first_eligible, supported

SECTIONS = ("opener", "gatekeeper", "screening_intro", "discovery_questions", "proof", "closing_question")
EDITABLE = ("opener", "gatekeeper", "discovery_questions", "closing_question")
LOCKED = {
    "screening_intro": "The screening line names the seller and stays as the seller approved it.",
    "proof": "Proof is filled only from reviewed research facts.",
    "opener": "The opener keeps who is calling: {rep} and {seller}.",
}
# Timing depends on transcription timestamps and is used after the call only, never live.
TIMING_FEATURES = frozenset(
    {"rep_talk_ratio", "longest_monologue", "time_to_first_question", "opening_length"}
)
BANNED = ("guarantee", "risk-free", "risk free", "we spoke before", "double your revenue", "promise")
MATCH_THRESHOLD = 0.6
ENGAGED_WORDS = 12
NAMES = {
    "opener": "opener",
    "gatekeeper": "gatekeeper line",
    "screening_intro": "screening line",
    "discovery_questions": "discovery question",
    "proof": "proof line",
    "closing_question": "closing question",
}


def tokens(text):
    return re.findall(r"[a-z0-9']+", (text or "").casefold())


def variant(text):
    """Stable identity of one section's seller-authored wording."""
    return hashlib.sha256((text or "").encode()).hexdigest()[:12]


def section_texts(script):
    """(key, rendered text, template text) for every section a rep may say."""
    template = script.get("template") or {}
    result = []
    for field in SECTIONS:
        value, raw = script.get(field), template.get(field)
        if isinstance(value, list):
            for i, text in enumerate(value):
                source = raw[i] if isinstance(raw, list) and i < len(raw) else text
                result.append((f"{field}:{i}", text, source))
        elif isinstance(value, str) and value.strip():
            result.append((field, value, raw if isinstance(raw, str) else value))
    for i, section in enumerate(script.get("sections", [])):
        raw_sections = template.get("sections", [])
        raw = raw_sections[i]["text"] if i < len(raw_sections) else section["text"]
        result.append((f"sections:{i}", section["text"], raw))
    return result


def section_label(key):
    field, _, index = key.partition(":")
    name = NAMES.get(field, field)
    return f"{name} {int(index) + 1}" if index else name


def match_section(text, script):
    """The script section a rep line delivers: most of the section's words, said in one line."""
    words, best = set(tokens(text)), (None, 0.0)
    for key, rendered, _ in section_texts(script):
        wanted = set(tokens(rendered))
        if len(wanted) < 3:
            continue
        score = len(wanted & words) / len(wanted)
        if score > best[1]:
            best = (key, score)
    return best if best[1] >= MATCH_THRESHOLD else (None, round(best[1], 3))


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def script_trace(script, segments, observations=()):
    """Delivered sections in order, the prospect's reaction to each, the opening and call shape."""
    from app.postcall.analysis import behavioral_metrics

    rows = [
        s
        for s in segments
        if s.get("is_final") and not s.get("system_audio") and s.get("side") in {"rep", "remote"}
    ]
    timed = bool(rows) and all(
        _number(s.get("start_ms")) and _number(s.get("end_ms")) and s["end_ms"] >= s["start_ms"] for s in rows
    )
    order = sorted(
        rows,
        key=lambda s: (s["start_ms"], s["end_ms"]) if timed else (s.get("server_received_at") or "", 0),
    )
    variants = {key: variant(source) for key, _, source in section_texts(script)}
    reviewed = [
        o
        for o in observations
        if o.get("state") not in {"stale", "retracted"}
        and o.get("epistemic_status") in {"model_reviewed", "human_confirmed"}
    ]

    def sourced(kinds):
        return {
            ref.get("id")
            for o in reviewed
            if o.get("kind") in kinds
            for ref in o.get("sources", [])
            if ref.get("kind") == "segment"
        }

    objections = sourced({"objection"})
    interest = sourced({"need", "impact", "answer", "signal", "commitment"})
    delivered, seen = [], set()
    for position, segment in enumerate(order):
        key = segment.get("script_section")
        if segment["side"] == "rep" and key in variants and key not in seen:
            seen.add(key)
            delivered.append((position, key, segment))
    first_ms = order[0]["start_ms"] if timed else None
    end_ms = max(s["end_ms"] for s in order) if timed else None
    sections = []
    for i, (position, key, segment) in enumerate(delivered):
        stop = delivered[i + 1][0] if i + 1 < len(delivered) else len(order)
        window = [s for s in order[position + 1 : stop] if s["side"] == "remote"]
        words = sum(int(s.get("word_count") or 0) for s in window)
        questions = sum(int(s.get("question_count") or 0) for s in window)
        ids = {s.get("segment_id") for s in window}
        if ids & objections:
            reaction = "objection"
        elif words >= ENGAGED_WORDS or questions or ids & interest:
            reaction = "engaged"
        elif i == len(delivered) - 1 and words < 4:
            reaction = "ended"
        else:
            reaction = "brief" if words else "silent"
        start = segment["start_ms"] if timed else None
        following = delivered[i + 1][2]["start_ms"] if timed and i + 1 < len(delivered) else end_ms
        sections.append(
            {
                "key": key,
                "variant": variants[key],
                "order": i,
                "start_ms": start - first_ms if timed else None,
                "portion_ms": following - start if timed else None,
                "reaction": reaction,
                "prospect_words": words,
                "prospect_questions": questions,
            }
        )
    opening = None
    if delivered:
        position, key, segment = delivered[0]
        reply = next((s for s in order[position + 1 :] if s["side"] == "remote"), None)
        opening = {
            "key": key,
            "variant": variants[key],
            "reaction": sections[0]["reaction"],
            "first_response_ms": reply["start_ms"] - segment["start_ms"] if timed and reply else None,
        }
    metrics = behavioral_metrics(order)
    question = next((s for s in order if s["side"] == "rep" and (s.get("question_count") or 0) > 0), None)
    shape = {
        "timing": "measured" if timed else "order_only",
        "duration_ms": end_ms - first_ms if timed else None,
        "rep_talk_ratio": metrics["rep_talk_ratio"] if timed else None,
        "longest_monologue_ms": metrics["longest_monologue_ms"] if timed else None,
        "question_count": metrics["question_count"],
        "turn_exchanges": metrics["turn_exchanges"],
        "time_to_first_question_ms": question["start_ms"] - first_ms if timed and question else None,
        "sections_delivered": len(sections),
        "discovery_questions_asked": sum(s["key"].startswith("discovery_questions") for s in sections),
    }
    return {
        "version": 1,
        "base_version": script.get("base_version"),
        "arm": script.get("script_arm"),
        "proposal_id": script.get("proposal_id"),
        "sections": sections,
        "opening": opening,
        "shape": shape,
    }


def _bucket(value, edges, labels):
    for edge, label in zip(edges, labels, strict=False):
        if value < edge:
            return label
    return labels[-1]


def trace_features(trace):
    """Categorical (feature, value) keys of one call, for the association estimator."""
    keys = set()
    for section in trace.get("sections", []):
        keys.add(("section:" + section["key"], section["variant"]))
        keys.add(("reaction:" + section["key"], section["reaction"]))
    if not keys:
        return keys
    shape = trace["shape"]
    asked = shape["discovery_questions_asked"]
    keys.add(("discovery_questions", str(asked) if asked < 3 else "3+"))
    if shape["timing"] == "measured":
        ratio = shape["rep_talk_ratio"]
        if ratio is not None:
            keys.add(
                ("rep_talk_ratio", _bucket(ratio, (0.35, 0.5, 0.65), ("<35%", "35-50%", "50-65%", ">65%")))
            )
        monologue = shape["longest_monologue_ms"]
        if monologue is not None:
            keys.add(("longest_monologue", _bucket(monologue, (30000, 60000), ("<30s", "30-60s", ">60s"))))
        first = shape["time_to_first_question_ms"]
        keys.add(
            (
                "time_to_first_question",
                "none" if first is None else _bucket(first, (20000, 45000), ("<20s", "20-45s", ">45s")),
            )
        )
        opener = next((s for s in trace["sections"] if s["key"] == "opener"), None)
        if opener and opener["portion_ms"] is not None:
            keys.add(
                ("opening_length", _bucket(opener["portion_ms"], (10000, 20000), ("<10s", "10-20s", ">20s")))
            )
    return keys


def feature_label(feature, value, texts):
    if feature.startswith("section:"):
        key = feature.split(":", 1)[1]
        text = texts.get(value)
        return (
            f"{section_label(key).capitalize()} “{text}”"
            if text
            else f"{section_label(key).capitalize()} ({value})"
        )
    if feature.startswith("reaction:"):
        return f"Prospect {value} after the {section_label(feature.split(':', 1)[1])}"
    return {
        "discovery_questions": f"Discovery questions asked: {value}",
        "rep_talk_ratio": f"Rep talk ratio {value}",
        "longest_monologue": f"Longest rep monologue {value}",
        "time_to_first_question": "No rep question" if value == "none" else f"First rep question {value} in",
        "opening_length": f"Opening {value}",
    }.get(feature, f"{feature} {value}")


def script_report(rows, *, as_of, texts=None, min_support=10):
    """Section reactions and section/call-shape associations with the campaign's close."""
    texts = texts or {}

    def keys_of(row, at, available):
        trace = row.get("script_trace") or {}
        return trace_features(trace) if trace.get("sections") else set()

    eligible, ordered = first_eligible(rows, keys_of, as_of=as_of)
    found = association_patterns(eligible, min_support=min_support)
    patterns = []
    for item in found["patterns"]:
        feature, value = item["key"]
        status = (
            "supported_correlation"
            if supported(item, min_support)
            else "supported_negative"
            if supported(item, min_support, direction=-1)
            else "descriptive_only"
        )
        patterns.append(
            {
                "feature": feature,
                "value": value,
                "label": feature_label(feature, value, texts),
                "live": feature not in TIMING_FEATURES,
                **{k: item[k] for k in ("samples", "conversions", "conversion_rate", "conversion_low")},
                "conversion_high": item["conversion_high"],
                "difference": item["validation"]["difference"],
                "discovery": item["discovery"],
                "validation": item["validation"],
                "status": status,
            }
        )
    # Reactions are known right after a call; close rates use only matured labels.
    traced, leads = [], set()
    for row in ordered:
        trace = row.get("script_trace") or {}
        if trace.get("sections") and row.get("lead_id") not in leads:
            leads.add(row.get("lead_id"))
            traced.append(row)
    sections = defaultdict(
        lambda: {
            "delivered": 0,
            "reactions": Counter(),
            "labeled": Counter(),
            "closed": Counter(),
            "variants": Counter(),
        }
    )
    for row in traced:
        labeled = row.get("conversion") in (0, 1) and row.get("window_closed") is not False
        for section in row["script_trace"]["sections"]:
            entry = sections[section["key"]]
            entry["delivered"] += 1
            entry["reactions"][section["reaction"]] += 1
            entry["variants"][section["variant"]] += 1
            if labeled:
                entry["labeled"][section["reaction"]] += 1
                entry["closed"][section["reaction"]] += row["conversion"]
    order = {key: i for i, key in enumerate(SECTIONS)}
    summary = [
        {
            "key": key,
            "label": section_label(key),
            "delivered": entry["delivered"],
            "reactions": dict(entry["reactions"]),
            "close_after": {r: entry["closed"][r] / n for r, n in entry["labeled"].items() if n},
            "variants": [
                {"variant": v, "text": texts.get(v), "delivered": n}
                for v, n in entry["variants"].most_common()
            ],
            "engaged_rate": entry["reactions"]["engaged"] / entry["delivered"],
        }
        for key, entry in sorted(sections.items(), key=lambda kv: (order.get(kv[0].split(":")[0], 99), kv[0]))
    ]
    return {
        "status": "ready",
        "as_of": as_of.isoformat(),
        "evidence_type": "observational_correlation",
        "unit": "one_first_eligible_conversation_per_lead_or_endpoint",
        "samples": len(eligible),
        "traced_leads": len(traced),
        "input_rows": len(ordered),
        "discovery_samples": found["discovery_samples"],
        "validation_samples": found["validation_samples"],
        "validation_starts_at": found["validation_starts_at"],
        "min_support": min_support,
        "patterns": patterns,
        "sections": summary,
    }


def weakest_section(report, *, minimum=10):
    """The editable section delivered to enough leads with the lowest engagement after it."""
    candidates = [
        s
        for s in report.get("sections", [])
        if s["delivered"] >= minimum and s["key"].split(":")[0] in EDITABLE
    ]
    return min(
        candidates, key=lambda s: (s["engaged_rate"], SECTIONS.index(s["key"].split(":")[0])), default=None
    )


def candidate_template(base, section, text, *, index=None, source="team"):
    """The base template with one editable section changed, within the identity and claim locks."""
    from app.preparation.templates import ScriptTemplate

    if not base:
        raise ValueError("Save a base script template before proposing changes")
    if section not in EDITABLE:
        raise ValueError(
            LOCKED.get(
                section,
                "Only the opener, gatekeeper line, discovery questions and closing question can change",
            )
        )
    text = (text or "").strip()
    lowered = text.casefold()
    if not 3 <= len(text) <= 600:
        raise ValueError("Write between 3 and 600 characters")
    if any(term in lowered for term in BANNED):
        raise ValueError("No guarantees, promises or invented relationships")
    if source == "memory" and re.search(r"[0-9%$£€]", text):
        raise ValueError("A generated change cannot add numbers, prices or other claims")
    if section == "opener" and not ("{rep}" in text and "{seller}" in text):
        raise ValueError(LOCKED["opener"])
    template = deepcopy(base)
    if section == "discovery_questions":
        questions = list(template["discovery_questions"])
        if index is None or not 0 <= index <= len(questions) or index >= 5:
            raise ValueError("Choose an existing discovery question or the next free slot (at most five)")
        if index == len(questions):
            questions.append(text)
        else:
            questions[index] = text
        template["discovery_questions"] = questions
    else:
        template[section] = text
    for uploaded in template.get("sections", []):
        if uploaded["text"] == base.get(section):
            uploaded["text"] = text
    if template == base:
        raise ValueError("The proposal does not change the base script")
    return ScriptTemplate(**template).model_dump()


def memory_digest(report, notes, *, live_only=True, limit=5):
    """The bounded part of the memory that script writing and live help may use."""
    eligible = [p for p in report.get("patterns", []) if p["live"] or not live_only]
    working = [p for p in eligible if p["status"] == "supported_correlation"][:limit]
    weak = [p for p in eligible if p["status"] == "supported_negative"][:limit]
    keep = ("feature", "value", "label", "samples", "conversion_rate", "difference")
    return {
        "as_of": report.get("as_of"),
        "working": [{k: p[k] for k in keep} for p in working],
        "not_working": [{k: p[k] for k in keep} for p in weak],
        "team_notes": notes or "",
        "evidence": "Observational correlations within this campaign; not facts about any prospect.",
    }


LEARNED_START, LEARNED_END = "<!-- learned:start -->", "<!-- learned:end -->"


def _pct(value):
    return "n/a" if value is None else f"{round(value * 100)}%"


def _finding(p):
    diff = p["difference"]
    versus = (
        f"{'+' if diff >= 0 else ''}{round(diff * 100)} points vs others"
        if diff is not None
        else "no comparison"
    )
    return (
        f"- **{p['label']}**: closed {_pct(p['conversion_rate'])} of {p['samples']} conversations "
        f"(interval {_pct(p['conversion_low'])}-{_pct(p['conversion_high'])}); {versus}, confirmed in a later period."
    )


def learned_markdown(*, report, tests, history, texts):
    """The machine-maintained body of the memory; no prospect words, only script text and numbers."""
    patterns = report.get("patterns", [])
    script = [p for p in patterns if p["live"]]
    timing = [p for p in patterns if not p["live"]]
    lines = ["## What is working"]
    good = [p for p in script if p["status"] == "supported_correlation"]
    lines += [_finding(p) for p in good] or ["- Nothing confirmed yet."]
    lines += ["", "## What is not working"]
    bad = [p for p in script if p["status"] == "supported_negative"]
    lines += [_finding(p) for p in bad] or ["- Nothing confirmed yet."]
    lines += ["", "## Too early to tell"]
    early = [p for p in script if p["status"] == "descriptive_only"]
    if report.get("samples", 0) < report.get("min_support", 10) * 2:
        lines.append(
            f"- Not enough calls yet: {report.get('samples', 0)} conversations analyzed; a pattern needs at least "
            f"{report.get('min_support', 10)} separate leads on each side and a later period to confirm it."
        )
    lines += [
        f"- {p['label']}: closed {_pct(p['conversion_rate'])} of {p['samples']} (not yet confirmed)."
        for p in early[:8]
    ]
    lines += ["", "## How prospects react, by section"]
    for s in report.get("sections", []):
        mix = ", ".join(
            f"{r} {_pct(n / s['delivered'])}"
            for r, n in sorted(s["reactions"].items(), key=lambda kv: -kv[1])
        )
        for v in s["variants"][:2]:
            text = f" “{v['text']}”" if v.get("text") else ""
            lines.append(f"- **{s['label'].capitalize()}**{text}: delivered to {v['delivered']} leads.")
        lines.append(f"  Reactions: {mix}.")
    if not report.get("sections"):
        lines.append("- No delivered script sections recorded yet.")
    lines += ["", "## Call shape (post-call)"]
    shape = [p for p in timing if p["status"] != "descriptive_only"] + [
        p for p in timing if p["status"] == "descriptive_only"
    ][:6]
    lines += [
        f"- {p['label']} (talk timing): closed {_pct(p['conversion_rate'])} of {p['samples']}; "
        + ("confirmed in a later period." if p["status"] != "descriptive_only" else "not yet confirmed.")
        for p in shape
    ] or ["- No timed calls yet (talk ratio needs transcription timestamps)."]
    lines += ["", "## Script tests"]
    for t in tests:
        arms = t.get("arms", {})
        a, b = arms.get("control", {}), arms.get("candidate", {})
        lines.append(
            f"- {t['status'].capitalize()}: {section_label(t['section'])} “{t['text']}” vs the base. "
            f"Base {a.get('matured_conversions', 0)}/{a.get('matured_leads', 0)}, "
            f"candidate {b.get('matured_conversions', 0)}/{b.get('matured_leads', 0)} matured; "
            f"about {t.get('required_per_arm', '?')} per arm needed."
        )
    if not tests:
        lines.append("- No script test yet.")
    lines += ["", "## Script versions"]
    for h in history:
        lines.append(f"- v{h['version']} adopted {h.get('adopted_at') or 'at setup'} ({h['source']}).")
    return "\n".join(lines)


def compose_memory(*, title, header, learned, notes):
    return (
        f"# Script memory: {title}\n\n{header}\n\n{LEARNED_START}\n{learned}\n{LEARNED_END}\n\n"
        f"## Team notes\n\n{notes.strip() if notes and notes.strip() else '_None yet._'}\n"
    )
