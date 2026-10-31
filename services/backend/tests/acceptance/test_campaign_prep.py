"""One pasted script, per-lead preparation before calling, company-aware calling and the per-list budget."""

from datetime import timedelta
from uuid import uuid4

from conftest import ready
from journey import call, get, hang_up, import_leads, post, run_jobs, wrap_up

SCRIPT = """# Opening
Hi {first_name}, this is {rep} with {seller}. {{ one line on why now, from research }}

# Screening
{rep} from {seller}, calling about roof care.

# Discovery
- When was the roof last checked?
- Who handles repairs?
- What happens when it leaks?
- How old are the buildings?

# Offers
## Inspection
A free 20-minute inspection.

## Plan
{{ how a maintenance plan fits them }}

# Objections
## Busy
When is better?

# Close
- Would Tuesday work?
- Would Thursday work?

# Never say
- Guaranteed savings
"""


def leads(prefix, count, **extra):
    return [
        {
            "name": f"{prefix} Person{i}",
            "phone": f"+1415555{3000 + i + 100 * len(prefix)}",
            "company": f"{prefix} Co {i}",
            "timezone": "America/Los_Angeles",
            "city": "Fresno",
            "units": str(10 + i),
            **extra,
        }
        for i in range(count)
    ]


def lead_row(ws, lead_id):
    return next(item for item in get(ws, "/v1/workspace/leads")["items"] if item["id"] == lead_id)


def save_script(ws, markdown=SCRIPT):
    base = get(ws, "/v1/optimization/script")["base"]
    return post(
        ws,
        "/v1/admin/script/import",
        {"markdown": markdown, "expected_version": base["version"], "preview": False},
    )


def test_pasted_script_is_parsed_with_every_problem_listed(workspace):
    ws = workspace
    base = get(ws, "/v1/optimization/script")["base"]
    assert base["template"]["format"] == 2  # the simulated workspace starts from the starter script
    broken = post(
        ws,
        "/v1/admin/script/import",
        {
            "markdown": "# Opening\nHello\n# Pitch\nBuy\n",
            "expected_version": base["version"],
            "preview": True,
        },
    )
    assert broken["template"] is None
    messages = " | ".join(f"{e['line']}:{e['message']}" for e in broken["errors"])
    assert "1:Every Opening must say who is calling" in messages and "Unknown section '# Pitch'" in messages
    assert "Add a '# Screening' section" in messages and "Add a '# Close' section" in messages
    refused = call(
        ws,
        "POST",
        "/v1/admin/script/import",
        status=422,
        json={"markdown": "# Opening\nHello\n", "expected_version": base["version"], "preview": False},
    )
    assert refused["detail"]["code"] == "SCRIPT_INVALID" and refused["detail"]["errors"]
    preview = post(
        ws,
        "/v1/admin/script/import",
        {"markdown": SCRIPT, "expected_version": base["version"], "preview": True},
    )
    assert preview["errors"] == [] and [b["id"] for b in preview["template"]["blocks"]] == ["ai-1", "ai-2"]
    saved = save_script(ws)
    assert saved["version"] == base["version"] + 1
    call(
        ws,
        "POST",
        "/v1/admin/script/import",
        status=409,
        json={"markdown": SCRIPT, "expected_version": base["version"], "preview": False},
    )


