"""Model-output journeys over HTTP: a scripted fake model supplies tips, reviews, rundowns and coaching."""

import json
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from journey import answered_by_human, call, event, get, post, run_jobs, start, wrap_up

UTTERANCE = "We need it to keep working with no connection in the field."
OFFLINE_QUOTE = "no connection"
OBSERVATION = "Prospect needs field work to continue offline"
QUESTION = "What would an outage in the field cost your crews?"
REASON = "Explore the operational impact of offline work."
RUNDOWN = [
    "The seller introduced the company and asked for a short slot",
    "Prospect reported no budget for the current quarter",
]
COACHING = "Name the budget constraint before proposing a follow-up"
APPROACH = "Opened with the company identity before asking about need"


class FakeModel:
    """A provider that returns canned structured output in the production transport. No network."""

    last_provider, last_model = "Cerebras", "fixture-model"
    last_usage, last_request_id = {}, None
    on_progress = None
    first_useful_ms = connect_ms = None

    def __init__(self, script):
        self.script = script

    async def suggest(self, request, deadline_ms=None, **kwargs):
        finalizing = request.get("mode") == "finalize"
        self.last_request_id = f"{'finalize' if finalizing else 'live'}-{uuid4().hex[:8]}"
        self.last_usage = {"cost": 0.001}
        out = deepcopy(self.script.get("finalize" if finalizing else "live", quiet)(request))
        if request.get("private_question") and self.on_progress is not None:
            await self.on_progress(deepcopy(out))
        return out

    async def review(self, request, deadline_ms=None, **kwargs):
        self.last_request_id = f"review-{uuid4().hex[:8]}"
        self.last_usage = {"cost": 0.002}
        verdicts = self.script.get("verdicts", lambda _: {})(request)
        return {
            "fingerprint": request["fingerprint"],
            "verdicts": [
                {
                    "field": item["field"],
                    "verdict": verdicts.get(item["field"], "SUPPORTED"),
                    "evidence": item["evidence"] or [source["id"] for source in request["sources"]],
                }
                for item in request["fields"]
            ],
        }


def install_fake_models(app, monkeypatch, script):
    """The only seam into the application. Every model call goes to the scripted fake.

    `script` may hold "live" and "finalize" (request -> one provider turn) and "verdicts"
    (request -> {field: verdict}; unlisted fields are SUPPORTED). Live help runs only when the
    settings are not simulated, so this also leaves simulation, as the existing live tests do.
    """
    service = app.state.service
    monkeypatch.setattr(service, "configured_coach", lambda *args: (FakeModel(script), "openrouter"))
    monkeypatch.setattr(service.settings, "simulation", False)


def turn_output(request, *, state_delta=None, guidance=None, field_audit=()):
    return {
        "state_version": request["state_version"],
        "context_fingerprint": request["context_fingerprint"],
        "state_delta": state_delta,
        "field_audit": list(field_audit),
        "guidance": guidance,
    }


def quiet(request):
    return turn_output(request)


def ref(identity, revision=1):
    return {"kind": "segment", "id": identity, "version": revision}


def revision_of(request, identity):
    return next((s.get("revision", 1) for s in request["recent_dialogue"] if s["segment_id"] == identity), 1)


def prospect_turns(request):
    return [s for s in request["recent_dialogue"] if s.get("side") == "remote" and s.get("is_final")]


def audit(field, classification="nonassertive"):
    return {"field": field, "classification": classification, "sources": [], "implied_premises": []}


def guidance(request, say_or_ask, reason):
    return {
        "kind": "DISCOVERY",
        "say_or_ask": say_or_ask,
        "reason": reason,
        "alternatives": [],
        "evidence_segment_ids": [prospect_turns(request)[-1]["segment_id"]],
        "playbook_card_ids": [],
        "expires_in_ms": 30000,
    }


def delta(request, *, observations=(), rubric=(), trackers=(), stage="discovery"):
    interpreted = [s for s in request["recent_dialogue"] if s.get("is_final") and not s.get("system_audio")]
    return {
        "stage": stage,
        "observations": list(observations),
        "rubric_items": list(rubric),
        "tracker_matches": list(trackers),
        "interpreted_segments": [ref(s["segment_id"], s.get("revision", 1)) for s in interpreted],
    }


