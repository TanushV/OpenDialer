"""Call insights plan P7: script variants with written hypotheses, assigned by code and tracked on every call.

`# Variants` declares one test: each arm has a share and a hypothesis, and `When:` can limit the comparison to leads
with certain triggers. Options are tagged `## Title [variant: name]`; untagged options are shared. Each company gets
one variant from a stable draw (not the AI), its script keeps only that variant's options, and the variant rides
with the script, the call snapshot, the completed CSV and the statistics.
"""

import base64
import csv
import io
import zipfile

import pytest
from app.preparation.templates import (
    ScriptError,
    assign_variant,
    parse_markdown,
    render,
    variant_in_test,
    with_variant,
)
from conftest import ready
from journey import answered_by_human, get, hang_up, import_leads, post, run_jobs, wrap_up
from test_campaign_prep import save_script

SCRIPT = """# Variants
- direct-open (50%): Hypothesis: naming the reason first gets past the opener more often.
- permission-open (50%): Hypothesis: asking for thirty seconds cuts early hang-ups.

# Opening
## Direct [variant: direct-open]
Hi {first_name}, {rep} with {seller}. I'm calling because teams like yours chase invoices by hand.
## Permission [variant: permission-open]
Hi {first_name}, {rep} with {seller}. Can I have thirty seconds to tell you why I called?

# Pitch
We take invoice follow-up off your team's plate.

# Objections
## Busy
When is better?

# Close
Would Tuesday work?
"""


def lead(identity, company=None, trigger=None):
    return {
        "id": identity,
        "company_key": company,
        "name": "Alex Example",
        **({"trigger": trigger} if trigger else {}),
    }


def test_a_variant_test_parses_with_its_arms_shares_and_hypotheses():
    template = parse_markdown(SCRIPT)
    test = template["variants"]
    assert test["order"] == ["direct-open", "permission-open"]
    assert test["arms"]["permission-open"] == {
        "share": 50,
        "hypothesis": "Hypothesis: asking for thirty seconds cuts early hang-ups.",
    }
    assert len(test["id"]) == 12 and test["when"] == []
    opening = next(section for section in template["sections"] if section["key"] == "opening")
    assert [option["variants"] for option in opening["options"]] == [["direct-open"], ["permission-open"]]
    assert [option["title"] for option in opening["options"]] == ["Direct", "Permission"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("(50%): Hypothesis: asking", "(40%): Hypothesis: asking"), "add up to 100%"),
        (
            ("- permission-open (50%): Hypothesis: asking for thirty seconds cuts early hang-ups.\n", ""),
            "two variants",
        ),
        (("[variant: permission-open]", "[variant: other]"), "Unknown script variants: other"),
        (("## Busy", "## Busy [variant: direct-open]"), "Keep all objections shared"),
        (("- direct-open (50%)", "direct-open 50%"), "Declare each variant"),
    ],
)
def test_a_broken_variant_test_says_what_to_fix(change, message):
    with pytest.raises(ScriptError) as error:
        parse_markdown(SCRIPT.replace(*change))
    assert any(message in problem["message"] for problem in error.value.errors), error.value.errors


def test_every_variant_needs_an_opening_and_tagged_options():
    with pytest.raises(ScriptError) as error:
        parse_markdown(
            SCRIPT.replace("## Permission [variant: permission-open]", "## Permission [variant: direct-open]")
        )
    assert any("Every variant needs this section" in e["message"] for e in error.value.errors)


def test_the_draw_is_stable_by_company_and_follows_the_shares():
    template = parse_markdown(SCRIPT)
    assert assign_variant(template, lead("a", "acme")) == assign_variant(template, lead("b", "acme"))
    drawn = [assign_variant(template, lead(str(i))) for i in range(2000)]
    assert 0.45 < drawn.count("direct-open") / len(drawn) < 0.55
    lopsided = parse_markdown(
        SCRIPT.replace("(50%): Hypothesis: naming", "(90%): Hypothesis: naming").replace(
            "(50%): Hypothesis: asking", "(10%): Hypothesis: asking"
        )
    )
    drawn = [assign_variant(lopsided, lead(str(i))) for i in range(2000)]
    assert 0.86 < drawn.count("direct-open") / len(drawn) < 0.94


def test_a_leads_script_keeps_only_its_variant_and_records_the_test():
    template = parse_markdown(SCRIPT)
    one = lead("x", "acme")
    name = assign_variant(template, one)
    rendered = render(template, lead=one, seller="Example Seller", rep="Sam")
    opening = next(section for section in rendered["sections"] if section["key"] == "opening")
    assert len(opening["options"]) == 1
    assert ("thirty seconds" in opening["options"][0]["text"]) is (name == "permission-open")
    assert rendered["script_variant"] == name and rendered["variant_in_test"] is True
    assert rendered["variant_test"] == template["variants"]["id"]
    assert rendered["variant_hypothesis"].startswith("Hypothesis:")
    other = "direct-open" if name == "permission-open" else "permission-open"
    forced = render(with_variant(template, other), lead=one, seller="Example Seller", rep="Sam")
    assert forced["script_variant"] == other


