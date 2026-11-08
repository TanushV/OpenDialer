"""Preparation over HTTP: requested leads, AI routes, business knowledge and script proposals."""

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


def test_business_knowledge_versions_keep_the_locks(workspace):
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
