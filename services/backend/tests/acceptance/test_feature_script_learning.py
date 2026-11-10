"""Script learning over HTTP: section reactions, effects on close, the Markdown memory and tested script changes."""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from journey import answered_by_human, call, get, hang_up, import_leads, post, run_jobs, start, wrap_up

SECTIONS = {
    "opening": "Hi {first_name}, this is {rep} from {seller}.",
    "screening": "{rep} from {seller}, calling about invoice follow-up.",
    "gatekeeper": "Who handles invoice follow-up for {company}?",
    "discovery": "- How does your team handle invoice follow-up today?",
    "close": "Would a short walkthrough next week be useful?",
}
OPEN, ASK, CLOSE = "opening:opening-1", "discovery:discovery-1", "close:close-1"
PROSPECT = "Honestly we chase every invoice by hand in spreadsheets and it eats two days each month."


def use_template(ws, **changes):
    """Save the journey's script as Markdown; returns the new base version."""
    body = "\n\n".join(f"# {key.title()}\n{text}" for key, text in {**SECTIONS, **changes}.items())
    base = get(ws, "/v1/optimization/script")["base"]
    saved = post(
        ws,
        "/v1/admin/script/import",
        {"markdown": body, "expected_version": base["version"], "preview": False},
    )
    return saved["version"]


def post_put(ws, path, body):
    response = ws["client"].put(path, headers=ws["headers"], json=body)
    assert response.status_code == 200, response.text
    return response.json()


def one_attempt_per_lead(ws):
    """Each lead is called once, so both periods hold separate leads."""
    policy = get(ws, "/v1/workspace")["policy"]
    body = {"reason": "One attempt per lead for this journey", "max_attempts": 1}
    return post(ws, f"/v1/admin/policies/{policy['id']}/publish", body)


def say(ws, call_id, segment_id, side, text, start_ms, end_ms):
    body = {
        "segment_id": segment_id,
        "revision": 1,
        "side": side,
        "is_final": True,
        "text": text,
        "start_ms": start_ms,
        "end_ms": end_ms,
    }
    return post(ws, f"/v1/calls/{call_id}/transcript", body)


def leads(prefix, count, area="415"):
    return [
        {
            "name": f"{prefix} {i}",
            "phone": f"+1{area}555{i:04d}" if area != "415" else f"+1415556{i:04d}",
            "company": f"{prefix} Co {i}",
            "timezone": "America/Los_Angeles",
            "timezone_confidence": "confirmed",
            "region": area,
        }
        for i in range(count)
    ]


def relogin(ws):
    token = call(ws, "POST", "/v1/local/session", headers={"X-Local-Token": "test-local-secret"}, json={})[
        "access_token"
    ]
    return {**ws, "headers": {"Authorization": f"Bearer {token}"}}


def refresh_learning(ws):
    run_jobs(
        ws, rounds=4
    )  # Finish post-call work first (finalize and the after-call summary), as the lanes would.
    post(ws, "/v1/admin/optimization/refresh")
    run_jobs(ws, rounds=4)
    return get(ws, "/v1/optimization/script")


def finish(ws, call_id, outcome):
    hang_up(ws, call_id)
    return wrap_up(ws, call_id, outcome=outcome, contact_role="right_party")


def good_call(ws, key):
    """Opener, an early discovery question and a long prospect answer: balanced talk."""
    placed = start(ws, key=key)
    call_id = placed["id"]
    answered_by_human(ws, call_id)
    script = get(ws, f"/v1/calls/{call_id}")["script_snapshot"]
    say(ws, call_id, "r1", "rep", script["opener"], 0, 4000)
    say(ws, call_id, "s1", "remote", "Okay, what is this about?", 4500, 6000)
    say(ws, call_id, "r2", "rep", script["discovery_questions"][0], 6500, 9000)
    say(ws, call_id, "s2", "remote", PROSPECT, 9500, 21000)
    return call_id


