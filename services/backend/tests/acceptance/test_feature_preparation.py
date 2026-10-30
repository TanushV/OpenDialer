"""Preparation and research over HTTP: batches, runway, seller templates, playbooks, variants and model routes."""

import json
from uuid import uuid4

from journey import get, import_leads, post, run_jobs

TEMPLATE = {
    "topic": "roof repair estimates",
    "opener": "Hi {name}, {rep} from {seller} here.",
    "gatekeeper": "Who handles {topic} for {company}?",
    "screening_intro": "{rep} from {seller}, calling about {topic}.",
    "discovery_questions": ["How are you handling {topic} today?"],
    "proof": "{proof}",
    "closing_question": "Would a short call help?",
    "final_voicemail_asset": None,
}


def leads(prefix, count):
    return [
        {
            "name": f"{prefix}{i}",
            "phone": f"+1415555{prefix.encode().hex()[:2]}{i:02d}",
            "company": f"{prefix} Co {i}",
            "domain": f"{prefix.lower()}{i}.example.test",
            "timezone": "America/Los_Angeles",
            "region": "415",
        }
        for i in range(count)
    ]


def save_policy(ws, **values):
    current = get(ws, "/v1/admin/operating-policy")
    body = {"request_id": str(uuid4()), "expected_version": current["version"], **values}
    return ws["client"].put("/v1/admin/operating-policy", headers=ws["headers"], json=body)


def assert_status(response, status):
    assert response.status_code == status, (response.status_code, response.text)
    return response.json()


def test_research_batch_prepares_named_leads_and_reports_runway(workspace):
    ids = [lead["id"] for lead in import_leads(workspace, leads("Batch", 5))["leads"]]
    queued = post(workspace, "/v1/research/batches", {"lead_ids": ids})
    assert queued["work_units"] == 1 and len(queued["jobs"]) == 1
    run_jobs(workspace)
    status = get(workspace, "/v1/preparation")
    assert status["fallback"] == 5 and status["ready"] == 0 and status["failed"] == 0
    assert status["prepared"] == status["runway_attempts"] == 5
    assert status["queued"] == 0 and status["target"] == 20
    assert status["inspected_leads"] == 8 and status["enabled"] is True
    packet = get(workspace, f"/v1/leads/{ids[0]}/packet")
    assert {"script", "research", "prospect_brief", "previous_calls", "tasks"} <= set(packet)
    script = packet["script"]
    assert script["lead_id"] == ids[0] and script["opener"] and script["gatekeeper"]
    assert "Example Seller" in script["screening_intro"] and script["discovery_questions"]


def test_advance_and_off_hours_windows_follow_the_saved_schedule(workspace):
    response = assert_status(save_policy(workspace, preparation={"enabled": False}), 200)
    assert response["preparation"]["enabled"] is False
    assert get(workspace, "/v1/preparation")["enabled"] is False
    response = assert_status(
        save_policy(
            workspace,
            preparation={"enabled": True, "off_hours_start": 0, "off_hours_end": 1, "timezone": "UTC"},
        ),
        200,
    )
    assert response["preparation"]["off_hours_end"] == 1
    assert get(workspace, "/v1/preparation")["within_schedule"] is False
    assert_status(
        save_policy(workspace, preparation={"off_hours_start": 0, "off_hours_end": 24, "timezone": "UTC"}),
        200,
    )
    assert get(workspace, "/v1/preparation")["within_schedule"] is True
    assert_status(save_policy(workspace, preparation={"timezone": "Mars/Olympus"}), 422)
    assert_status(save_policy(workspace, preparation={"ahead_count": 0}), 422)


def test_seller_templates_render_gatekeeper_and_iphone_screening(workspace):
    before = get(workspace, "/v1/admin/operating-policy")
    saved = assert_status(save_policy(workspace, script_template=TEMPLATE), 200)
    assert saved["script_template"]["gatekeeper"] == TEMPLATE["gatekeeper"]
    assert saved["script_template"]["screening_intro"] == TEMPLATE["screening_intro"]
    assert saved["version"] == before["version"] + 1
    assert saved["template_version"] != before["template_version"]
    assert_status(
        save_policy(workspace, script_template={**TEMPLATE, "screening_intro": "Calling about {topic}."}), 422
    )
    assert_status(save_policy(workspace, script_template={**TEMPLATE, "gatekeeper": "Hi {secret}"}), 422)
    ids = [lead["id"] for lead in import_leads(workspace, leads("Screen", 1))["leads"]]
    post(workspace, "/v1/research/batches", {"lead_ids": ids})
    run_jobs(workspace)
    script = get(workspace, f"/v1/leads/{ids[0]}/packet")["script"]
    assert script["template_version"] == saved["template_version"]
    assert script["opener"] == "Hi Screen0, Local operator from Example Seller here."
    assert script["gatekeeper"] == "Who handles roof repair estimates for Screen Co 0?"
    assert (
        script["screening_intro"]
        == "Local operator from Example Seller, calling about roof repair estimates."
    )
    assert script["discovery_questions"] == ["How are you handling roof repair estimates today?"]
    assert script["closing_question"] == "Would a short call help?"
    assert get(workspace, "/v1/preparation")["fallback"] == 1


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