def test_imported_lists_are_researched_and_scripted_before_they_can_be_called(workspace):
    ws = workspace
    save_script(ws)
    call(ws, "PUT", "/v1/admin/preparation", json={"leads_per_agent": 2, "agents_at_once": 1})
    imported = import_leads(ws, leads("Prep", 5), list_name="October roofs", prepare=True)
    assert imported["imported"] == 5 and imported["preparing"] == 5
    ids = [lead["id"] for lead in imported["leads"]]
    assert {lead_row(ws, i)["status"] for i in ids} == {"preparing"}
    status = get(ws, "/v1/preparation")
    entry = next(e for e in status["lists"] if e["list_name"] == "October roofs")
    assert entry["total"] == 5 and entry["working"] == 2 and entry["waiting"] == 3
    assert len(status["threads"]) == 1 and status["threads"][0]["leads"] == 2
    assert status["has_script"] and status["ai_connected"]
    # The queue never offers a lead whose preparation has not finished.
    offered = set()
    for _ in range(3):
        ready(ws)
        response = ws["client"].post(
            "/v1/queue/next", headers=ws["headers"], json={"campaign_id": ws["campaign_id"]}
        )
        if response.status_code != 200:
            break
        offered.add(response.json()["lead"]["id"])
        post(ws, "/v1/rep/presence", {"status": "paused"})
    assert not offered & set(ids)
    run_jobs(ws, rounds=4)
    status = get(ws, "/v1/preparation")
    entry = next(e for e in status["lists"] if e["list_name"] == "October roofs")
    assert entry["ready"] == 5 and entry["working"] == 0 and entry["waiting"] == 0
    assert entry["spent_usd"] == 0 and entry["budget_usd"] == 25
    assert {lead_row(ws, i)["status"] for i in ids} == {"eligible"}
    packet = get(ws, f"/v1/leads/{ids[0]}/packet")
    script = packet["script"]
    assert script["fallback"] is False and script["preparation_status"] == "ready"
    assert script["written"]["ai-1"]["text"] and script["written"]["ai-1"]["fact_ids"]
    assert script["opener"].startswith("Hi Prep, this is Local operator with Example Seller.")
    assert len(script["discovery_questions"]) == 3 and [o["title"] for o in script["offers"]][0]
    assert packet["research"]["facts"][0]["source_url"].startswith("https://")
    assert script["picks"]["close"] == ["close-1"]


def test_a_script_change_rewrites_prepared_leads_without_blocking_calls(workspace):
    ws = workspace
    save_script(ws)
    ids = [
        lead["id"] for lead in import_leads(ws, leads("Edit", 2), list_name="Edits", prepare=True)["leads"]
    ]
    run_jobs(ws, rounds=3)
    before = get(ws, f"/v1/leads/{ids[0]}/packet")["script"]
    save_script(ws, SCRIPT.replace("Would Tuesday work?", "Does Tuesday suit you?"))
    after = get(ws, f"/v1/leads/{ids[0]}/packet")["script"]
    # Written text whose block and instruction are unchanged stays; the lead stays callable.
    assert after["written"]["ai-1"] == before["written"]["ai-1"]
    assert after["preparation_status"] == "stale" and lead_row(ws, ids[0])["status"] == "eligible"
    run_jobs(ws, rounds=3)
    assert get(ws, f"/v1/leads/{ids[0]}/packet")["script"]["preparation_status"] == "ready"


def test_unprepared_leads_can_be_released_to_call_with_the_base_script(workspace):
    ws = workspace
    save_script(ws)
    call(ws, "PUT", "/v1/admin/preparation", json={"leads_per_agent": 1, "agents_at_once": 1})
    ids = [
        lead["id"]
        for lead in import_leads(ws, leads("Free", 3), list_name="Release me", prepare=True)["leads"]
    ]
    released = post(ws, "/v1/preparation/lists/release", {"list_name": "Release me"})
    # The lead already in a running thread finishes there; the waiting ones are released.
    assert released["released"] == 2
    assert sorted(lead_row(ws, i)["status"] for i in ids) == ["eligible", "eligible", "preparing"]
    script = get(ws, f"/v1/leads/{ids[2]}/packet")["script"]
    assert script["fallback"] is True and "ai-1" not in script["written"]