def monologue_call(ws, key):
    """Opener, then a long pitch with no question; the prospect leaves."""
    placed = start(ws, key=key)
    call_id = placed["id"]
    answered_by_human(ws, call_id)
    script = get(ws, f"/v1/calls/{call_id}")["script_snapshot"]
    say(ws, call_id, "r1", "rep", script["opener"], 0, 4000)
    say(ws, call_id, "s1", "remote", "Okay, what is this about?", 4500, 6000)
    pitch = "We built a platform that automates collections with reminders and dashboards for finance teams."
    say(ws, call_id, "r2", "rep", pitch, 6500, 46000)
    say(ws, call_id, "s2", "remote", "Not for us.", 46500, 47500)
    return call_id


def test_rep_lines_are_matched_to_script_sections_and_the_trace_outlives_raw_speech(workspace, clock):
    ws = workspace
    base = use_template(ws)
    call_id = start(ws)["id"]
    answered_by_human(ws, call_id)
    script = get(ws, f"/v1/calls/{call_id}")["script_snapshot"]
    assert script["base_version"] == base and script["script_arm"] is None
    say(ws, call_id, "r1", "rep", script["opener"], 0, 4000)
    say(ws, call_id, "s1", "remote", "Okay, what is this about?", 4500, 6000)
    say(ws, call_id, "r2", "rep", script["discovery_questions"][0], 6500, 9000)
    say(ws, call_id, "s2", "remote", PROSPECT, 9500, 15000)
    say(ws, call_id, "r3", "rep", script["closing_question"], 15500, 17500)
    say(ws, call_id, "s3", "remote", "No thanks.", 18000, 18800)
    finish(ws, call_id, "not_interested")
    run_jobs(ws)
    clock.now += timedelta(seconds=301)  # Raw speech is gone; the trace is metadata.
    trace = get(ws, f"/v1/calls/{call_id}/review")["script_trace"]
    sections = trace["sections"]
    assert [s["key"] for s in sections] == [OPEN, ASK, CLOSE]
    assert [s["reaction"] for s in sections] == ["engaged", "engaged", "ended"]
    assert [s["portion_ms"] for s in sections] == [6500, 9000, 3300]
    assert all(len(s["variant"]) == 12 for s in sections)
    assert trace["opening"] == {
        "key": OPEN,
        "variant": sections[0]["variant"],
        "reaction": "engaged",
        "first_response_ms": 4500,
    }
    shape = trace["shape"]
    assert shape["timing"] == "measured" and shape["duration_ms"] == 18800
    assert round(shape["rep_talk_ratio"], 3) == round(8500 / (8500 + 7800), 3)
    assert shape["time_to_first_question_ms"] == 6500 and shape["discovery_questions_asked"] == 1
    assert "spreadsheets" not in json.dumps(trace) and "thanks" not in json.dumps(trace)


@pytest.mark.slow
def test_section_and_call_shape_effects_on_close_reach_the_markdown_memory(workspace, clock):
    ws = workspace
    use_template(ws)
    one_attempt_per_lead(ws)
    import_leads(ws, leads("Effect", 64))
    for period in range(2):
        for i in range(32):
            key = f"effect-{period}-{i}"
            if i % 2:
                finish(ws, good_call(ws, key), "meeting_held")
            else:
                finish(ws, monologue_call(ws, key), "not_interested")
            clock.now += timedelta(seconds=60)
        clock.now += timedelta(days=20)
        ws = relogin(ws)
    lab = refresh_learning(ws)
    report = lab["report"]
    assert report["samples"] == 64 and report["evidence_type"] == "observational_correlation"
    found = {(p["feature"], p["value"]): p for p in report["patterns"]}
    asked = found[("discovery_questions", "1")]
    assert asked["status"] == "supported_correlation" and asked["conversion_rate"] == 1
    assert asked["live"] is True, "Script content findings may guide live help"
    talk = found[("rep_talk_ratio", "<35%")]
    assert talk["status"] == "supported_correlation" and talk["live"] is False, "Timing stays post-call"
    sections = {s["key"]: s for s in report["sections"]}
    assert sections[OPEN]["delivered"] == 64
    assert sections[ASK]["delivered"] == 32
    assert sections[ASK]["reactions"]["engaged"] == 32
    memory = lab["memory"]
    markdown = memory["markdown"]
    assert markdown.startswith("# Script memory")
    assert "<!-- learned:start -->" in markdown and "<!-- learned:end -->" in markdown
    working = markdown.split("## What is working")[1].split("##")[0]
    assert "discovery question" in working.casefold()
    shape = markdown.split("## Call shape (post-call)")[1].split("##")[0]
    assert "talk ratio" in shape.casefold()
    assert "How does your team handle invoice follow-up today?" in markdown, "Seller-authored text is cited"
    assert "spreadsheets" not in markdown and "Not for us" not in markdown, "Never prospect words"
    assert memory["written_by"] == "learning_refresh" and memory["version"] >= 1