def test_playbooks_and_script_changes_require_reviewed_truthful_language(workspace):
    seeded = get(workspace, "/v1/workspace")["active_playbook"]
    assert seeded["approved"] is True
    assert playbook(workspace, (None, 0)).status_code == 409
    first = assert_status(playbook(workspace, (seeded["id"], seeded["version"])), 200)
    assert first["version"] == seeded["version"] + 1 and first["previous_id"] == seeded["id"]
    assert get(workspace, "/v1/workspace")["active_playbook"]["id"] == first["id"]
    assert playbook(workspace, (seeded["id"], seeded["version"])).status_code == 409
    second = assert_status(
        playbook(
            workspace, (first["id"], first["version"]), opener="Hi {rep}, Example Seller calls about roofs."
        ),
        200,
    )
    assert second["version"] == first["version"] + 1 and second["previous_id"] == first["id"]
    active = get(workspace, "/v1/workspace")["active_playbook"]
    assert active["id"] == second["id"] and active["opener"] == "Hi {rep}, Example Seller calls about roofs."
    stranger = playbook(
        workspace, (second["id"], second["version"]), screening_intro="A stranger calls about roofs."
    )
    assert stranger.status_code == 422
    # Script wording changes are proposals with the same truthful-language rules (script learning).
    assert_status(save_policy(workspace, script_template=TEMPLATE), 200)
    proposal = {
        "section": "opener",
        "text": "Hi {name}, {rep} from {seller} with written roof estimates.",
        "review_reference": "Reviewed copy for the October test",
    }
    path = "/v1/admin/script/proposals"
    made = assert_status(workspace["client"].post(path, headers=workspace["headers"], json=proposal), 200)
    assert made["status"] == "suggested" and made["template"]["opener"] == proposal["text"]
    for changed in (
        {"text": "We guarantee roof savings from {seller}, {rep} speaking."},
        {"text": "Hello there with a roof offer."},
        {"review_reference": "x"},
    ):
        response = workspace["client"].post(path, headers=workspace["headers"], json={**proposal, **changed})
        assert response.status_code == 422


def test_chatgpt_plan_routes_refuse_what_cannot_be_verified_offline(workspace):
    started = assert_status(
        workspace["client"].post(
            "/v1/inference/chatgpt/start", headers={**workspace["headers"], "Host": "127.0.0.1:8000"}
        ),
        200,
    )
    assert started["authorization_url"].startswith("https://") and started["expires_in"] == 600
    assert started["completion"] == "loopback"
    models = workspace["client"].get("/v1/inference/chatgpt/models", headers=workspace["headers"])
    assert models.status_code == 409
    address = "http://evil.example.test/auth/callback?code=x&state=y"
    complete = workspace["client"].post(
        "/v1/inference/chatgpt/complete", headers=workspace["headers"], json={"redirect_url": address}
    )
    assert complete.status_code == 422
    wrong = "http://127.0.0.1:1455/auth/callback?code=x&state=not-the-pending-state"
    complete = workspace["client"].post(
        "/v1/inference/chatgpt/complete", headers=workspace["headers"], json={"redirect_url": wrong}
    )
    assert complete.status_code == 400
    imported = workspace["client"].post("/v1/inference/chatgpt/import", headers=workspace["headers"], json={})
    assert imported.status_code == 422
    dynamic = {
        "client_id": "dynamic_agent_client",
        "access_token": "a",
        "id_token": "b",
        "subject": "s",
        "scopes": [],
    }
    imported = workspace["client"].post(
        "/v1/inference/chatgpt/import", headers=workspace["headers"], json=dynamic
    )
    assert imported.status_code == 422
    token = {"chatgpt_access_token": "plan-token-not-real", "request_id": str(uuid4()), "expected_version": 0}
    rejected = workspace["client"].put("/v1/admin/settings", headers=workspace["headers"], json=token)
    assert rejected.status_code == 422 and "plan-token-not-real" not in rejected.text


def test_compatible_endpoints_and_search_are_configured_without_echoing_keys(workspace):
    settings = get(workspace, "/v1/admin/settings")
    secret = "sk-acceptance-only-1234"
    body = {
        "request_id": str(uuid4()),
        "expected_version": settings["version"],
        "openai_api_key": secret,
        "research_search": "openai_web_search",
        "inference_mode": "litellm",
        "inference_base_url": "http://127.0.0.1:8081/v1",
    }
    saved = assert_status(
        workspace["client"].put("/v1/admin/settings", headers=workspace["headers"], json=body), 200
    )
    assert saved["configured"]["openai_api_key"] is True
    assert saved["effective"]["research_search"] == "openai_web_search"
    assert saved["effective"]["inference_mode"] == "litellm"
    assert saved["inference_base_url"] == "http://127.0.0.1:8081/v1"
    assert secret not in json.dumps(saved)
    assert saved["version"] == settings["version"] + 1
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