def observation(request, kind, text, *, segment, quote="", review_required=False, approach=None):
    return {
        "kind": kind,
        "text": text,
        "state": "open",
        "sources": [ref(segment, revision_of(request, segment))],
        "replaces": [],
        "quote": quote,
        "commitment": None,
        "review_required": review_required,
        "approach": approach,
        "outcome": None,
        "observed_guidance_id": None,
    }


def quoted_tip(request):
    """A model tip whose own state update carries a quote from the prospect's words."""
    observed = observation(request, "need", OBSERVATION, segment="s1", quote=OFFLINE_QUOTE)
    return turn_output(
        request,
        state_delta=delta(request, observations=[observed]),
        guidance=guidance(request, QUESTION, REASON),
        field_audit=[audit("say_or_ask"), audit("reason")],
    )


def factual_reason(request):
    """The question is nonassertive; the reason states a product fact that needs review."""
    return turn_output(
        request,
        guidance=guidance(
            request,
            "How often do crews lose connectivity today?",
            "Our tool works without an internet connection.",
        ),
        field_audit=[audit("say_or_ask"), audit("reason", "material_claim")],
    )


def factual_question(request):
    """The displayed sentence is itself a product claim."""
    return turn_output(
        request,
        guidance=guidance(
            request,
            "Our tool is fully offline and needs no cloud sync.",
            "Answers the offline question directly.",
        ),
        field_audit=[audit("say_or_ask", "material_claim"), audit("reason")],
    )


def card_question_with_claim(request):
    """Real Cerebras shape (October 30): the question from an approved card is audited as a
    supported fact and the explanation adds an unsupported claim."""
    return turn_output(
        request,
        guidance={
            **guidance(
                request,
                "How often do crews lose connectivity today?",
                "Our tool works without an internet connection.",
            ),
            "alternatives": ["Who handles connectivity problems?"],  # left unaudited by the model
        },
        field_audit=[audit("say_or_ask", "supported_fact"), audit("reason", "material_claim")],
    )


def supported_demo(request):
    return turn_output(
        request,
        guidance=guidance(
            request,
            "Would a short demo of the offline mode help your team?",
            "The demo shows the offline mode working without a connection.",
        ),
        field_audit=[audit("say_or_ask"), audit("reason", "material_claim")],
    )


def pending_need(request):
    observed = observation(
        request,
        "need",
        "Prospect needs a workflow that stays usable offline",
        segment="s1",
        review_required=True,
    )
    return turn_output(request, state_delta=delta(request, observations=[observed]))


def scripted(*plans):
    """Route each live turn by the prospect's latest words; other turns stay quiet."""

    def live(request):
        turns = prospect_turns(request)
        words = turns[-1]["text"] if turns else ""
        for keyword, build in plans:
            if keyword in words:
                return build(request)
        return quiet(request)

    return live


def finalize_rundown(request):
    """Rundown bullets, one scored rubric item and a semantic tracker match, in one finalization."""
    trackers = [
        {
            "tracker_id": t["id"],
            "definition_version": t["version"],
            "sources": [ref("s1", revision_of(request, "s1"))],
        }
        for t in request["trackers"]
    ]
    rubric = [
        {
            "dimension": "identity_relevance",
            "status": "scored",
            "score": 2,
            "explanation": "Opened with company identity and a brief purpose",
            "sources": [ref("r1", revision_of(request, "r1"))],
        }
    ]
    observed = [
        observation(request, "rundown", RUNDOWN[0], segment="r1"),
        observation(request, "rundown", RUNDOWN[1], segment="s1"),
    ]
    state = delta(request, observations=observed, rubric=rubric, trackers=trackers, stage="objection")
    return turn_output(request, state_delta=state)


def finalize_coaching(request):
    """A coaching point on the prospect's budget remark and a rep approach, both source-linked."""
    coaching = observation(request, "coaching", COACHING, segment="s1")
    approach = observation(request, "approach", APPROACH, segment="r1", approach="Opened with identity first")
    approach["sources"].append(ref("s1", revision_of(request, "s1")))
    return turn_output(request, state_delta=delta(request, observations=[coaching, approach]))