@pytest.mark.slow
def test_team_notes_survive_every_rewrite_with_versions(workspace, clock):
    ws = workspace
    use_template(ws)
    lab = refresh_learning(ws)
    first = lab["memory"]
    assert "Not enough calls yet" in first["markdown"]
    body = {"expected_version": first["version"], "team_notes": "Prospects in logistics prefer email first."}
    saved = post_put(ws, "/v1/admin/script/memory", body)
    assert saved["version"] == first["version"] + 1 and saved["written_by"] == "Local operator"
    stale = ws["client"].put("/v1/admin/script/memory", headers=ws["headers"], json=body)
    assert stale.status_code == 409
    call_id = good_call(ws, "notes-call")
    finish(ws, call_id, "not_interested")
    rewritten = refresh_learning(ws)["memory"]
    assert rewritten["version"] > saved["version"]
    assert rewritten["team_notes"] == "Prospects in logistics prefer email first."
    assert rewritten["markdown"].rstrip().endswith("Prospects in logistics prefer email first.")
    versions = get(ws, "/v1/optimization/script")["memory_versions"]
    assert [v["version"] for v in versions][:2] == [rewritten["version"], saved["version"]]


def opening(template):
    return template["sections"][0]["options"][0]["text"]


OPENER = "Hi {first_name}, {rep} with {seller}. How does your team chase invoice follow-up today?"


def run_test(ws, clock, *, candidate_converts, count=30):
    """One call per lead; outcomes depend only on the arm the test assigned."""
    arms = []
    for i in range(count):
        placed = start(ws, key=f"trial-{i}")
        snapshot = get(ws, f"/v1/calls/{placed['id']}")["script_snapshot"]
        arm = snapshot["script_arm"]
        arms.append(arm)
        if arm == "candidate":
            assert snapshot["opener"].startswith("Hi ") and " with " in snapshot["opener"]
        converts = (arm == "candidate") == candidate_converts
        finish(ws, placed["id"], "meeting_held" if converts else "not_interested")
        clock.now += timedelta(seconds=60)
    return arms


class RecordingAgent:
    """The simulated preparation agent, recording what each thread was given."""

    billing = "simulation"

    def __init__(self):
        from app.preparation.agent import SimulatedAgent

        self.inner, self.contexts = SimulatedAgent(), []

    async def run(self, instructions, tools, handler, first, follow_up, **kwargs):
        self.contexts.append(json.loads(first))
        return await self.inner.run(instructions, tools, handler, first, follow_up, **kwargs)


class ScriptModel:
    """The script-writing model: records each request and answers with a fixed proposal."""

    billing, search_enabled = "openai", False

    def __init__(self):
        self.requests = []

    async def complete(self, instructions, payload, schema):
        from app.providers.inference import InferenceResult

        self.requests.append((instructions, payload, schema))
        if "section" in schema.get("properties", {}):
            data = {
                "section": "opening",
                "index": None,
                "text": OPENER,
                "rationale": "Ask about the process sooner.",
            }
        else:
            data = {}
        return InferenceResult(data, {"cost": 0.001}, str(uuid4()))


