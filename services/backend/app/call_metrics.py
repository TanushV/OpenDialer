"""Per-call numbers (call insights plan phase 6): who spoke when, how the rep paced the call, and the rep's voice.

Timing comes from voice activity on each side's own audio track when the app captured it, otherwise from the
transcript segments' times. Word counts and questions come from segment metadata, which carries no words. Pitch and
loudness are measured on the rep's own kept audio, and on the prospect's only where their audio is kept.

Plan C5: these are a fixed list of named numbers. Nothing here builds a voiceprint, a speaker embedding or any
feature meant to identify a person; `ALLOWED` is the whole list, and a test holds the stored metrics to it.
"""

from statistics import median

ALLOWED = frozenset(
    {
        "source",
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
        "part_ms",
        "rep_words_per_minute",
        "rep_questions",
        "rep_pitch_range_semitones",
        "rep_loudness_dbfs",
        "rep_loudness_variation_db",
        "prospect_pitch_range_semitones",
    }
)
JOIN_MS = 1000  # Pauses shorter than this inside one side's speech do not end a monologue.
REPLY_WITHIN_MS = 10000
OVERLAP_MS = 300


def runs_from_segments(segments, side):
    return sorted(
        [row["start_ms"], row["end_ms"]]
        for row in segments
        if row.get("side") == side
        and row.get("is_final")
        and not row.get("bleed")
        and not row.get("system_audio")
        and row.get("start_ms") is not None
        and row.get("end_ms") is not None
        and row["end_ms"] > row["start_ms"]
    )


def joined(runs, other=()):
    """One side's speech as monologues: runs closer than JOIN_MS merge unless the other side spoke in the gap."""
    result = []
    for start, end in sorted(runs):
        if (
            result
            and start - result[-1][1] < JOIN_MS
            and not any(o_start < start and o_end > result[-1][1] for o_start, o_end in other)
        ):
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def interruptions(starter, holder):
    """How often one side began speaking while the other was mid-turn, by more than OVERLAP_MS."""
    return sum(
        1
        for start, _ in starter
        if any(h_start + OVERLAP_MS < start < h_end - OVERLAP_MS for h_start, h_end in holder)
    )


def timing(rep, remote, segments):
    rep_turns, prospect_turns = joined(rep, remote), joined(remote, rep)
    rep_ms = sum(end - start for start, end in rep)
    prospect_ms = sum(end - start for start, end in remote)
    gaps = []
    for _, end in prospect_turns:
        following = [start for start, _ in rep_turns if start >= end]
        if following and min(following) - end <= REPLY_WITHIN_MS:
            gaps.append(min(following) - end)
    edges = [edge for run in rep + remote for edge in run]
    result = {
        "duration_ms": (max(edges) - min(edges)) if edges else 0,
        "rep_talk_ms": rep_ms,
        "prospect_talk_ms": prospect_ms,
        "talk_share": round(rep_ms / (rep_ms + prospect_ms), 3) if rep_ms + prospect_ms else None,
        "longest_rep_monologue_ms": max((e - s for s, e in rep_turns), default=0),
        "longest_prospect_monologue_ms": max((e - s for s, e in prospect_turns), default=0),
        "median_response_ms": round(median(gaps)) if gaps else None,
        "rep_interruptions": interruptions(rep_turns, prospect_turns),
        "prospect_interruptions": interruptions(prospect_turns, rep_turns),
    }
    rep_rows = [
        row
        for row in segments
        if row.get("side") == "rep"
        and row.get("is_final")
        and not row.get("bleed")
        and not row.get("system_audio")
    ]
    pitch = [row["start_ms"] for row in rep_rows if (row.get("script_section") or "").startswith("pitch:")]
    result["time_to_pitch_ms"] = min(pitch) if pitch and pitch[0] is not None else None
    parts = {}
    for row in rep_rows:
        section = (row.get("script_section") or "").split(":", 1)[0]
        if section and row.get("start_ms") is not None and row.get("end_ms") is not None:
            parts[section] = parts.get(section, 0) + max(0, row["end_ms"] - row["start_ms"])
    result["part_ms"] = parts
    words = sum(row.get("word_count") or 0 for row in rep_rows)
    result["rep_words_per_minute"] = round(words / (rep_ms / 60000)) if rep_ms >= 5000 and words else None
    result["rep_questions"] = sum(row.get("question_count") or 0 for row in rep_rows)
    return result


def voice(pcm, *, rate=16000):
    """Pitch range (semitones, 10th to 90th percentile) and loudness of one side's speech, from its own PCM."""
    import numpy as np

    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float32) / 32768.0
    window = int(rate * 0.04)
    if len(samples) < window * 10:
        return {}
    frames = samples[: len(samples) // window * window].reshape(-1, window)
    level = 10 * np.log10(np.maximum(np.mean(frames * frames, axis=1), 1e-10))
    voiced = frames[level > -40]
    pitches = []
    low, high = int(rate / 400), int(rate / 70)
    for frame in voiced[:: max(1, len(voiced) // 600)]:
        frame = frame - frame.mean()
        correlation = np.correlate(frame, frame, mode="full")[window - 1 :]
        if correlation[0] <= 0:
            continue
        lag = low + int(np.argmax(correlation[low:high]))
        if correlation[lag] / correlation[0] > 0.45:
            pitches.append(rate / lag)
    result = {}
    if len(pitches) >= 10:
        semitones = 12 * np.log2(np.array(pitches) / 100.0)
        result["pitch_range_semitones"] = round(
            float(np.percentile(semitones, 90) - np.percentile(semitones, 10)), 2
        )
    if len(voiced):
        loud = level[level > -40]
        result["loudness_dbfs"] = round(float(np.mean(loud)), 1)
        result["loudness_variation_db"] = round(float(np.std(loud)), 1)
    return result


def compute(segments, activity=None, rep_pcm=None, prospect_pcm=None):
    """All numbers for one call. `activity` is {side: runs} from the audio tracks, when captured."""
    if activity and (activity.get("rep") or activity.get("remote")):
        rep, remote, source = activity.get("rep") or [], activity.get("remote") or [], "audio"
    else:
        rep, remote, source = (
            runs_from_segments(segments, "rep"),
            runs_from_segments(segments, "remote"),
            "transcript",
        )
    result = {"source": source, **timing(rep, remote, segments)}
    if rep_pcm:
        sound = voice(rep_pcm)
        result.update(
            {
                f"rep_{key}": value
                for key, value in sound.items()
                if f"rep_{key}"
                in {"rep_pitch_range_semitones", "rep_loudness_dbfs", "rep_loudness_variation_db"}
            }
        )
    if prospect_pcm:
        sound = voice(prospect_pcm)
        if "pitch_range_semitones" in sound:
            result["prospect_pitch_range_semitones"] = sound["pitch_range_semitones"]
    return {key: value for key, value in result.items() if key in ALLOWED}