def asker_answer(request):
    return turn_output(
        request,
        guidance=guidance(
            request, "What would a budget have to cover first?", "Clarifies the budget objection."
        ),
        field_audit=[audit("say_or_ask"), audit("reason")],
    )


def chunk(ws, call_id, segment_id, text, *, side="remote"):
    return post(
        ws,
        f"/v1/calls/{call_id}/transcript",
        {"segment_id": segment_id, "revision": 1, "side": side, "is_final": True, "text": text},
    )


def helper_tip(ws, clock, call_id):
    clock.now += timedelta(milliseconds=400)
    run_jobs(ws)
    return get(ws, f"/v1/calls/{call_id}")["coach"]


def review_states(ws, call_id):
    observations = get(ws, f"/v1/calls/{call_id}")["conversation_state"]["observations"]
    return [(o["kind"], o["review_status"], o["epistemic_status"]) for o in observations]


def finish_call(ws, app, monkeypatch, script):
    """A two-sided call hung up while still simulated, then finalized by the scripted model."""
    cid = start(ws)["id"]
    answered_by_human(ws, cid)
    chunk(ws, cid, "r1", "Hi, this is Example Seller calling about a brief introduction.", side="rep")
    chunk(ws, cid, "s1", "We have no budget for this right now.")
    call(ws, "POST", f"/v1/calls/{cid}/hangup")
    install_fake_models(app, monkeypatch, script)
    wrap_up(ws, cid, outcome="not_interested", contact_role="right_party")
    run_jobs(ws)
    run_jobs(ws)
    return cid


def test_a_model_tip_and_its_quote_last_only_as_long_as_their_raw_speech(workspace, app, monkeypatch, clock):
    ws = workspace
    cid = start(ws)["id"]
    answered_by_human(ws, cid)
    install_fake_models(app, monkeypatch, {"live": quoted_tip})
    chunk(ws, cid, "s1", UTTERANCE)
    tip = helper_tip(ws, clock, cid)
    assert tip["say_or_ask"] == QUESTION and tip["reason"] == REASON
    assert tip["meaning_review"]["status"] == "model_suggestion"
    assert tip["provider"] == "openrouter" and tip["actual_provider"] == "Cerebras"
    assert tip["state_update"]["observations"][0]["quote"] == OFFLINE_QUOTE
    call(ws, "PATCH", f"/v1/calls/{cid}/coach", json={"pinned": True})
    clock.now += timedelta(seconds=200)
    kept = get(ws, f"/v1/calls/{cid}")["coach"]
    assert kept["id"] == tip["id"] and OFFLINE_QUOTE in json.dumps(kept)
    clock.now += timedelta(seconds=101)
    after = get(ws, f"/v1/calls/{cid}")
    assert after["coach"] is None and after["transcript"] == []
    assert [o["text"] for o in after["conversation_state"]["observations"]] == [OBSERVATION]
    for path in (f"/v1/calls/{cid}", f"/v1/calls/{cid}/analysis", "/v1/events"):
        assert OFFLINE_QUOTE not in json.dumps(get(ws, path)), path


def test_selective_fact_review_withdraws_only_the_claim_the_reviewer_rejects(
    workspace, app, monkeypatch, clock
):
    ws = workspace
    cid = start(ws)["id"]
    answered_by_human(ws, cid)
    verdicts = {"reason": "UNSUPPORTED"}
    live = scripted(
        ("internet", factual_reason),
        ("fully offline", factual_question),
        ("demo", supported_demo),
    )
    install_fake_models(app, monkeypatch, {"live": live, "verdicts": lambda _: verdicts})
    chunk(ws, cid, "s1", "Does it work without internet?")
    shown = helper_tip(ws, clock, cid)
    assert shown["say_or_ask"] == "How often do crews lose connectivity today?"
    assert shown["reason"] == "", "The unsupported product claim is withdrawn"
    review = shown["meaning_review"]
    assert review["status"] == "model_reviewed"
    assert review["rejected_fields"] == ["reason"] and review["displayed_fields"] == ["say_or_ask"]

    chunk(ws, cid, "s2", "We would need it fully offline.")
    verdicts.clear()
    verdicts["say_or_ask"] = "UNSUPPORTED"
    assert helper_tip(ws, clock, cid) is None, "A rejected factual sentence withholds the whole tip"

    chunk(ws, cid, "s3", "Could you show me the offline mode in a demo?")
    verdicts.clear()
    supported = helper_tip(ws, clock, cid)
    assert supported["say_or_ask"] == "Would a short demo of the offline mode help your team?"
    assert supported["reason"] == "The demo shows the offline mode working without a connection."
    assert supported["meaning_review"]["status"] == "model_reviewed"
    assert "rejected_fields" not in supported["meaning_review"]


