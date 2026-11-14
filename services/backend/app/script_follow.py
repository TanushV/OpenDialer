"""Match short phrases in the call's frozen script to finalized rep speech."""

import re
from difflib import SequenceMatcher
from functools import lru_cache

WORDS = 40


def tokens(text):
    return tuple(re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.casefold().replace("’", "'")))


def options(script):
    for section in script.get("sections", []):
        if section.get("spoken") is False or not section.get("key"):
            continue
        for option in section.get("options") or ():
            yield f"{section['key']}:{option['id']}", option


def sequence(segments):
    """Use audio offsets when both sides have them, with receipt order as a fallback."""
    rows = [
        row
        for row in segments
        if row.get("is_final") and not row.get("system_audio") and row.get("side") in {"rep", "remote"}
    ]
    timed = all(row.get("start_ms") is not None for row in rows)
    return sorted(
        rows,
        key=lambda row: (
            row["start_ms"] if timed else row.get("server_received_at", ""),
            row.get("transcript_version", 0),
            row.get("segment_id", ""),
        ),
    )


def latest_match(segments):
    return next(
        (row for row in reversed(sequence(segments)) if row["side"] == "rep" and row.get("script_section")),
        None,
    )


@lru_cache(maxsize=512)
def phrases(text):
    return tuple(tokens(part) for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip())


def _score(spoken, new_start, target):
    groups = []
    for block in SequenceMatcher(None, spoken, target, autojunk=False).get_matching_blocks()[:-1]:
        if (
            groups
            and block.a - groups[-1][-1].a - groups[-1][-1].size <= 2
            and (block.b - groups[-1][-1].b - groups[-1][-1].size <= 2)
        ):
            groups[-1].append(block)
        else:
            groups.append([block])
    best = 0.0
    for group in groups:
        first, last = group[0], group[-1]
        matched = sum(block.size for block in group)
        fresh = sum(max(0, block.a + block.size - max(block.a, new_start)) for block in group)
        # A fresh segment must carry evidence. A tangent cannot inherit an earlier match.
        minimum = min(5, len(target))
        if minimum < 3 or matched < minimum or fresh < min(3, minimum):
            continue
        span = max(last.a + last.size - first.a, last.b + last.size - first.b)
        density = matched / span
        if density >= 0.7:
            best = max(best, min(1.0, matched / min(8, len(target))) * density)
    return best


def match(script, segment, earlier=()):
    current = tokens(segment.get("text", ""))[-WORDS:]
    prior = []
    for row in reversed(earlier[-3:]):
        if row["side"] != "rep":
            break
        prior.append(row)
    tail = (
        tokens(" ".join(row.get("text", "") for row in reversed(prior)))[-(WORDS - len(current)) :]
        if len(current) < WORDS
        else ()
    )
    last = next((row for row in reversed(earlier) if row.get("script_section")), None)
    matches = []
    for key, option in options(script):
        score = max(
            (_score(tail + current, len(tail), words) for words in phrases(option.get("text", ""))),
            default=0.0,
        )
        if score:
            matches.append((score, key))
    matches.sort(reverse=True)
    if not matches:
        return None, 0.0
    score, key = matches[0]
    needed = 0.62 if last is None or last["script_section"] == key else 0.72
    if score < needed or len(matches) > 1 and score - matches[1][0] < 0.10:
        return None, round(score, 3)
    return key, round(score, 3)
