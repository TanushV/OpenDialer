"""R9.2 and R9.5: a trigger stands only when code checks pass; strength comes from the rule, never from the model."""

from datetime import date

import pytest
from app.leads.record import UNVERIFIED
from app.leads.record import trigger_label as label
from app.prep.criteria import DEFAULT_RULES, check_trigger, default_section, parse_rules

TODAY = date(2026, 11, 8)
RULES = list(DEFAULT_RULES)
PAGE = "Acme Logistics is hiring 3 SDRs to grow its outbound team this quarter."


def claim(**trigger):
    base = {
        "type": "hiring_sellers",
        "date": "2026-10-20",
        "source_url": "https://acme.example/careers",
        "quote": "hiring 3 SDRs to grow its outbound team",
        "evidence": "page",
    }
    return {"trigger": {**base, **trigger}, "bridge": {"text": "Saw you're growing the SDR team."}}


def check(value, **kw):
    return check_trigger(value, RULES, today=TODAY, **({"page_text": PAGE, "names_lead": True} | kw))


def test_a_checked_page_trigger_stands_with_the_rules_strength():
    record, errors = check(claim())
    assert errors == []
    assert record["trigger"]["strength"] == "A" and record["trigger"]["evidence"] == "page"
    assert record["bridge"] == {"text": "Saw you're growing the SDR team.", "is_inference": True}
    assert label(record["trigger"]) == "A · checked on the page"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"type": "rebrand"}, "not on the list"),
        ({"date": "2026-06-01"}, "outside the 90-day window"),
        ({"date": "2026-12-01"}, "in the future"),
        ({"quote": None}, "needs an exact quote"),
        ({"quote": "hiring 30 account executives"}, "quote is not on"),
        ({"evidence": "rumour"}, "page or row_only"),
    ],
)
def test_out_of_window_unlisted_and_unquoted_triggers_are_rejected(change, message):
    record, errors = check(claim(**change))
    assert record is None and any(message in e for e in errors), errors


def test_a_page_that_does_not_name_the_company_or_cannot_be_read_is_rejected():
    assert check(claim(), names_lead=False)[1][0].endswith("does not name this lead or their company")
    assert "could not be read" in check(claim(), page_text=None)[1][0]


def test_a_row_only_signal_is_capped_at_c_and_labelled_unverified():
    url = "https://acme.example/careers"
    record, errors = check(claim(evidence="row_only", quote="ignored"), page_text=None, row_urls=(url,))
    assert errors == []
    trigger = record["trigger"]
    assert trigger["strength"] == "C" and trigger["quote"] is None and trigger["from_row"] is True
    assert label(trigger) == f"C · {UNVERIFIED}"
    # A weaker rule stays at its own strength... and C is the ceiling, never raised.
    weak = [{**RULES[2], "strength": "C"}]
    assert (
        check_trigger(claim(type="new_funding", evidence="row_only"), weak, today=TODAY, row_urls=(url,))[0][
            "trigger"
        ]["strength"]
        == "C"
    )


def test_row_only_without_any_row_signal_is_rejected():
    assert "needs a signal in the lead's row" in check(claim(evidence="row_only"), row_urls=())[1][0]


def test_none_needs_no_evidence_but_still_a_bridge():
    record, errors = check(
        {"trigger": {"type": "none"}, "bridge": {"text": "Calling about your outbound team."}}
    )
    assert (
        errors == []
        and record["trigger"]["strength"] == "none"
        and label(record["trigger"]) == "No trigger found"
    )
    assert check({"trigger": {"type": "none"}, "bridge": {"text": ""}})[0] is None


def test_the_seeded_section_parses_back_to_the_defaults():
    lines = list(enumerate(default_section().splitlines()[1:], 2))
    rules, errors = parse_rules(lines)
    assert errors == [] and rules == RULES


def test_rule_lines_are_checked():
    rules, errors = parse_rules(
        [(3, "- Hiring SDRs (90 days, A)"), (4, "- Bad line"), (5, "- x: Twice (9 days, D)")]
    )
    assert rules == [{"type": "hiring_sdrs", "label": "Hiring SDRs", "window_days": 90, "strength": "A"}]
    assert [e["line"] for e in errors] == [4, 5]
    assert parse_rules([(2, "- a: One (0 days, A)")])[1][0]["message"] == "A trigger window is 1 to 730 days"
    assert parse_rules([])[1][0]["line"] == 0