def test_conversation_review_runs_on_its_clock_and_marks_the_model_observation(
    workspace, app, monkeypatch, clock
):
    ws = workspace
    cid = start(ws)["id"]
    answered_by_human(ws, cid)
    install_fake_models(app, monkeypatch, {"live": scripted(("field work", pending_need))})
    chunk(ws, cid, "s1", "Our field work depends on it.")
    helper_tip(ws, clock, cid)
    assert review_states(ws, cid) == [("need", "pending", "model_interpreted")]
    clock.now += timedelta(seconds=25)
    run_jobs(ws)
    assert review_states(ws, cid) == [("need", "pending", "model_interpreted")], (
        "Review waits for its interval"
    )
    clock.now += timedelta(seconds=10)
    run_jobs(ws)
    assert review_states(ws, cid) == [("need", "reviewed", "model_reviewed")]


def test_model_rundown_scorecard_and_tracker_match_publish_after_finalization(workspace, app, monkeypatch):
    ws = workspace
    tracker = call(
        ws,
        "POST",
        "/v1/admin/trackers",
        json={
            "kind": "semantic",
            "name": "Budget objection",
            "definition": "Prospect rejects spending",
            "positive_examples": ["We have no budget"],
            "negative_examples": ["Budget is approved"],
        },
    )
    cid = finish_call(ws, app, monkeypatch, {"finalize": finalize_rundown})
    assert get(ws, f"/v1/calls/{cid}")["rundown_status"] == "ready"
    an = get(ws, f"/v1/calls/{cid}/analysis")
    assert an["finalization"]["status"] == "ready"
    assert an["notes"]["summary"].split("\n") == RUNDOWN
    items = {item["dimension"]: item for item in an["scorecard"]["items"]}
    assert items["identity_relevance"]["status"] == "SCORED" and items["identity_relevance"]["score"] == 2
    assert items["identity_relevance"]["explanation"] == "Opened with company identity and a brief purpose"
    assert items["listening_response"]["status"] == "INSUFFICIENT_EVIDENCE"
    coverage = an["interpretation_coverage"]
    assert coverage["interpreted_segments"] == coverage["total_segments"] == 2
    [match] = [m for m in an["trackers"] if m["tracker_id"] == tracker["id"]]
    assert match["kind"] == "semantic" and match["evidence"] == ["s1"]


def test_coaching_point_becomes_a_library_snippet_and_recent_coaching_history(
    workspace, app, monkeypatch, clock
):
    ws = workspace
    cid = finish_call(ws, app, monkeypatch, {"finalize": finalize_coaching})
    clock.now += timedelta(seconds=31)
    run_jobs(ws)
    point = get(ws, f"/v1/calls/{cid}/analysis")["call_memory"]["coaching_points"][0]
    assert point["text"] == COACHING
    history = get(ws, "/v1/coaching")["recent_tips"]
    assert [entry["call_id"] for entry in history] == [cid]
    assert history[0]["tips"][0]["text"] == COACHING
    version = get(ws, f"/v1/calls/{cid}")["transcript_version"]
    snippet = call(
        ws,
        "POST",
        "/v1/admin/snippets",
        json={
            "call_id": cid,
            "coaching_point_id": point["id"],
            "expected_transcript_version": version,
            "review_reference": "SIM-REVIEW-1",
        },
    )
    library = get(ws, "/v1/coaching")["examples"]
    assert [e["text"] for e in library] == [COACHING]
    assert library[0]["raw_evidence_status"] == "available_temporarily"
    clock.now += timedelta(seconds=301)
    library = get(ws, "/v1/coaching")["examples"]
    assert library[0]["text"] == COACHING and library[0]["raw_evidence_status"] == "expired"
    withdrawn = {"expected_version": snippet["version"], "approved": False}
    call(ws, "PATCH", f"/v1/admin/snippets/{snippet['id']}", json=withdrawn)
    assert get(ws, "/v1/coaching")["examples"] == []