@pytest.mark.slow
def test_the_memory_reaches_script_writing_but_call_shape_stays_post_call(workspace, app, monkeypatch, clock):
    ws = workspace
    use_template(ws)
    post_put(ws, "/v1/admin/script/settings", {"auto_propose": False, "promotion": "automatic"})
    one_attempt_per_lead(ws)
    import_leads(ws, leads("Feed", 64))
    for period in range(2):
        for i in range(32):
            key = f"feed-{period}-{i}"
            if i % 2:
                finish(ws, good_call(ws, key), "meeting_held")
            else:
                finish(ws, monologue_call(ws, key), "not_interested")
            clock.now += timedelta(seconds=60)
        clock.now += timedelta(days=20)
        ws = relogin(ws)
    memory = refresh_learning(ws)["memory"]
    post_put(
        ws,
        "/v1/admin/script/memory",
        {"expected_version": memory["version"], "team_notes": "Lead with the time saved."},
    )
    service = app.state.service
    agent = RecordingAgent()
    monkeypatch.setattr(service, "agent_route", lambda p: agent)
    lead = import_leads(ws, leads("Write", 1, area="512"))["leads"][0]["id"]
    post(ws, "/v1/research/batches", {"lead_ids": [lead]})
    run_jobs(ws)
    [context] = agent.contexts
    digest = context["what_works"]
    assert digest["team_notes"] == "Lead with the time saved."
    assert any(item["feature"] == "discovery_questions" for item in digest["working"])
    assert not any(item["feature"] == "rep_talk_ratio" for item in digest["working"] + digest["not_working"])


@pytest.mark.slow
def test_the_memory_proposes_a_change_for_the_weakest_section_and_starts_its_test(
    workspace, app, monkeypatch, clock
):
    ws = workspace
    use_template(ws)
    import_leads(ws, leads("Weak", 12))
    for i in range(12):
        placed = start(ws, key=f"weak-{i}")["id"]
        answered_by_human(ws, placed)
        opener = get(ws, f"/v1/calls/{placed}")["script_snapshot"]["opener"]
        say(ws, placed, "r1", "rep", opener, 0, 4000)
        say(ws, placed, "s1", "remote", "Not interested.", 4500, 5200)
        finish(ws, placed, "not_interested")
        clock.now += timedelta(seconds=60)
    model = ScriptModel()
    monkeypatch.setattr(app.state.service, "inference", lambda *args, **kwargs: model)
    lab = refresh_learning(ws)
    [proposal] = lab["proposals"]
    assert proposal["source"] == "memory" and proposal["section"] == "opening"
    assert proposal["status"] == "testing" and proposal["text"] == OPENER
    [(instructions, payload, _)] = [r for r in model.requests if "section" in r[2].get("properties", {})]
    assert payload["section"] == "opening" and payload["current_text"] == SECTIONS["opening"]
    assert "{seller}" in json.dumps(payload["locked"]) and payload["memory"].startswith("# Script memory")
    assert "never" in instructions.casefold()
    # One test at a time: another refresh does not open a second proposal.
    assert len(refresh_learning(ws)["proposals"]) == 1


def test_changed_memory_reaches_reprepared_existing_lead(workspace, app, monkeypatch, clock):
    ws = workspace
    use_template(ws)
    agent = RecordingAgent()
    monkeypatch.setattr(app.state.service, "agent_route", lambda p: agent)
    lead = import_leads(ws, leads("Cache", 1, area="512"))["leads"][0]["id"]
    post(ws, "/v1/research/batches", {"lead_ids": [lead]})
    run_jobs(ws)
    assert len(agent.contexts) == 1
    post_put(
        ws,
        "/v1/admin/script/memory",
        {"expected_version": 0, "team_notes": "Use the operational time saving angle."},
    )
    post(ws, "/v1/research/batches", {"lead_ids": [lead]})
    run_jobs(ws)
    assert len(agent.contexts) == 2, (
        "Explicit reprepare reused the old script despite changed campaign memory"
    )
    assert agent.contexts[-1]["what_works"]["team_notes"] == "Use the operational time saving angle."
    clock.now += timedelta(minutes=1)
    refresh_learning(ws)  # Only the report timestamp changes, not the writer's memory content.
    post(ws, "/v1/research/batches", {"lead_ids": [lead]})
    run_jobs(ws)
    assert len(agent.contexts) == 2, "Unchanged memory must keep the prepared script"
