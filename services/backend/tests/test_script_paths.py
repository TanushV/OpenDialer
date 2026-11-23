"""One generated track with unchanged shared content (work package section 4).

When a section mixes path-tagged and untagged options, the untagged ones are shared: always kept, in authored
order. Picks choose only among the selected path's own alternatives, and the model writes `{{ }}` blocks only
inside them. Masters without paths keep today's picks. No business-specific heading is special.
"""

import pytest
from app.preparation.templates import ScriptError, check_submission, default_picks, parse_markdown, render

MASTER = """# Paths
- leader: a verified sales leader
- founder (default): a founder who sells

# Opening
Hi {first_name}, this is {rep} with {seller}.

# Discovery
## Leader question [path: leader]
{{ one sourced sentence about their team }} How do you run calls today?

## Founder question [path: founder]
Who takes the first calls today?

## Their answer
Listen for who owns calls and how often.

# Where it hurts
## Leader pain [path: leader]
Ramp time for new reps.

## Founder pain [path: founder]
Time away from building.

## They pick one
Dig into the one they chose.

# Close
## Leader close [path: leader]
Would a short look next week help your team?

## Founder close [path: founder]
Would a short look next week save you time?

## Meeting times and invite
Offer two times and confirm the email for the invite.

# Objections
## Not interested
Fair enough. Is that because it is handled?

## Send me an email
Happy to. What should it cover?
"""


def rendered(template, path, picks=None, written=None):
    script = render(
        template, lead={"name": "Pat Lee"}, seller="Acme", rep="Sam", path=path, picks=picks, written=written
    )
    return {
        section["key"]: [option["title"] or option["text"] for option in section.get("options", [])]
        for section in script["sections"]
        if section.get("spoken") is not False
    }


@pytest.mark.parametrize("path", ["leader", "founder"])
def test_a_path_keeps_its_own_options_and_every_shared_one_in_authored_order(path):
    template = parse_markdown(MASTER)
    title = "Leader" if path == "leader" else "Founder"
    sections = rendered(template, path)
    assert sections["discovery"] == [f"{title} question", "Their answer"]
    assert sections["custom:where-it-hurts"] == [f"{title} pain", "They pick one"]
    assert sections["close"] == [f"{title} close", "Meeting times and invite"]
    assert sections["objections"] == ["Not interested", "Send me an email"]
    assert list(sections) == ["opening", "discovery", "custom:where-it-hurts", "close", "objections"]


def test_picks_choose_only_the_paths_alternatives_and_shared_options_need_no_pick():
    template = parse_markdown(MASTER)
    sections = {s["key"]: s for s in template["sections"]}
    close = {o["title"]: o["id"] for o in sections["close"]["options"]}
    discovery = {o["title"]: o["id"] for o in sections["discovery"]["options"]}
    block = next(iter({b["id"] for b in template["blocks"]}))
    written = {block: {"text": "Your team grew this year.", "fact_ids": ["row:company"]}}
    row = {"company": "Northwind"}
    errors = check_submission(
        template,
        {"close": [close["Leader close"]], "discovery": [discovery["Leader question"]]},
        written,
        {},
        row,
        path="leader",
    )
    assert errors == []
    shared = check_submission(
        template,
        {"close": [close["Meeting times and invite"]], "discovery": [discovery["Leader question"]]},
        written,
        {},
        row,
        path="leader",
    )
    assert any("close" in error for error in shared), "A shared option is never a pick"


def test_the_model_writes_blocks_only_inside_the_chosen_path():
    template = parse_markdown(MASTER)
    picks = default_picks({**template, "sections": template["sections"]})
    errors = check_submission(template, {}, {}, {}, {}, path="founder")
    assert not any("Write text" in error for error in errors), "The founder path has no blocks to write"
    errors = check_submission(template, {}, {}, {}, {}, path="leader")
    assert any("Write text" in error for error in errors), "The leader path's own block must be written"
    assert picks is not None


def test_a_block_inside_shared_content_is_a_line_specific_format_error():
    broken = MASTER.replace(
        "Listen for who owns calls and how often.",
        "{{ say something about them }} Listen for who owns calls.",
    )
    with pytest.raises(ScriptError) as error:
        parse_markdown(broken)
    (problem,) = [e for e in error.value.errors if "Shared text" in e["message"]]
    assert problem["line"] == broken.splitlines().index("## Their answer") + 1
    assert "[path:" in problem["message"]


def test_renamed_and_reordered_custom_sections_follow_the_same_rule():
    reordered = MASTER.replace("# Where it hurts", "# Pain points").replace(
        "# Objections", "# Common objections"
    )
    template = parse_markdown(reordered)
    sections = rendered(template, "founder")
    assert sections["custom:pain-points"] == ["Founder pain", "They pick one"]


def test_a_master_without_paths_keeps_todays_picks():
    plain = """# Opening
Hi {first_name}, this is {rep} with {seller}.

# Discovery
- One?
- Two?
- Three?
- Four?

# Close
Would next week work?
"""
    template = parse_markdown(plain)
    discovery = next(s for s in template["sections"] if s["key"] == "discovery")
    assert discovery["pick"] == [1, 3]


def test_the_preview_states_what_each_leads_script_keeps_and_picks():
    from app.preparation.templates import pick_notes

    notes = pick_notes(parse_markdown(MASTER))
    assert any("one path: leader, founder (default)" in n for n in notes)
    assert any(n.startswith("Close: kept as written on every path: Meeting times and invite") for n in notes)
    plain = parse_markdown(
        "# Opening\nHi {first_name}, this is {rep} with {seller}.\n\n# Discovery\n- One?\n- Two?\n- Three?\n- Four?\n\n"
        "# Close\nWould next week work?\n"
    )
    assert "Discovery: the AI picks up to 3 of your 4 per lead; the rest are left out." in pick_notes(plain)