def test_a_reviewed_example_becomes_a_coaching_strategy_with_logged_assignments(
    workspace, app, monkeypatch, clock
):
    ws = workspace
    cid = finish_call(ws, app, monkeypatch, {"finalize": finalize_coaching})
    clock.now += timedelta(seconds=31)
    run_jobs(ws)
    point = get(ws, f"/v1/calls/{cid}/analysis")["call_memory"]["coaching_points"][0]
    version = get(ws, f"/v1/calls/{cid}")["transcript_version"]
    example = call(
        ws,
        "POST",
        "/v1/admin/snippets",
        json={
            "call_id": cid,
            "coaching_point_id": point["id"],
            "expected_transcript_version": version,
            "review_reference": "SIM-REVIEW-STRATEGY",
        },
    )
    strategies = get(ws, "/v1/optimization/strategies")["strategies"]
    assert [s["id"] for s in strategies] == ["control", example["id"]]
    assert strategies[1]["text"] == COACHING
    # Calls are placed as simulated; only the helper turn below leaves simulation.
    monkeypatch.setattr(app.state.service.settings, "simulation", True)
    rows = [
        {
            "name": f"Strategy {i}",
            "phone": f"+1415555{3100 + i}",
            "company": f"Strategy Co {i}",
            "timezone": "America/Los_Angeles",
            "timezone_confidence": "confirmed",
            "region": "415",
        }
        for i in range(16)
    ]
    post(ws, "/v1/leads/import", {"campaign_id": ws["campaign_id"], "rows": rows})
    body = {
        "family": "coaching",
        "review_reference": "Reviewed coaching comparison",
        "duration_days": 1,
        "request_id": str(uuid4()),
        "strategy_ids": ["control", example["id"]],
        "candidate_probability": 0.5,
    }
    unknown = ws["client"].post(
        "/v1/admin/optimization/comparisons",
        headers=ws["headers"],
        json={**body, "request_id": str(uuid4()), "strategy_ids": ["control", "unreviewed"]},
    )
    assert unknown.status_code == 422, "Only seller-reviewed examples can be approaches"
    study = post(ws, "/v1/admin/optimization/comparisons", body)
    assert post(ws, "/v1/admin/optimization/comparisons", body)["id"] == study["id"], "Idempotent"
    contexts = []
    live = {"live": lambda request: contexts.append(request["strategy_context"]) or asker_answer(request)}
    chosen = []
    for i in range(16):
        placed = start(ws, key=f"strategy-call-{i}")["id"]
        assignment = get(ws, f"/v1/calls/{placed}")["coaching_comparison"]
        assert assignment["comparison_id"] == study["id"]
        assert assignment["strategy_probabilities"] == {"control": 0.5, example["id"]: 0.5}
        assert assignment["propensity"] == 0.5
        assert assignment["arm"] == ("control" if assignment["strategy_id"] == "control" else "candidate")
        chosen.append(assignment["strategy_id"])
        if assignment["strategy_id"] == example["id"] and not contexts:
            answered_by_human(ws, placed)
            chunk(ws, placed, "s1", "We have no budget for this right now.")
            install_fake_models(app, monkeypatch, live)
            assert helper_tip(ws, clock, placed)["say_or_ask"]
            monkeypatch.setattr(app.state.service.settings, "simulation", True)
            call(ws, "POST", f"/v1/calls/{placed}/hangup")
            wrap_up(ws, placed, outcome="not_interested", contact_role="right_party")
        else:
            event(ws, placed, "call.hangup")
            wrap_up(ws, placed, outcome="no_answer")
        clock.now += timedelta(seconds=30)
    assert set(chosen) == {"control", example["id"]}, "A fair draw reaches both approaches"
    [context] = contexts
    assert context["strategy_id"] == example["id"] and context["approach"] == COACHING
    assert context["reason"] == "reviewed_finite_strategy"
    report = settled(ws, "/v1/optimization")
    comparison = next(c for c in report["comparisons"] if c["id"] == study["id"])
    assert comparison["allocation_policy"] == "two_arm"
    assert comparison["inference_unit"] == "lead; stable assignment within rep strata"
    assert comparison["strategy_exposure"]["generated"] == 1
    assert comparison["strategy_exposure"]["displayed"] == 0
    snapshot = comparison["strategy_policy_snapshot"]
    assert set(snapshot["probabilities"]) == {"control", example["id"]}
    assert snapshot["observations"] == {"control": 0, example["id"]: 0}, "No outcome window has closed"
    assert comparison["sufficient_evidence"] is False
    # Once each assignment's outcome window closes, it counts once for the approach it was given.
    clock.now += timedelta(days=15)
    login = {"email": "operator@example.test", "password": "a-test-only-password-123"}
    token = call(ws, "POST", "/v1/auth/session", json=login)["access_token"]
    ws = {**ws, "headers": {"Authorization": f"Bearer {token}"}}
    later = settled(ws, "/v1/optimization")
    comparison = next(c for c in later["comparisons"] if c["id"] == study["id"])
    counted = {action: chosen.count(action) for action in ("control", example["id"])}
    assert comparison["strategy_policy_snapshot"]["observations"] == counted
    assert comparison["strategy_policy_snapshot"]["best"] == "control", "Sparse rewards keep the default"