def test_when_limits_the_comparison_to_listed_triggers():
    template = parse_markdown(SCRIPT.replace("# Opening", "When: hiring_sellers\n\n# Opening"))
    assert template["variants"]["when"] == ["hiring_sellers"]
    assert variant_in_test(template, lead("1", trigger={"type": "hiring_sellers"})) is True
    assert variant_in_test(template, lead("2", trigger={"type": "funding"})) is False
    assert variant_in_test(template, lead("3")) is False


def prepared(ws, count=6):
    save_script(ws, SCRIPT)
    rows = [
        {"name": f"Lead {i}", "phone": f"+1212555{1800 + i}", "company": f"Company {i}", "title": "VP Sales"}
        for i in range(count)
    ]
    imported = import_leads(ws, rows, list_name="Variants")
    assert post(ws, "/v1/preparation/lists", {"list_name": "Variants"})["marked"] == count
    run_jobs(ws, rounds=4)
    return imported["leads"]


def test_generation_writes_each_lead_inside_its_own_variant(workspace):
    leads = prepared(workspace)
    seen = set()
    for item in leads:
        script = get(workspace, f"/v1/leads/{item['id']}/packet")["script"]
        assert not script["fallback"] and script["script_variant"] in {"direct-open", "permission-open"}
        opening = script["sections"][0]["options"]
        assert len(opening) == 1
        assert ("thirty seconds" in opening[0]["text"]) is (script["script_variant"] == "permission-open")
        seen.add(script["script_variant"])
    assert seen == {"direct-open", "permission-open"}, "Six companies at 50/50 draw both arms here"


def test_the_variant_rides_with_the_call_the_export_and_the_comparison(workspace):
    leads = prepared(workspace, count=2)
    ready(workspace)
    reservation = post(
        workspace, "/v1/queue/next", {"campaign_id": workspace["campaign_id"], "list_names": ["Variants"]}
    )
    placed = post(
        workspace,
        "/v1/calls/start",
        {"reservation_id": reservation["reservation_id"], "idempotency_key": "v1"},
    )
    snapshot = get(workspace, f"/v1/calls/{placed['id']}")["script_snapshot"]
    assert snapshot["script_variant"] in {"direct-open", "permission-open"} and snapshot["variant_in_test"]
    answered_by_human(workspace, placed["id"])
    hang_up(workspace, placed["id"])
    wrap_up(workspace, placed["id"], outcome="not_interested", contact_role="right_party")
    run_jobs(workspace)
    archive = base64.b64decode(get(workspace, "/v1/review/call-statistics/export")["archive"])
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        calls = list(csv.DictReader(io.StringIO(bundle.read("calls.csv").decode())))
    row = next(c for c in calls if c["call_id"] == placed["id"])
    assert row["script_variant"] == snapshot["script_variant"] and row["variant_in_test"] == "True"
    review = get(workspace, "/v1/review/variants")
    assert [arm["variant"] for arm in review["arms"]] == ["direct-open", "permission-open"]
    assert all(arm["hypothesis"].startswith("Hypothesis:") for arm in review["arms"])
    tried = next(arm for arm in review["arms"] if arm["variant"] == snapshot["script_variant"])
    assert tried["results"]["dials"] == 1 and tried["results"]["conversations"]["count"] == 1
    assert tried["results"]["enough"] is False, "One conversation is not a result"
    assert len(leads) == 2


def test_the_completed_csv_carries_the_variant_and_imports_it(workspace):
    prepared(workspace, count=2)
    exported = get(workspace, "/v1/preparation/export", params={"list_name": "Variants"})
    assert {row["script_variant"] for row in exported["rows"]} <= {"direct-open", "permission-open"}
    assert all(row["script_variant_test"] for row in exported["rows"])
    received = import_leads(
        workspace,
        [
            {
                **row["csv"],
                "phone": f"+1212555{1900 + index}",
                **{key: row[key] for key in exported["columns"]},
            }
            for index, row in enumerate(exported["rows"])
        ],
        list_name="Received",
    )
    for item, row in zip(received["leads"], exported["rows"], strict=True):
        script = get(workspace, f"/v1/leads/{item['id']}/packet")["script"]
        assert script["script_variant"] == row["script_variant"] and script["variant_in_test"] is True


def test_comparisons_cover_every_dimension(workspace):
    for by in (
        "variant",
        "path",
        "band",
        "list",
        "trigger",
        "talk_share",
        "longest_monologue",
        "response_gap",
    ):
        result = get(workspace, "/v1/review/compare", params={"by": by})
        assert result["by"] == by and result["enough_at"] == 30
    assert get(workspace, "/v1/review/compare", params={"by": "nonsense"}, status=422)
