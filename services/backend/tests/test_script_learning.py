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
from app.preparation.templates import parse_markdown, render

MARKDOWN = """# Opening
Hi {first_name}, this is {rep} from {seller}.

# Screening
{rep} from {seller}, calling about invoice follow-up.

# Gatekeeper
Who handles invoice follow-up for {company}?

# Discovery
- How does your team handle invoice follow-up today?

# Close
Would a short walkthrough next week be useful?
"""
TEMPLATE = parse_markdown(MARKDOWN)
SCRIPT = {
    **render(TEMPLATE, lead={"name": "Dana Ruiz", "company": "Dana Co"}, seller="Acme", rep="Sam"),
    "template": TEMPLATE,
    "base_version": 2,
    "script_arm": None,
}
OPEN, ASK, CLOSE, GATE = (
    "opening:opening-1",
    "discovery:discovery-1",
    "close:close-1",
    "gatekeeper:gatekeeper-1",
)


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
    assert match_section("Hi Dana, this is Sam from Acme.", SCRIPT)[0] == OPEN
    assert match_section("so how does your team handle invoice follow-up these days?", SCRIPT)[0] == ASK
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
        segment("r1", "rep", 0, 3000, words=7, section=OPEN),
        segment("s1", "remote", 3500, 4000, words=2),
        segment("r2", "rep", 4500, 7000, words=8, questions=1, section=ASK),
        segment("s2", "remote", 7500, 9000, words=5),
        segment("r3", "rep", 9500, 11000, words=8, questions=1, section=CLOSE),
    ]
    trace = script_trace(SCRIPT, segments, [objection("s2")])
    assert [(s["key"], s["reaction"]) for s in trace["sections"]] == [
        (OPEN, "brief"),
        (ASK, "objection"),
        (CLOSE, "ended"),
    ]
    unreviewed = script_trace(SCRIPT, segments, [objection("s2", status="model_interpreted")])
    assert unreviewed["sections"][1]["reaction"] == "brief", "Only reviewed interpretations count"
    assert trace["base_version"] == 2 and trace["opening"]["first_response_ms"] == 3500
    assert trace["shape"]["time_to_first_question_ms"] == 4500
    assert trace["shape"]["discovery_questions_asked"] == 1


def test_a_repeated_section_counts_once_and_silence_before_the_next_is_silent():
    segments = [
        segment("r1", "rep", 0, 2000, words=7, section=OPEN),
        segment("r2", "rep", 2100, 4000, words=8, section=GATE),
        segment("r3", "rep", 4100, 6000, words=7, section=OPEN),
        segment("s1", "remote", 6500, 9000, words=14),
    ]
    trace = script_trace(SCRIPT, segments)
    assert [(s["key"], s["reaction"]) for s in trace["sections"]] == [
        (OPEN, "silent"),
        (GATE, "engaged"),
    ]


def test_without_timestamps_the_order_is_kept_and_timing_is_unknown():
    segments = [
        {
            **segment("r1", "rep", None, None, words=7, section=OPEN),
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
        segment("r1", "rep", 0, 4000, words=7, section=OPEN),
        segment("s1", "remote", 4500, 6000, words=3, questions=1),
        segment("r2", "rep", 6500, 9000, words=8, questions=1, section=ASK),
        segment("s2", "remote", 9500, 21000, words=16),
    ]
    features = dict(trace_features(script_trace(SCRIPT, segments)))
    assert features["rep_talk_ratio"] == "<35%" and features["discovery_questions"] == "1"
    assert features["time_to_first_question"] == "<20s" and features["opening_length"] == "<10s"
    assert features["longest_monologue"] == "<30s" and features["reaction:" + OPEN] == "engaged"
    assert len(features["section:" + OPEN]) == 12


@pytest.mark.parametrize(
    ("section", "text"),
    [
        ("screening", "{rep} from {seller}, calling about invoices."),
        ("opening", "Hi {name}, quick question."),
        ("opening", "Hi {name}, {rep} from {seller}. Guaranteed savings."),
        ("opening", "Hi {name}, {rep} from {seller}. {unknown}"),
        ("discovery", "What happens next?"),
        ("close", "Would a short walkthrough next week be useful?"),
    ],
)
def test_locked_or_invalid_changes_are_refused(section, text):
    with pytest.raises(ValueError):
        candidate_template(TEMPLATE, section, text)


def test_generated_changes_cannot_add_numbers_or_prices():
    with pytest.raises(ValueError):
        candidate_template(TEMPLATE, "close", "Would 15 minutes on Tuesday work?", source="memory")
    team = candidate_template(TEMPLATE, "close", "- Would 15 minutes on Tuesday work?\n- Or Thursday?")
    assert [o["text"] for o in team["sections"][-1]["options"]] == [
        "Would 15 minutes on Tuesday work?",
        "Or Thursday?",
    ]
    assert team["sections"][1] == TEMPLATE["sections"][1], "the screening line is untouched"


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
            {"key": OPEN, "delivered": 12, "engaged_rate": 0.2},
            {"key": "screening:screening-1", "delivered": 30, "engaged_rate": 0.0},
            {"key": CLOSE, "delivered": 4, "engaged_rate": 0.0},
            {"key": ASK, "delivered": 15, "engaged_rate": 0.6},
        ]
    }
    assert weakest_section(report)["key"] == OPEN
    assert weakest_section({"sections": []}) is None


def test_an_offer_option_is_its_own_learning_key():
    from app.optimization.script_learning import section_label, section_texts

    template = parse_markdown(
        MARKDOWN + "\n# Offers\n## Audit\nA free audit.\n\n## Trial\nA two-week trial.\n"
    )
    script = {
        **render(template, lead={"name": "Dana"}, seller="Acme", rep="Sam", picks={"offers": ["offers-2"]}),
        "template": template,
    }
    keys = [key for key, _, _ in section_texts(script)]
    assert "offers:offers-2" in keys and "offers:offers-1" not in keys
    assert (
        section_label("offers:offers-2") == "offer 2"
        and section_label("screening:screening-1") == "screening line"
    )
