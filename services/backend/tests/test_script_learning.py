"""Script learning primitives: section matching, reactions, call shape, features and locks."""

import pytest
from app.optimization.script_learning import (
    candidate_template,
    match_section,
    memory_digest,
    script_trace,
    trace_features,
    weakest_section,
)

TEMPLATE = {
    "topic": "invoice follow-up",
    "opener": "Hi {name}, this is {rep} from {seller}. {relevance}",
    "gatekeeper": "Who handles {topic} for {company}?",
    "screening_intro": "{rep} from {seller}, calling about {topic}.",
    "discovery_questions": ["How does your team handle {topic} today?"],
    "proof": "{proof}",
    "closing_question": "Would a short walkthrough next week be useful?",
    "final_voicemail_asset": None,
}
SCRIPT = {
    "template": TEMPLATE,
    "opener": "Hi Dana, this is Sam from Acme.",
    "gatekeeper": "Who handles invoice follow-up for Dana Co?",
    "screening_intro": "Sam from Acme, calling about invoice follow-up.",
    "discovery_questions": ["How does your team handle invoice follow-up today?"],
    "proof": "",
    "closing_question": "Would a short walkthrough next week be useful?",
    "base_version": 2,
    "script_arm": None,
}


def segment(segment_id, side, start, end, *, words=0, questions=0, section=None):
    return {
        "segment_id": segment_id,
        "side": side,
        "is_final": True,
        "start_ms": start,
        "end_ms": end,
        "word_count": words,
        "question_count": questions,
        "script_section": section,
    }


def test_a_rep_line_matches_the_section_it_delivers():
    assert match_section("Hi Dana, this is Sam from Acme.", SCRIPT)[0] == "opener"
    assert match_section("so how does your team handle invoice follow-up these days?", SCRIPT)[0] == (
        "discovery_questions:0"
    )
    assert match_section("We built a platform with dashboards for finance teams.", SCRIPT)[0] is None
    assert match_section("Who handles it?", SCRIPT)[0] is None, "Too little of the section was said"


def objection(segment_id, status="model_reviewed"):
    return {
        "kind": "objection",
        "state": "open",
        "epistemic_status": status,
        "sources": [{"kind": "segment", "id": segment_id, "version": 1}],
    }


def test_reactions_follow_the_prospect_after_each_delivered_section():
    segments = [
        segment("r1", "rep", 0, 3000, words=7, section="opener"),
        segment("s1", "remote", 3500, 4000, words=2),
        segment("r2", "rep", 4500, 7000, words=8, questions=1, section="discovery_questions:0"),
        segment("s2", "remote", 7500, 9000, words=5),
        segment("r3", "rep", 9500, 11000, words=8, questions=1, section="closing_question"),
    ]
    trace = script_trace(SCRIPT, segments, [objection("s2")])
    assert [(s["key"], s["reaction"]) for s in trace["sections"]] == [
        ("opener", "brief"),
        ("discovery_questions:0", "objection"),
        ("closing_question", "ended"),
    ]
    unreviewed = script_trace(SCRIPT, segments, [objection("s2", status="model_interpreted")])
    assert unreviewed["sections"][1]["reaction"] == "brief", "Only reviewed interpretations count"
    assert trace["base_version"] == 2 and trace["opening"]["first_response_ms"] == 3500
    assert trace["shape"]["time_to_first_question_ms"] == 4500
    assert trace["shape"]["discovery_questions_asked"] == 1


def test_a_repeated_section_counts_once_and_silence_before_the_next_is_silent():
    segments = [
        segment("r1", "rep", 0, 2000, words=7, section="opener"),
        segment("r2", "rep", 2100, 4000, words=8, section="gatekeeper"),
        segment("r3", "rep", 4100, 6000, words=7, section="opener"),
        segment("s1", "remote", 6500, 9000, words=14),
    ]
    trace = script_trace(SCRIPT, segments)
    assert [(s["key"], s["reaction"]) for s in trace["sections"]] == [
        ("opener", "silent"),
        ("gatekeeper", "engaged"),
    ]


