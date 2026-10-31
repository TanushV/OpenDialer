"""Preparation over HTTP: requested leads, AI routes, business knowledge and script proposals."""

import json
from uuid import uuid4

from journey import get, import_leads, post, run_jobs


def leads(prefix, count):
    return [
        {
            "name": f"{prefix} Person{i}",
            "phone": f"+1415555{prefix.encode().hex()[:2]}{i:02d}",
            "company": f"{prefix} Co {i}",
            "domain": f"{prefix.lower()}{i}.example.test",
            "timezone": "America/Los_Angeles",
        }
        for i in range(count)
    ]


def assert_status(response, status):
    assert response.status_code == status, (response.status_code, response.text)
    return response.json()


def test_selected_leads_are_prepared_on_request(workspace):
    ids = [lead["id"] for lead in import_leads(workspace, leads("Batch", 3))["leads"]]
    # Without the app's prepare flag an API import is callable at once with the base script.
    assert get(workspace, f"/v1/leads/{ids[0]}/packet")["script"]["fallback"] is True
    queued = post(workspace, "/v1/research/batches", {"lead_ids": ids})
    assert queued == {"marked": 3}
    run_jobs(workspace, rounds=2)
    status = get(workspace, "/v1/preparation")
    entry = next(e for e in status["lists"] if e["list_name"] == "")
    assert entry["ready"] == 3 and entry["total"] == 3
    packet = get(workspace, f"/v1/leads/{ids[0]}/packet")
    assert {"script", "research", "prospect_brief", "previous_calls", "tasks"} <= set(packet)
    script = packet["script"]
    assert script["lead_id"] == ids[0] and script["fallback"] is False
    assert "Example Seller" in script["screening_intro"] and script["discovery_questions"]
    assert packet["research"]["facts"][0]["lead_id"] == ids[0]


def playbook(ws, previous, **values):
    body = {
        "name": "Business introduction",
        "opener": "Hi, this is {rep} from Example Seller about roof estimates.",
        "screening_intro": "Example Seller calling about roof estimates.",
        "product_description": "Roof repair estimates for local businesses.",
        "claims": ["Free written estimates"],
        "prohibited_claims": ["Guaranteed savings"],
        "cards": [{"id": "budget", "text": "What would make an estimate useful?", "tags": ["budget"]}],
        "approved": True,
        "reason": "Reviewed business introduction",
        **values,
        "expected_previous_id": previous[0],
        "expected_previous_version": previous[1],
    }
    return ws["client"].post("/v1/admin/playbooks", headers=ws["headers"], json=body)


def test_business_knowledge_versions_and_script_proposals_keep_the_locks(workspace):
    seeded = get(workspace, "/v1/workspace")["active_playbook"]
    assert seeded["approved"] is True
    assert playbook(workspace, (None, 0)).status_code == 409
    first = assert_status(playbook(workspace, (seeded["id"], seeded["version"])), 200)
    assert first["version"] == seeded["version"] + 1 and first["previous_id"] == seeded["id"]
    assert playbook(workspace, (seeded["id"], seeded["version"])).status_code == 409
    stranger = playbook(
        workspace, (first["id"], first["version"]), screening_intro="A stranger calls about roofs."
    )
    assert stranger.status_code == 422
    # Script changes are proposals over one Markdown section, with the same truthful-language rules.
    proposal = {
        "section": "close",
        "text": "- Would a written roof estimate this week help?\n- Is Thursday better?",
        "review_reference": "Reviewed copy for the October test",
    }
    path = "/v1/admin/script/proposals"
    made = assert_status(workspace["client"].post(path, headers=workspace["headers"], json=proposal), 200)
    close = next(s for s in made["template"]["sections"] if s["key"] == "close")
    assert made["status"] == "suggested" and len(close["options"]) == 2
    for changed in (
        {"text": "We guarantee roof savings."},
        {"section": "opening", "text": "Hello there with a roof offer."},
        {"section": "screening", "text": "{rep} from {seller}, about roofs."},
        {"review_reference": "x"},
    ):
        response = workspace["client"].post(path, headers=workspace["headers"], json={**proposal, **changed})
        assert response.status_code == 422, changed


def test_compatible_endpoints_are_configured_without_echoing_keys(workspace):
    settings = get(workspace, "/v1/admin/settings")
    assert settings["effective"]["inference_mode"] == "codex"
    assert settings["effective"]["model"] == "gpt-6.1-terra"
    secret = "sk-acceptance-only-1234"
    body = {
        "request_id": str(uuid4()),
        "expected_version": settings["version"],
        "openai_api_key": secret,
        "inference_mode": "litellm",
        "inference_base_url": "http://127.0.0.1:8081/v1",
    }
    saved = assert_status(
        workspace["client"].put("/v1/admin/settings", headers=workspace["headers"], json=body), 200
    )
    assert saved["configured"]["openai_api_key"] is True
    assert saved["effective"]["inference_mode"] == "litellm"
    assert saved["inference_base_url"] == "http://127.0.0.1:8081/v1"
    assert secret not in json.dumps(saved)
    assert saved["version"] == settings["version"] + 1
    for extra in ({"inference_mode": "chatgpt_plan"}, {"research_search": "openai_web_search"}):
        body = {"request_id": str(uuid4()), "expected_version": saved["version"], **extra}
        assert (
            workspace["client"].put("/v1/admin/settings", headers=workspace["headers"], json=body).status_code
            == 422
        )
    for base_url in (
        "https://user:pw@example.test/v1",
        "http://remote.example.test/v1",
        "ftp://example.test",
    ):
        body = {
            "request_id": str(uuid4()),
            "expected_version": saved["version"],
            "inference_base_url": base_url,
        }
        assert (
            workspace["client"].put("/v1/admin/settings", headers=workspace["headers"], json=body).status_code
            == 422
        )
