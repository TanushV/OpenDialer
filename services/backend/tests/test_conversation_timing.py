"""A27/A48/A50: bounded, observable timing without double-counted tracks."""

import math
import random

import pytest
from app.postcall.analysis import behavioral_metrics


def segment(side, start, end, **extra):
    return {"side": side, "start_ms": start, "end_ms": end, **extra}


def test_overlapping_same_side_segments_do_not_duplicate_talk_or_overlap():
    metrics = behavioral_metrics(
        [
            segment("rep", 0, 10),
            segment("rep", 5, 15),
            segment("remote", 7, 12),
            segment("remote", 8, 13),
        ]
    )
    assert metrics["talk_ms"] == {"rep": 15, "remote": 6}
    assert metrics["overlap_ms"] == 6
    assert metrics["rep_talk_ratio"] == pytest.approx(15 / 21)


def test_segment_boundaries_do_not_invent_a_gap_or_shorten_a_continuous_speaking_turn():
    metrics = behavioral_metrics(
        [
            segment("rep", 0, 5),
            segment("rep", 5, 10),
            segment("remote", 14, 20),
            segment("remote", 20, 24),
        ]
    )
    assert metrics["longest_monologue_ms"] == 10
    assert metrics["turn_exchanges"] == 1
    assert metrics["response_gaps_ms"] == [4]


def test_other_side_speech_interrupts_a_monologue_and_nested_turns_have_no_false_gap():
    metrics = behavioral_metrics(
        [
            segment("rep", 0, 100),
            segment("remote", 20, 40),
            segment("remote", 60, 80),
        ]
    )
    assert metrics["longest_monologue_ms"] == 20
    # Overlap alone cannot prove a transfer of the conversational floor.
    assert metrics["response_gaps_ms"] == []
    assert metrics["turn_exchanges"] == 0


@pytest.mark.parametrize(
    "start,end",
    [(None, 1), (0, None), (-1, 5), (5, 4), (5, 5), (0, math.inf), (0, math.nan), ("0", 5), (False, 5)],
)
def test_missing_or_invalid_timing_is_unknown_not_fabricated_speaking_time(start, end):
    metrics = behavioral_metrics([segment("rep", start, end, question_count=1)])
    assert metrics["timed_segments"] == 0
    assert metrics["total_segments"] == 1
    assert metrics["rep_talk_ratio"] is None
    assert metrics["overlap_ms"] is None
    assert metrics["longest_monologue_ms"] is None
    assert metrics["question_count"] == 1


def test_overlap_is_not_a_response_pause_and_simultaneous_sides_have_no_inferred_floor():
    observed = behavioral_metrics([segment("rep", 0, 10), segment("remote", 5, 15)])
    assert observed["turn_exchanges"] == 1
    assert observed["response_gaps_ms"] == [0]
    assert observed["longest_monologue_ms"] == 5
    simultaneous = behavioral_metrics([segment("rep", 0, 10), segment("remote", 0, 10)])
    assert simultaneous["turn_exchanges"] == 0
    assert simultaneous["longest_monologue_ms"] == 0


def test_system_audio_and_unknown_sides_are_excluded_from_behavior():
    measured = behavioral_metrics(
        [
            segment("rep", 0, 10, system_audio=True, question_count=2),
            segment("unknown", 0, 20, question_count=1),
            segment("remote", 25, 30),
        ]
    )
    assert measured["talk_ms"] == {"rep": 0, "remote": 5}
    assert measured["question_count"] == 0
    assert measured["timed_segments"] == 1


def test_timing_matches_an_independent_small_interval_union_oracle():
    rng = random.Random(410)
    for _ in range(100):
        chunks = [
            segment(rng.choice(["rep", "remote"]), start := rng.randrange(30), start + rng.randrange(1, 15))
            for _ in range(12)
        ]
        observed = {
            side: {
                t
                for chunk in chunks
                if chunk["side"] == side
                for t in range(chunk["start_ms"], chunk["end_ms"])
            }
            for side in ("rep", "remote")
        }
        metrics = behavioral_metrics(chunks)
        assert metrics["talk_ms"] == {side: len(points) for side, points in observed.items()}
        assert metrics["overlap_ms"] == len(observed["rep"] & observed["remote"])
        assert metrics == behavioral_metrics(list(reversed(chunks)))


def test_timing_does_not_compare_every_segment_pair():
    class Time(float):
        comparisons = 0

        def __lt__(self, other):
            Time.comparisons += 1
            return super().__lt__(other)

        def __gt__(self, other):
            Time.comparisons += 1
            return super().__gt__(other)

    chunks = [segment("rep" if i % 2 else "remote", Time(i * 10), Time(i * 10 + 5)) for i in range(800)]
    metrics = behavioral_metrics(chunks)
    assert metrics["overlap_ms"] == 0
    assert Time.comparisons < 40000, Time.comparisons
