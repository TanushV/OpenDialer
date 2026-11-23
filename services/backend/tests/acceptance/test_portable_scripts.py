"""One generator (docs/ONE_GENERATOR_PLAN.md): the app's own preparation exports finished scripts with their research,
and another workspace imports them as ready, with the same wording, path and why-now."""

import json

from journey import get, import_leads, post, run_jobs

MASTER = """# Paths
- founder: The prospect is a founder.
- sales-leader (default): The prospect leads a sales team.

# Before you dial
Pick the path from the title.

# Triggers
- new_funding: New funding round (180 days, B)

# Opening
Hello {first_name}, {rep} with {seller}.

# The Question
> Ask it, then stop talking.
## Founder question [path: founder]
{{ one line about their company, from research }}
How did you get your first customers?
## Sales question [path: sales-leader]
{{ one line about their company, from research }}
How do your reps run discovery?
## Shared question
What would you change?

# Objections
## Busy
When is better?

# Close
Would Tuesday work?

# Proof notes
> Say only what is true.
"""

PORTABLE = """# Opening
Hey Morgan, it's {rep} from {seller}.

# The Question
I noticed the Series A, congrats.
How do your reps spend the week?

# Close
Worth 20 minutes?
"""

RESEARCH = {
    "summary": "Morgan Co raised a Series A.",
    "trigger": {
        "type": "new_funding",
        "date": "2026-10-01",
        "source_url": "https://morgan.example.test/news",
        "quote": "Morgan Co raised a Series A",
        "evidence": "page",
        "strength": "B",
        "from_row": False,
    },
    "bridge": "New funding usually means more pipeline to cover.",
    "facts": [
        {
            "statement": "Morgan Co raised a Series A.",
            "quote": "Morgan Co raised a Series A",
            "source_url": "https://morgan.example.test/news",
        }
    ],
}


def save_master(ws, markdown=MASTER):
    base = get(ws, "/v1/optimization/script")["base"]
    post(
        ws,
        "/v1/admin/script/import",
        {"markdown": markdown, "expected_version": base["version"], "preview": False},
    )


def prepared(ws, list_name="Authored"):
    save_master(ws)
    import_leads(
        ws,
        [
            {"name": "Avery Stone", "phone": "+14155551940", "title": "Founder", "company": "Avery Co"},
            {"name": "Jordan Lake", "phone": "+14155551941", "title": "Sales Leader", "company": "Jordan Co"},
        ],
        list_name=list_name,
    )
    post(ws, "/v1/preparation/lists", {"list_name": list_name})
    run_jobs(ws, rounds=3)
    return get(ws, f"/v1/preparation/export?list_name={list_name}")


def test_prepared_scripts_export_with_portable_tokens_one_path_and_their_research(workspace):
    ws = workspace
    exported = prepared(ws)
    assert exported["ready"] == 2 and exported["not_ready"] == []
    by_name = {row["name"]: row for row in exported["rows"]}
    founder, leader = by_name["Avery Stone"], by_name["Jordan Lake"]
    assert (founder["script_path"], leader["script_path"]) == ("founder", "sales-leader")
    for row in exported["rows"]:
        script = row["script"]
        assert "{rep}" in script and "{seller}" in script, "The receiving rep's names fill these"
        assert "Before you dial" not in script and "# Triggers" not in script, (
            "Used by generation, not the rep"
        )
        assert "> Say only what is true." in script, "Rep notes stay notes"
        assert row["script_format"] == "1" and row["script_master_digest"] and row["script_generator"]
        research = json.loads(row["script_research"])
        assert research["facts"] and research["facts"][0]["source_url"].startswith("https://")
        assert research["trigger"]["type"] == "none" and research["summary"]
        assert row["csv"]["name"] == row["name"], "The original row rides along for the output file"
        assert row["dropped_by_review"] == [], "Lines the review rejects are reported with the row"
    assert "How did you get your first customers?" in founder["script"]
    assert "How do your reps run discovery?" not in founder["script"], "Only the chosen path"
    assert "What would you change?" in founder["script"], "Shared text stays on every path"


def test_an_exported_file_imports_as_ready_with_the_same_wording_and_research(workspace):
    ws = workspace
    exported = prepared(ws)
    me = get(ws, "/v1/workspace")
    received = import_leads(
        ws,
        [
            {
                **row["csv"],
                "phone": f"+1415555196{index}",
                **{key: row[key] for key in exported["columns"]},
            }
            for index, row in enumerate(exported["rows"])
        ],
        list_name="Received",
    )
    assert (received["imported"], received["scripts_imported"], received["invalid"]) == (2, 2, 0)
    for lead, row in zip(received["leads"], exported["rows"], strict=True):
        packet = get(ws, f"/v1/leads/{lead['id']}/packet")
        script = packet["script"]
        assert packet["preparation"]["status"] == "ready" and script["source"] == "csv"
        assert script["provenance"]["path"] == row["script_path"]
        opener = script["sections"][0]["options"][0]["text"]
        assert me["user"]["name"] in opener and me["seller"]["name"] in opener
        statements = [fact["statement"] for fact in packet["research"]["facts"]]
        assert json.loads(row["script_research"])["facts"][0]["statement"] in statements


def test_imported_research_shows_the_why_now_and_where_it_came_from(workspace):
    ws = workspace
    result = import_leads(
        ws,
        [
            {
                "name": "Morgan Lee",
                "phone": "+14155551970",
                "company": "Morgan Co",
                "script": PORTABLE,
                "script_format": "1",
                "script_research": json.dumps(RESEARCH),
            }
        ],
        list_name="With research",
    )
    assert (result["scripts_imported"], result["invalid"]) == (1, 0)
    packet = get(ws, f"/v1/leads/{result['leads'][0]['id']}/packet")
    assert packet["lead"]["trigger"]["type"] == "new_funding"
    assert packet["lead"]["trigger"]["source_url"] == "https://morgan.example.test/news"
    assert packet["lead"]["trigger_label"] == "B · checked when the script was written"
    fact = next(f for f in packet["research"]["facts"] if f["statement"] == "Morgan Co raised a Series A.")
    assert (
        fact["evidence_quote"] == "Morgan Co raised a Series A" and fact["grounding"]["status"] == "imported"
    )
    assert packet["research"]["sources"][0]["url"] == "https://morgan.example.test/news"


def test_a_bad_research_cell_is_refused_per_row_with_its_reason(workspace):
    ws = workspace
    rows = [
        {"name": "Bad JSON", "phone": "+14155551980", "script": PORTABLE, "script_research": "{not json"},
        {
            "name": "Bad link",
            "phone": "+14155551981",
            "script": PORTABLE,
            "script_research": json.dumps(
                {**RESEARCH, "facts": [{**RESEARCH["facts"][0], "source_url": "ftp://x"}]}
            ),
        },
        {"name": "Plain", "phone": "+14155551982", "script": PORTABLE},
    ]
    result = import_leads(ws, rows, list_name="Research checks")
    assert (result["scripts_imported"], result["invalid"]) == (1, 2)
    assert all("script_research" in error["detail"] for error in result["errors"])
