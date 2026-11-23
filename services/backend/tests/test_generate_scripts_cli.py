"""The command-line run of the app's own preparation (docs/ONE_GENERATOR_PLAN.md, Step 1.1), offline."""

import csv
import json
import os

import pytest
from app.preparation import batch

MASTER = """# Paths
- founder: The prospect is a founder.
- sales-leader (default): The prospect leads a sales team.

# Before you dial
Pick the path from the title.

# Opening
Hello {first_name}, {rep} with {seller}.

# The Question
## Founder question [path: founder]
{{ one line about their company, from research }}
How did you get your first customers?
## Sales question [path: sales-leader]
{{ one line about their company, from research }}
How do your reps run discovery?

# Objections
## Busy
When is better?

# Close
Would Tuesday work?
"""

FINISHED = "# Opening\nHey Morgan, it's {rep} from {seller}.\n\n# Close\nWorth 20 minutes?"


def write(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["name", "phone", "company", "title", "script"])
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def own_key(monkeypatch):
    # The run points the secret key at its work folder; keep that change inside this test.
    for name in ("OPENDIALER_SECRET_KEY", "OPENDIALER_SECRET_KEY_FILE"):
        if name in os.environ:
            monkeypatch.setenv(name, os.environ[name])
        else:
            monkeypatch.setenv(name, "")
            monkeypatch.delenv(name)


def test_a_lead_csv_becomes_finished_scripts_that_another_workspace_imports(tmp_path, own_key, workspace):
    source, master = tmp_path / "leads.csv", tmp_path / "master.md"
    master.write_text(MASTER, encoding="utf-8")
    write(
        source,
        [
            {"name": "Avery Stone", "phone": "+14155552040", "company": "Avery Co", "title": "Founder"},
            {
                "name": "Jordan Lake",
                "phone": "(415) 555-2041",
                "company": "Jordan Co",
                "title": "Sales Leader",
            },
            {
                "name": "Morgan Kept",
                "phone": "+14155552042",
                "company": "Kept Co",
                "title": "CEO",
                "script": FINISHED,
            },
        ],
    )
    command = [str(source), "--master", str(master), "--simulation", "--retry-wait", "0"]
    assert batch.main(command) == 0
    output = tmp_path / "leads-scripts.csv"
    rows = list(csv.DictReader(output.open(encoding="utf-8-sig")))
    assert [row["name"] for row in rows] == ["Avery Stone", "Jordan Lake", "Morgan Kept"], "Input order kept"
    founder, leader, kept = rows
    assert (founder["script_path"], leader["script_path"]) == ("founder", "sales-leader")
    assert "How did you get your first customers?" in founder["script"] and "{rep}" in founder["script"]
    assert "Before you dial" not in founder["script"]
    assert json.loads(founder["script_research"])["facts"], "Research travels with the script"
    assert kept["script"] == FINISHED and not kept["script_research"], (
        "A finished row passes through as it was"
    )

    assert batch.main(command) == 0, "Running the same command again continues the same work"
    assert list(csv.DictReader(output.open(encoding="utf-8-sig"))) == rows

    master.write_text(MASTER.replace("Would Tuesday work?", "Would Monday work?"), encoding="utf-8")
    with pytest.raises(ValueError, match="--restart"):
        batch.main(command)

    received = (
        workspace["client"]
        .post(
            "/v1/leads/import",
            headers=workspace["headers"],
            json={
                "campaign_id": workspace["campaign_id"],
                "list_name": "Received",
                "rows": [{**row, "phone": f"+1415555206{index}"} for index, row in enumerate(rows)],
            },
        )
        .json()
    )
    assert (received["imported"], received["scripts_imported"], received["invalid"]) == (3, 3, 0)


def test_a_rotated_login_goes_back_to_the_codex_cli_unless_it_signed_in_again(tmp_path, own_key):
    """Codex may rotate the ChatGPT login mid-run; the CLI's copy must not be left with a used refresh token."""
    from app.platform.secret_files import write_private
    from app.providers.codex import profile_home

    work, codex = tmp_path / "work", tmp_path / "codex"
    work.mkdir()
    codex.mkdir()
    os.environ.pop("OPENDIALER_SECRET_KEY", None)  # The run uses its own key file, as batch.main does.
    os.environ["OPENDIALER_SECRET_KEY_FILE"] = str(batch.key_file(work))
    tokens = {"access_token": "a1", "refresh_token": "r1", "id_token": "i1", "account_id": "acct"}
    original = {"auth_mode": "chatgpt", "tokens": tokens, "last_refresh": "2026-11-23T10:00:00Z", "extra": 1}
    (codex / "auth.json").write_text(json.dumps(original))
    home = profile_home(work, "user-1")
    home.mkdir(parents=True)
    rotated = {
        **original,
        "tokens": {**tokens, "refresh_token": "r2"},
        "last_refresh": "2026-11-23T11:00:00Z",
    }
    write_private(home / "auth.json", rotated)

    batch.write_back_login(work, "user-1", codex, original)
    saved = json.loads((codex / "auth.json").read_text())
    assert saved["tokens"]["refresh_token"] == "r2" and saved["extra"] == 1, "Newer tokens, other fields kept"
    assert oct((codex / "auth.json").stat().st_mode & 0o777) == "0o600"

    signed_in_again = {**original, "tokens": {**tokens, "refresh_token": "r9"}}
    (codex / "auth.json").write_text(json.dumps(signed_in_again))
    batch.write_back_login(work, "user-1", codex, original)
    assert json.loads((codex / "auth.json").read_text())["tokens"]["refresh_token"] == "r9"