def test_contacts_at_one_company_are_spaced_and_a_booking_holds_the_rest(workspace, clock):
    ws = workspace
    rows = [
        {
            "name": "Ana One",
            "phone": "+14155557001",
            "company": "Shared Holdings",
            "timezone": "America/Los_Angeles",
        },
        {
            "name": "Bo Two",
            "phone": "+14155557002",
            "company": "Shared Holdings",
            "timezone": "America/Los_Angeles",
        },
        {
            "name": "Cy Three",
            "phone": "+14155557003",
            "company": "shared  holdings",
            "timezone": "America/Los_Angeles",
        },
    ]
    ids = [lead["id"] for lead in import_leads(ws, rows, list_name="Shared")["leads"]]
    called, reasons = [], set()
    for index in range(8):
        ready(ws)
        response = ws["client"].post(
            "/v1/queue/next", headers=ws["headers"], json={"campaign_id": ws["campaign_id"]}
        )
        if response.status_code != 200:
            reasons.update(response.json()["detail"].get("reasons", []))
            break
        reservation = response.json()
        started = post(
            ws,
            "/v1/calls/start",
            {"reservation_id": reservation["reservation_id"], "idempotency_key": f"space-{index}"},
        )
        hang_up(ws, started["id"])
        wrap_up(ws, started["id"], outcome="no_answer")
        called.append(reservation["lead"]["id"])
    assert len(set(called) & set(ids)) == 1, "only one contact at the company is called today"
    assert "COMPANY_CONTACTED_TODAY" in reasons
    other = next(i for i in ids if i not in called)
    lead = lead_row(ws, other)
    booked = post(
        ws,
        f"/v1/leads/{other}/meeting",
        {
            "expected_version": lead["version"],
            "request_id": str(uuid4()),
            "status": "booked",
            "due_at": (clock.now + timedelta(days=2)).isoformat(),
            "timezone": "America/Los_Angeles",
            "reference": "cal-7",
        },
    )
    assert booked["outreach_hold"] == "meeting_booked"
    siblings = [lead_row(ws, i) for i in ids if i != other]
    assert {s["outreach_hold"] for s in siblings} == {"company_booked"}
    assert {s["company_booked_by"] for s in siblings} == {other}
    lead = lead_row(ws, other)
    post(
        ws,
        f"/v1/leads/{other}/meeting",
        {
            "expected_version": lead["version"],
            "request_id": str(uuid4()),
            "status": "cancelled",
            "next_action": "resume",
        },
    )
    assert {lead_row(ws, i)["outreach_hold"] for i in ids if i != other} == {None}


def test_ai_budget_is_set_per_lead_list(workspace):
    ws = workspace
    budget = call(ws, "PUT", "/v1/admin/budget", json={"per_list_usd": 40, "number_purchase_limit_usd": 0})
    assert budget["per_list_usd"] == 40
    assert get(ws, "/v1/admin/settings")["budget"] == {"per_list_usd": 40, "number_purchase_limit_usd": 0}
    call(ws, "PUT", "/v1/admin/budget", status=422, json={"daily_limit_usd": 5, "monthly_limit_usd": 10})


def test_retired_routes_and_settings_are_gone(workspace):
    ws = workspace
    for path in ("/v1/inference/chatgpt/start", "/v1/inference/chatgpt/complete"):
        assert ws["client"].post(path, headers=ws["headers"], json={}).status_code in {404, 405}
    policy = get(ws, "/v1/admin/operating-policy")
    assert "dnc_refresh" not in policy and "script_template" not in policy
    current = policy["version"]
    response = ws["client"].put(
        "/v1/admin/operating-policy",
        headers=ws["headers"],
        json={"request_id": str(uuid4()), "expected_version": current, "dnc_refresh": {"enabled": True}},
    )
    assert response.status_code == 422
    assert "refresh" not in get(ws, "/v1/admin/dnc/screens")


def test_opted_out_numbers_are_never_researched(workspace):
    ws = workspace
    save_script(ws)
    rows = leads("Optout", 2)
    post(ws, "/v1/suppressions", {"phone": rows[0]["phone"], "reason": "opt_out"})
    imported = import_leads(ws, rows, list_name="Opt-outs", prepare=True)
    assert imported["preparing"] == 1
    blocked, open_lead = (lead["id"] for lead in imported["leads"])
    assert lead_row(ws, blocked)["status"] == "eligible" and lead_row(ws, open_lead)["status"] == "preparing"
    assert post(ws, "/v1/preparation/lists", {"list_name": "Opt-outs"})["marked"] == 0


def test_the_scripts_facts_and_never_say_become_the_helpers_approved_knowledge(workspace):
    ws = workspace
    before = get(ws, "/v1/workspace")["active_playbook"]
    save_script(ws, SCRIPT + "\n# Facts\n- Licensed and insured since 2009.\n")
    after = get(ws, "/v1/workspace")["active_playbook"]
    assert after["version"] == before["version"] + 1
    assert after["claims"] == ["Licensed and insured since 2009."]
    assert after["prohibited_claims"] == ["Guaranteed savings"]
    assert after.get("product_description", "") == before.get("product_description", "")
    assert [(c["id"], c["text"]) for c in after["cards"]] == [(c["id"], c["text"]) for c in before["cards"]]
    # Saving the same facts again does not publish another knowledge version.
    save_script(
        ws,
        SCRIPT.replace("Would Tuesday work?", "Does Tuesday work?")
        + "\n# Facts\n- Licensed and insured since 2009.\n",
    )
    assert get(ws, "/v1/workspace")["active_playbook"]["version"] == after["version"]
