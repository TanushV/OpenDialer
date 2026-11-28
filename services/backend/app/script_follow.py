"""Match short phrases in the call's frozen script to finalized rep speech.

Call insights plan P6: the panel moves on its own only to nearby parts (the current part, the next two in Markdown
order, the previous one). A far part needs a distinctive phrase, said in two rep segments in a row; until then it is
offered as a suggestion the rep can click. Parts reached from anywhere by nature (objections, screening, gatekeeper)
need the distinctive phrase but not the repeat. Rep segments that repeat the prospect's words at the same moment are
speaker bleed (C6) and never move the panel.
"""

import re
from difflib import SequenceMatcher
from functools import lru_cache

WORDS = 40
NEAR_BEFORE, NEAR_AFTER = 1, 2
FAR_MIN_WORDS = 6
FAR_MARGIN = 0.20
ANYWHERE = frozenset({"objections", "screening", "gatekeeper"})
BLEED_SIMILARITY = 0.70
BLEED_WINDOW_MS = 2500


def tokens(text):
    return tuple(re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.casefold().replace("’", "'")))


def options(script):
    for section in script.get("sections", []):
        if section.get("spoken") is False or not section.get("key"):
            continue
        for option in section.get("options") or ():
            yield f"{section['key']}:{option['id']}", option


def section_of(script, part):
    """The section key of a part key (section keys may contain colons, such as `custom:handoff`)."""
    for section in script.get("sections", []):
        key = section.get("key")
        if (
            key
            and part.startswith(key + ":")
            and any(part == f"{key}:{option['id']}" for option in section.get("options") or ())
        ):
            return key
    return part.split(":", 1)[0]


def order(script):
    """Each spoken section's place in the script's Markdown order, leaving out the parts reached from anywhere."""
    keys = [
        s["key"]
        for s in script.get("sections", [])
        if s.get("spoken") is not False and s.get("key") and s["key"] not in ANYWHERE
    ]
    return {key: index for index, key in enumerate(dict.fromkeys(keys))}


def usable(row):
    return row.get("is_final") and not row.get("system_audio") and not row.get("bleed")


def sequence(segments):
    """Use audio offsets when both sides have them, with receipt order as a fallback."""
    rows = [row for row in segments if usable(row) and row.get("side") in {"rep", "remote"}]
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


def latest_suggestion(segments):
    """A far part the rep's latest matched speech pointed at once, newer than the panel's position."""
    for row in reversed(sequence(segments)):
        if row["side"] != "rep":
            continue
        if row.get("script_section"):
            return None
        if row.get("script_candidate"):
            return row
    return None


@lru_cache(maxsize=512)
def phrases(text):
    return tuple(tokens(part) for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip())


def _score(spoken, new_start, target):
    """(score, matched words) of the best dense run of `target` in the spoken window."""
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
    best = (0.0, 0)
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
            best = max(best, (min(1.0, matched / min(8, len(target))) * density, matched))
    return best


def match(script, segment, earlier=()):
    """(part key or None, score, far part awaiting a second matching segment or None)."""
    earlier = [row for row in earlier if usable(row)]
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
        # The segment on its own as well: a phrase repeated from the previous segment would otherwise
        # align with its earlier copy in the window and carry no fresh evidence.
        score, matched = max(
            (
                max(_score(tail + current, len(tail), words), _score(current, 0, words))
                for words in phrases(option.get("text", ""))
            ),
            default=(0.0, 0),
        )
        if score:
            matches.append((score, matched, key))
    matches.sort(reverse=True)
    if not matches:
        return None, 0.0, None
    score, matched, key = matches[0]
    runner = matches[1][0] if len(matches) > 1 else 0.0
    needed = 0.62 if last is None or last["script_section"] == key else 0.72
    if score < needed or score - runner < 0.10:
        return None, round(score, 3), None
    places = order(script)
    section = section_of(script, key)
    if last and section_of(script, last["script_section"]) == section:
        return key, round(score, 3), None
    if section in places:
        # Distance runs from the last part of the main flow the rep read, so a detour comes back to it.
        flow = next(
            (
                section_of(script, row["script_section"])
                for row in reversed(earlier)
                if row.get("script_section") and section_of(script, row["script_section"]) in places
            ),
            next(iter(places)),
        )
        if -NEAR_BEFORE <= places[section] - places[flow] <= NEAR_AFTER:
            return key, round(score, 3), None
    if matched < FAR_MIN_WORDS or score - runner < FAR_MARGIN:
        return None, round(score, 3), None
    if section in ANYWHERE:
        return key, round(score, 3), None
    previous = next((row for row in reversed(earlier) if row["side"] == "rep"), None)
    if previous and previous.get("script_candidate") == key:
        return key, round(score, 3), None
    return None, round(score, 3), key


def bleeds(rep, remote):
    """Whether a rep segment repeats a prospect segment spoken at the same moment (speaker bleed)."""
    mine, theirs = tokens(rep.get("text", "")), tokens(remote.get("text", ""))
    if len(mine) < 3 or len(theirs) < 3:
        return False
    if all(row.get("start_ms") is not None and row.get("end_ms") is not None for row in (rep, remote)):
        apart = max(rep["start_ms"], remote["start_ms"]) - min(rep["end_ms"], remote["end_ms"])
    else:
        from datetime import datetime

        stamps = [row.get("server_received_at") for row in (rep, remote)]
        if not all(stamps):
            return False
        first, second = (datetime.fromisoformat(stamp) for stamp in stamps)
        apart = abs((first - second).total_seconds()) * 1000
    if apart > BLEED_WINDOW_MS:
        return False
    return SequenceMatcher(None, mine, theirs, autojunk=False).ratio() >= BLEED_SIMILARITY