SITE = {
    "https://acme.example.test/": (301, "", {"location": "https://www.acme.example.test/"}),
    "https://www.acme.example.test/": (
        200,
        "<title>Acme Repair</title><script>steal()</script>"
        '<a href="/services">Our services</a> <a href="/about-us">About the company</a>'
        ' <a href="/careers">Careers</a> <a href="https://other.example.test/services">Partner</a>'
        ' <a href="#top">Top</a> <a href="/about-us#team">Team</a>',
        {"content-type": "text/html; charset=utf-8"},
    ),
    "https://www.acme.example.test/services": (
        200,
        "<title>Services</title><p>We repair commercial refrigeration units across the Bay Area.</p>",
        {"content-type": "text/html"},
    ),
    "https://www.acme.example.test/about-us": (
        200,
        "<title>About</title><p>Acme Repair was founded in 1998 in Oakland.</p>",
        {"content-type": "text/html"},
    ),
    "https://brochure.example.test/": (200, "%PDF-1.7", {"content-type": "application/pdf"}),
}


class ResearchModel:
    """Research extraction and its grounding review, answered from the fetched sources."""

    billing, search_enabled = "openai", False

    async def complete(self, instructions, payload, schema):
        from app.providers.inference import InferenceResult

        usage = {"cost": 0.001}
        if "facts" in schema.get("properties", {}):
            by_url = {source["url"]: source["id"] for source in payload["sources"]}
            services, about = by_url.get(f"{BASE}services"), by_url.get(f"{BASE}about-us")
            facts = [
                fact(
                    "Repairs commercial refrigeration units", services, "We repair commercial refrigeration"
                ),
                fact("Founded in 1998", about, "founded in 1998"),
                fact("Employs 500 technicians", services, "500 technicians"),  # Not in the source.
                fact("Serves Sacramento", "unknown-source", "Sacramento"),
            ]
            return InferenceResult({"facts": facts, "unknowns": []}, usage, str(uuid4()))
        if "fields" in payload and "fingerprint" in payload:
            verdicts = [
                {"field": f["field"], "verdict": "SUPPORTED", "evidence": f["evidence"]}
                for f in payload["fields"]
            ]
            return InferenceResult(
                {"fingerprint": payload["fingerprint"], "verdicts": verdicts}, usage, str(uuid4())
            )
        return InferenceResult({}, usage, str(uuid4()))  # Script writing falls back to the template.


BASE = "https://www.acme.example.test/"


def fact(statement, source_id, quote):
    return {
        "statement": statement,
        "source_id": source_id,
        "quote": quote,
        "stability": "stable",
        "conflicts": [],
    }


def test_official_site_research_follows_a_small_official_set_and_keeps_only_quoted_facts(
    workspace, app, monkeypatch
):
    from unittest.mock import AsyncMock

    import app.research.fetch as fetch
    import httpx

    monkeypatch.setattr(fetch, "public_address", AsyncMock(return_value="93.184.215.14"))
    requested = []

    def handler(request):
        url = str(request.url.copy_with(host=request.headers["host"], path=request.url.path or "/"))
        requested.append(url)
        status, body, headers = SITE.get(url, (404, "", {}))
        return httpx.Response(status, text=body, headers=headers)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        fetch.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs)
    )
    service = app.state.service
    model = ResearchModel()
    monkeypatch.setattr(service, "inference", lambda *args, **kwargs: model)
    monkeypatch.setattr(service.settings, "simulation", False)
    rows = [
        {**leads("Acme", 1)[0], "company": "Acme Repair", "domain": "acme.example.test"},
        {**leads("Brochure", 1)[0], "domain": "brochure.example.test"},
    ]
    acme, brochure = [lead["id"] for lead in import_leads(workspace, rows)["leads"]]
    post(workspace, "/v1/research/batches", {"lead_ids": [acme, brochure]})
    run_jobs(workspace)
    # The imported domain and its www name only; two ranked pages; no careers, partner or repeats.
    site = [url for url in requested if "acme.example.test" in url]
    assert site[:2] == ["https://acme.example.test/", BASE]
    assert sorted(site[2:]) == [f"{BASE}about-us", f"{BASE}services"]
    assert not any("careers" in url or "other.example.test" in url for url in requested)
    assert [url for url in requested if "brochure" in url] == ["https://brochure.example.test/"]
    research = get(workspace, f"/v1/leads/{acme}/packet")["research"]
    facts = {f["statement"]: f for f in research["facts"]}
    assert set(facts) == {"Repairs commercial refrigeration units", "Founded in 1998"}
    assert facts["Founded in 1998"]["source_url"] == f"{BASE}about-us"
    assert all(f["entity_match"] == "explicit_imported_company_domain" for f in facts.values())
    assert all(f["status"] == "supported" and not f["synthetic"] for f in facts.values())
    sources = {s["url"]: s for s in research["sources"]}
    assert set(sources) == {f"{BASE}services", f"{BASE}about-us"}
    assert "Bay Area" in sources[f"{BASE}services"]["text"]
    # A source that cannot be read is a failed attempt, not an empty success.
    assert get(workspace, f"/v1/leads/{brochure}/packet")["research"]["facts"] == []
    assert get(workspace, "/v1/preparation")["failed"] >= 1
