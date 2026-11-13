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
    # Import leaves leads callable with the base script until the rep requests preparation.
    assert get(workspace, f"/v1/leads/{ids[0]}/packet")["script"]["fallback"] is True
    queued = post(workspace, "/v1/research/batches", {"lead_ids": ids})
    assert queued == {"marked": 3}
    run_jobs(workspace, rounds=2)
    status = get(workspace, "/v1/preparation")
    entry = next(e for e in status["lists"] if e["list_name"] == "")
    assert entry["ready"] == 3 and entry["total"] - entry["unrequested"] == 3
    packet = get(workspace, f"/v1/leads/{ids[0]}/packet")
    assert {"script", "research", "prospect_brief", "previous_calls", "tasks"} <= set(packet)
    script = packet["script"]
    assert script["lead_id"] == ids[0] and script["fallback"] is False
    assert "Example Seller" in script["screening_intro"] and script["discovery_questions"]
    assert packet["research"]["facts"][0]["lead_id"] == ids[0]
