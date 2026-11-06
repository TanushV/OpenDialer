"""The owner-authored playbook keeps cues separate and accepts a useful small structure."""

from app.preparation.templates import parse_markdown, render, template_version

PLAYBOOK = """# Goal
Earn a 20 minute conversation.

# Instructions
Use verified triggers. Stop at a firm no.

# Before you dial
Choose one verified workload trigger.

# Opener
## Permission
Hi {first_name}, {rep} from {seller}.
> Pause for permission. If busy, offer a callback.

# Problem and outcome
## Capacity
{{ one short bridge from verified research }} We take over the work around the sale.
> Do not use the pilot number without approved measurement details.

## Alternative
A shorter explanation.

# Discovery
What takes most of the team's time?
> Skip this if they already answered.

# Objections
## Email
Happy to. What should it cover?
> If they only want the email, finish the call.

# Close
## Meeting
Worth 20 minutes?

# Extra reference
- First checklist item
- Second checklist item
"""


def test_playbook_aliases_options_context_and_cues_are_kept_separate():
    template = parse_markdown(PLAYBOOK)
    sections = {s["key"]: s for s in template["sections"]}
    assert sections["pitch"]["pick"] == [1, 1]
    assert len(sections["pitch"]["options"]) == 2
    assert sections["opening"]["options"][0]["notes"].startswith("Pause for permission")
    assert "workload trigger" in template["context"]["preparation"]
    assert "firm no" in template["context"]["instructions"]
    assert len(sections["discovery"]["options"]) == 1
    custom = next(s for s in template["sections"] if s["title"] == "Extra reference")
    assert len(custom["options"]) == 1 and "Second checklist" in custom["options"][0]["text"]
    script = render(template, lead={"name": "Dana"}, seller="Seller", rep="Sam")
    assert "Seller" in script["screening_intro"]
    assert "Pause" not in script["opener"]
    assert script["sections"][0]["options"][0]["notes"]


def test_screening_and_discovery_are_optional_and_defaults_are_explicit():
    template = parse_markdown("# Opening\nHi {first_name}, {rep} from {seller}.\n# Close\nWorth a look?")
    assert (
        render(template, lead={}, seller="Agite", rep="Sam")["screening_intro"]
        == "Sam from Agite, calling with a business introduction."
    )
    assert not render(template, lead={}, seller="Agite", rep="Sam")["discovery_questions"]


def test_formatting_changes_keep_the_generation_key_but_meaning_changes_do_not():
    original = parse_markdown(PLAYBOOK)
    formatted = parse_markdown(PLAYBOOK.replace("# Opener", "# Opening").replace("\n\n", "\n\n\n"))
    assert template_version(original) == template_version(formatted)
    assert template_version(original) != template_version(
        parse_markdown(PLAYBOOK.replace("Worth 20 minutes?", "Worth a short review?"))
    )