def settled(ws, path):
    for _ in range(30):
        body = get(ws, path)
        if body["projection"]["status"] == "ready":
            return body
        run_jobs(ws)
    raise AssertionError(body["projection"])


def test_rep_confirms_a_reviewed_approach_and_it_becomes_a_reviewed_opportunity(
    workspace, app, monkeypatch, clock
):
    ws = workspace
    cid = finish_call(ws, app, monkeypatch, {"finalize": finalize_coaching})
    clock.now += timedelta(seconds=31)
    run_jobs(ws)
    memory = get(ws, f"/v1/calls/{cid}/analysis")["call_memory"]
    approach = next(o for o in memory["observations"] if o["kind"] == "approach")
    assert approach["review_status"] == "reviewed" and approach["epistemic_status"] == "model_reviewed"
    [opportunity] = get(ws, "/v1/coaching")["reviewed_opportunities"]
    assert opportunity["concept"] == APPROACH and opportunity["approach"] == "Opened with identity first"
    path = f"/v1/calls/{cid}/observations/{approach['id']}/review"
    confirmed = call(
        ws,
        "POST",
        path,
        json={
            "expected_version": approach["version"],
            "decision": "confirm",
            "reason": "Rep confirmed the opener",
        },
    )
    assert confirmed["review_status"] == "human_confirmed"
    stale = ws["client"].post(
        path,
        headers=ws["headers"],
        json={"expected_version": approach["version"], "decision": "retract", "reason": "Changed my mind"},
    )
    assert stale.status_code == 409
    [row] = get(ws, "/v1/coaching")["reviewed_opportunities"]
    assert row["review_status"] == "human_confirmed"


def test_private_help_streams_a_partial_answer_then_settles_on_the_same_identity(workspace, app, monkeypatch):
    ws = workspace
    cid = start(ws)["id"]
    answered_by_human(ws, cid)
    install_fake_models(app, monkeypatch, {"live": asker_answer})
    chunk(ws, cid, "s1", "We have no budget for this right now.")
    reply = post(
        ws,
        f"/v1/calls/{cid}/coach",
        {"question": "How do I handle the budget objection?", "urgent": True, "request_id": str(uuid4())},
    )
    assert reply["status"] == "ready" and reply["private"] is True and reply["say_or_ask"]
    published = [
        event
        for event in get(ws, "/v1/events")["events"]
        if event["type"] == "coach.card_ready" and event["payload"].get("coach_event_id") == reply["id"]
    ]
    assert len(published) == 2, "The partial answer is shown first, then the final answer replaces it"
    assert get(ws, f"/v1/calls/{cid}")["coach"]["id"] == reply["id"]