def test_without_timestamps_the_order_is_kept_and_timing_is_unknown():
    segments = [
        {
            **segment("r1", "rep", None, None, words=7, section="opener"),
            "server_received_at": "2026-10-24T10:00:00",
        },
        {**segment("s1", "remote", None, None, words=20), "server_received_at": "2026-10-24T10:00:05"},
    ]
    trace = script_trace(SCRIPT, segments)
    assert trace["shape"]["timing"] == "order_only" and trace["shape"]["rep_talk_ratio"] is None
    assert trace["sections"][0]["reaction"] == "engaged" and trace["sections"][0]["portion_ms"] is None
    assert not {f for f, _ in trace_features(trace)} & {"rep_talk_ratio", "opening_length"}


def test_features_bucket_call_shape_and_name_section_wording():
    segments = [
        segment("r1", "rep", 0, 4000, words=7, section="opener"),
        segment("s1", "remote", 4500, 6000, words=3, questions=1),
        segment("r2", "rep", 6500, 9000, words=8, questions=1, section="discovery_questions:0"),
        segment("s2", "remote", 9500, 21000, words=16),
    ]
    features = dict(trace_features(script_trace(SCRIPT, segments)))
    assert features["rep_talk_ratio"] == "<35%" and features["discovery_questions"] == "1"
    assert features["time_to_first_question"] == "<20s" and features["opening_length"] == "<10s"
    assert features["longest_monologue"] == "<30s" and features["reaction:opener"] == "engaged"
    assert len(features["section:opener"]) == 12


@pytest.mark.parametrize(
    ("section", "text", "index"),
    [
        ("screening_intro", "Calling about {topic}.", None),
        ("proof", "We halve collection time.", None),
        ("opener", "Hi {name}, quick question.", None),
        ("opener", "Hi {name}, {rep} from {seller}. Guaranteed savings.", None),
        ("opener", "Hi {name}, {rep} from {seller}. {unknown}", None),
        ("discovery_questions", "What happens next?", 3),
        ("closing_question", TEMPLATE["closing_question"], None),
    ],
)
def test_locked_or_invalid_changes_are_refused(section, text, index):
    with pytest.raises(ValueError):
        candidate_template(TEMPLATE, section, text, index=index)


def test_generated_changes_cannot_add_numbers_or_prices():
    with pytest.raises(ValueError):
        candidate_template(TEMPLATE, "closing_question", "Would 15 minutes on Tuesday work?", source="memory")
    team = candidate_template(TEMPLATE, "closing_question", "Would 15 minutes on Tuesday work?")
    assert team["closing_question"] == "Would 15 minutes on Tuesday work?" and team["proof"] == "{proof}"


def test_the_live_digest_never_carries_call_timing():
    report = {
        "as_of": "2026-10-24T00:00:00+00:00",
        "patterns": [
            {
                "feature": "discovery_questions",
                "value": "1",
                "label": "Q",
                "samples": 30,
                "conversion_rate": 0.5,
                "difference": 0.4,
                "status": "supported_correlation",
                "live": True,
            },
            {
                "feature": "rep_talk_ratio",
                "value": "<35%",
                "label": "T",
                "samples": 30,
                "conversion_rate": 0.5,
                "difference": 0.4,
                "status": "supported_correlation",
                "live": False,
            },
            {
                "feature": "reaction:opener",
                "value": "ended",
                "label": "E",
                "samples": 30,
                "conversion_rate": 0.0,
                "difference": -0.3,
                "status": "supported_negative",
                "live": True,
            },
        ],
    }
    digest = memory_digest(report, "Lead with time saved.")
    assert [p["feature"] for p in digest["working"]] == ["discovery_questions"]
    assert [p["feature"] for p in digest["not_working"]] == ["reaction:opener"]
    assert digest["team_notes"] == "Lead with time saved."


def test_the_weakest_section_needs_enough_deliveries_and_must_be_editable():
    report = {
        "sections": [
            {"key": "opener", "delivered": 12, "engaged_rate": 0.2},
            {"key": "screening_intro", "delivered": 30, "engaged_rate": 0.0},
            {"key": "closing_question", "delivered": 4, "engaged_rate": 0.0},
            {"key": "discovery_questions:0", "delivered": 15, "engaged_rate": 0.6},
        ]
    }
    assert weakest_section(report)["key"] == "opener"
    assert weakest_section({"sections": []}) is None
