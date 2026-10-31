"""The one pasted Markdown script: structure, options, AI blocks and the per-lead close-out rules."""

import pytest
from app.preparation.templates import (
    STARTER,
    ScriptError,
    check_submission,
    parse_markdown,
    render,
    replace_section,
    section_markdown,
)

SCRIPT = """# Opening
- Hi {first_name}, this is {rep} with {seller}. {{ why we're calling {company}, from research }}
- Hi {name}, {rep} from {seller} here.

# Screening
{rep} from {seller}, calling about roof inspections.

# Discovery
- When was the roof last inspected?
- Who handles repairs today?
- What happens when a leak shows up?
- How old is the building?

# Offers
## Free inspection
A free 20-minute roof inspection.

## Maintenance plan
{{ how a maintenance plan fits their buildings }}

# Objections
## Not interested
Is that because it's handled, or the timing?

## Send an email
Happy to. What should it cover?

# Close
Would Tuesday or Thursday work for a 15-minute call?

# Facts
- Licensed and insured since 2009.

# Never say
- Guaranteed savings
"""


def errors(markdown):
    with pytest.raises(ScriptError) as caught:
        parse_markdown(markdown)
    return caught.value.errors


def test_starter_and_full_script_parse_into_sections_options_and_blocks():
    assert parse_markdown(STARTER)["blocks"]
    template = parse_markdown(SCRIPT)
    assert [s["key"] for s in template["sections"]] == [
        "opening",
        "screening",
        "discovery",
        "offers",
        "objections",
        "close",
    ]
    opening = template["sections"][0]
    assert [o["id"] for o in opening["options"]] == ["opening-1", "opening-2"] and opening["pick"] == [1, 1]
    assert template["sections"][2]["pick"] == [1, 3]
    assert [o["title"] for o in template["sections"][3]["options"]] == ["Free inspection", "Maintenance plan"]
    assert [b["id"] for b in template["blocks"]] == ["ai-1", "ai-2"]
    assert template["blocks"][1] == {
        "id": "ai-2",
        "instruction": "how a maintenance plan fits their buildings",
        "section": "offers",
        "option": "offers-2",
    }
    assert template["facts"] == ["Licensed and insured since 2009."]
    assert template["never_say"] == ["Guaranteed savings"]
    assert template["sections"][1]["pick"] is None and template["sections"][5]["pick"] is None


def test_every_structure_error_is_reported_with_its_line():
    found = errors(
        "Intro text\n# Opening\nHello there\n# Pricing\n- $10\n# Screening\nCalling about roofs.\n"
        "# Discovery\nHow are you?\n# Discovery\n- again\n## Stray\n"
    )
    messages = [(e["line"], e["message"]) for e in found]
    assert (1, "Put this text under a # section heading") in messages
    assert any(line == 2 and "who is calling" in m for line, m in messages)
    assert any(line == 4 and "Unknown section '# Pricing'" in m for line, m in messages)
    assert any(line == 6 and "{seller}" in m for line, m in messages)
    assert any("Add at least one bullet to Discovery" in m or "mixes bullets" in m for _, m in messages)
    assert any(line == 10 and "appears twice" in m for line, m in messages)
    assert any(line == 0 and "# Close" in m for line, m in messages)


def test_fields_blocks_and_cards_are_validated():
    base = SCRIPT.replace("A free 20-minute roof inspection.", "Hi {secret}")
    assert any("Unknown field {secret}" in e["message"] for e in errors(base))
    assert any(
        "Close every {{" in e["message"] for e in errors(SCRIPT.replace("from research }}", "from research"))
    )
    assert any(
        "{{ }}" in e["message"] for e in errors(SCRIPT.replace("calling about roof", "{{ x y z }} roof"))
    )
    assert any(
        "own ## heading" in e["message"]
        for e in errors(SCRIPT.replace("# Offers\n", "# Offers\nLoose text\n"))
    )
    assert any(
        "## headings only" in e["message"] for e in errors(SCRIPT.replace("# Close\n", "# Close\n## Ask\n"))
    )