def test_a_reviewed_question_stays_when_only_its_explanation_is_rejected(workspace, app, monkeypatch, clock):
    ws = workspace
    cid = start(ws)["id"]
    answered_by_human(ws, cid)
    live = scripted(("internet", card_question_with_claim))
    install_fake_models(app, monkeypatch, {"live": live, "verdicts": lambda _: {"reason": "UNSUPPORTED"}})
    chunk(ws, cid, "s1", "Does it work without internet?")
    shown = helper_tip(ws, clock, cid)
    assert shown["say_or_ask"] == "How often do crews lose connectivity today?"
    assert shown["reason"] == "", "The rejected explanation is never shown"
    assert shown["meaning_review"]["displayed_fields"] == ["say_or_ask"]


def test_a_tip_whose_audit_cites_an_unknown_source_gets_the_full_review(workspace, app, monkeypatch, clock):
    """Real Cerebras (October 31) cites sources outside the catalog in its field audit. Audits only
    route work, so such a tip goes to the complete independent review instead of being dropped."""
    ws = workspace
    cid = start(ws)["id"]
    answered_by_human(ws, cid)
    reviewed = []

    def odd_audit(request):
        return turn_output(
            request,
            guidance=guidance(request, "What still slows follow-up down with that CRM?", "Build on it."),
            field_audit=[{**audit("say_or_ask"), "sources": [ref("private_question")]}, audit("reason")],
        )

    def verdicts(review):
        reviewed.append([field["field"] for field in review["fields"]])
        return {}

    install_fake_models(app, monkeypatch, {"live": scripted(("CRM", odd_audit)), "verdicts": verdicts})
    chunk(ws, cid, "s1", "We already use a CRM for follow-up and it works fine.")
    shown = helper_tip(ws, clock, cid)
    assert shown["say_or_ask"] == "What still slows follow-up down with that CRM?"
    assert reviewed and "say_or_ask" in reviewed[-1]


def test_an_exact_context_echo_outweighs_a_misnumbered_version_and_stray_evidence(
    workspace, app, monkeypatch, clock
):
    """Real Cerebras (October 30) misnumbers state_version and lists card ids as speech while
    echoing the exact context fingerprint. A wrong fingerprint is still refused."""
    ws = workspace
    cid = start(ws)["id"]
    answered_by_human(ws, cid)
    echo = {"fingerprint": True}

    def misnumbered(request):
        turn = turn_output(
            request,
            guidance={
                **guidance(request, "How often do crews lose connectivity today?", "Clarify the need."),
                "evidence_segment_ids": [prospect_turns(request)[-1]["segment_id"], "budget"],
            },
            field_audit=[audit("say_or_ask"), audit("reason")],
        )
        if not echo["fingerprint"]:
            turn["context_fingerprint"] = "0" * 64
        return {**turn, "state_version": request["state_version"] + 7}

    install_fake_models(app, monkeypatch, {"live": misnumbered})
    chunk(ws, cid, "s1", "Does it work without internet?")
    shown = helper_tip(ws, clock, cid)
    assert shown["say_or_ask"] == "How often do crews lose connectivity today?"
    assert shown["evidence_segment_ids"] == ["s1"]
    echo["fingerprint"] = False
    chunk(ws, cid, "s2", "We would need it offline.")
    assert helper_tip(ws, clock, cid) is None


def test_a_misnumbered_finalization_with_the_exact_context_is_kept(workspace, app, monkeypatch):
    """Real Cerebras (October 30) wrote a rundown but misnumbered its version."""

    def misnumbered(request):
        return {**finalize_rundown(request), "state_version": request["state_version"] + 1}

    cid = finish_call(workspace, app, monkeypatch, {"finalize": misnumbered})
    assert get(workspace, f"/v1/calls/{cid}")["rundown_status"] == "ready"
    assert get(workspace, f"/v1/calls/{cid}/analysis")["notes"]["summary"].split("\n") == RUNDOWN
