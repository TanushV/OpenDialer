"""Uploaded scripts stay readable and versioned in both authority modes."""

from journey import call, get, post


def test_markdown_script_preview_adopt_and_stale_edit(workspace):
    ws = workspace
    base = get(ws, "/v1/optimization/script")["base"]
    body = {
        "markdown": "# My script\n\n## Opener\nHi {name}, this is {rep} from {seller}.\n\n## Objection: busy\nWhen would be better?\n\n## Offer\nLet's look at your current process.",
        "expected_version": base["version"],
        "preview": True,
    }
    preview = post(ws, "/v1/admin/script/import", body)
    assert [s["title"] for s in preview["template"]["sections"]] == ["Opener", "Objection: busy", "Offer"]
    assert get(ws, "/v1/optimization/script")["base"] == base
    saved = post(ws, "/v1/admin/script/import", {**body, "preview": False})
    assert saved["version"] == base["version"] + 1
    lead = get(ws, "/v1/workspace")["leads"][0]
    packet = get(ws, f"/v1/leads/{lead['id']}/packet")
    assert packet["script"]["sections"][1]["text"] == "When would be better?"
    assert "{name}" not in packet["script"]["sections"][0]["text"]
    call(ws, "POST", "/v1/admin/script/import", status=409, json={**body, "preview": False})


def test_script_import_rejects_unsafe_placeholders(workspace):
    base = get(workspace, "/v1/optimization/script")["base"]
    call(
        workspace,
        "POST",
        "/v1/admin/script/import",
        status=422,
        json={
            "markdown": "## Opener\n{rep.__class__}",
            "expected_version": base["version"],
            "preview": False,
        },
    )