def test_render_uses_picks_and_written_text_and_falls_back_cleanly():
    template = parse_markdown(SCRIPT)
    lead = {"name": "Dana Reyes", "company": "Hilltop Apartments"}
    fallback = render(template, lead=lead, seller="Peak Roofing", rep="Sam")
    assert fallback["opener"] == "Hi Dana, this is Sam with Peak Roofing."
    assert fallback["discovery_questions"] == [
        "When was the roof last inspected?",
        "Who handles repairs today?",
        "What happens when a leak shows up?",
    ]
    # An option that is only AI text is left out until it has been written.
    assert [o["title"] for o in fallback["offers"]] == ["Free inspection"]
    assert "Peak Roofing" in fallback["screening_intro"]
    written = {
        "ai-1": {"text": "You just added two buildings on Elm Street.", "fact_ids": ["f1"]},
        "ai-2": {"text": "Your two older buildings would get spring and fall checks.", "fact_ids": ["f1"]},
    }
    picks = {
        "opening": ["opening-1"],
        "discovery": ["discovery-4", "discovery-1"],
        "offers": ["offers-2", "offers-1"],
        "objections": ["objections-2", "objections-1"],
    }
    script = render(template, lead=lead, seller="Peak Roofing", rep="Sam", picks=picks, written=written)
    assert script["opener"].endswith("You just added two buildings on Elm Street.")
    assert script["discovery_questions"] == ["How old is the building?", "When was the roof last inspected?"]
    assert [o["title"] for o in script["offers"]] == ["Maintenance plan", "Free inspection"]
    assert [c["tags"] for c in script["objection_cards"]] == [["Send an email"], ["Not interested"]]
    unpicked = [o for o in script["sections"][2]["options"] if not o["picked"]]
    assert [o["id"] for o in unpicked] == ["discovery-2", "discovery-3"]


def test_close_out_rules_name_each_problem():
    template = parse_markdown(SCRIPT)
    facts = {"f1": "Hilltop Apartments added two buildings on Elm Street in 2025."}
    row = {"city": "Fresno", "units": "120"}
    good = {
        "opening": ["opening-1"],
        "discovery": ["discovery-1", "discovery-2"],
        "offers": ["offers-1"],
    }
    text = {"ai-1": {"text": "You added two buildings on Elm Street in 2025.", "fact_ids": ["f1"]}}
    assert check_submission(template, good, text, facts, row) == []
    # Agents often "pick" the only version of a fixed section (real run, October 30): harmless.
    fixed = {**good, "screening": ["screening-1"], "close": ["close-1"]}
    assert check_submission(template, fixed, text, facts, row) == []
    assert any(
        "'screening' has no options" in p
        for p in check_submission(template, {**good, "screening": ["x"]}, text, facts, row)
    )
    problems = check_submission(
        template,
        {"opening": ["opening-9"], "discovery": ["discovery-1"] * 2, "offers": []},
        {
            "ai-1": {
                "text": "We guarantee savings of 40% for your 300 units.",
                "fact_ids": ["f9", "row:zip"],
            },
            "ai-2": {"text": "Extra", "fact_ids": ["f1"]},
        },
        facts,
        row,
    )
    joined = " | ".join(problems)
    for expected in (
        "Unknown opening option ids ['opening-9']",
        "distinct option ids",
        "Pick 1 to 2 'offers' options",
        "f9 is not a saved fact",
        "row:zip is not a filled column",
        "'40%' is not in a cited fact",
        "no guarantees or promises",
        "'ai-2'] are not in the options you picked",
    ):
        assert expected in joined, expected
    assert "Write text for ['ai-1']" in " ".join(check_submission(template, good, {}, facts, row))
    banned = {"ai-1": {"text": "Guaranteed savings on Elm Street.", "fact_ids": ["f1"]}}
    assert any("Never say" in p for p in check_submission(template, good, banned, facts, row))
    cited_row = {"ai-1": {"text": "Your 120 units in Fresno add up.", "fact_ids": ["row:units", "row:city"]}}
    assert check_submission(template, good, cited_row, facts, row) == []


def test_one_section_can_be_replaced_for_script_tests():
    template = parse_markdown(SCRIPT)
    assert section_markdown(template, "close") == "Would Tuesday or Thursday work for a 15-minute call?"
    changed = parse_markdown(replace_section(template, "close", "- Does Tuesday work?\n- Or Thursday?"))
    assert [o["text"] for o in changed["sections"][-1]["options"]] == ["Does Tuesday work?", "Or Thursday?"]
    with pytest.raises(ValueError):
        replace_section(template, "gatekeeper", "Who handles roofs?")
