"""Model-output journeys over HTTP: a scripted fake model supplies tips, reviews, rundowns and coaching."""

import json
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from journey import answered_by_human, call, get, post, run_jobs, start, wrap_up

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


def test_a_model_tip_expires_and_its_quote_stays_only_in_the_transcript(workspace, app, monkeypatch, clock):
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
    assert after["coach"] is None, "the tip expires with its display window"
    assert after["transcript"], "the transcript is kept 30 days (D7)"
    assert [o["text"] for o in after["conversation_state"]["observations"]] == [OBSERVATION]
    # The quote lives on only in the transcript itself (kept 30 days, D7), never in tips, notes or events.
    for path in (f"/v1/calls/{cid}", f"/v1/calls/{cid}/analysis", "/v1/events"):
        body = {k: v for k, v in get(ws, path).items() if k != "transcript"}
        assert OFFLINE_QUOTE not in json.dumps(body), path


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


def settled(ws, path):
    for _ in range(30):
        body = get(ws, path)
        if body["projection"]["status"] == "ready":
            return body
        run_jobs(ws)
    raise AssertionError(body["projection"])


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


@pytest.mark.slow
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


def test_a_quote_with_typographic_punctuation_still_matches_its_speech(workspace, app, monkeypatch):
    """Real Cerebras (October 31) quotes "follow‑ups" with a non-breaking hyphen and curly
    apostrophes where the transcript has plain ASCII; the quote check refused the whole rundown."""

    def typographic(request):
        observed = [
            observation(request, "rundown", RUNDOWN[0], segment="r1"),
            observation(
                request,
                "rundown",
                "Prospect said follow-up is handled and money is short",
                segment="s1",
                quote="don’t have budget for follow‑ups",
            ),
        ]
        return turn_output(request, state_delta=delta(request, observations=observed))

    ws = workspace
    cid = start(ws)["id"]
    answered_by_human(ws, cid)
    chunk(ws, cid, "r1", "Hi, this is Example Seller calling about a brief introduction.", side="rep")
    chunk(ws, cid, "s1", "We don't have budget for follow-ups right now.")
    call(ws, "POST", f"/v1/calls/{cid}/hangup")
    install_fake_models(app, monkeypatch, {"finalize": typographic})
    wrap_up(ws, cid, outcome="not_interested", contact_role="right_party")
    run_jobs(ws)
    run_jobs(ws)
    assert get(ws, f"/v1/calls/{cid}")["rundown_status"] == "ready"
    assert get(ws, f"/v1/calls/{cid}/analysis")["notes"]["summary"].split("\n") == [
        RUNDOWN[0],
        "Prospect said follow-up is handled and money is short",
    ]


@pytest.mark.slow
def test_the_rundown_sees_speech_already_interpreted_during_the_call(workspace, app, monkeypatch, clock):
    """Real Cerebras (October 31) interpreted the prospect's reply live without retaining an
    observation. Finalization then got only the rep's opener and wrote that the prospect never
    answered. The whole call is context for the rundown; only new turns are interpreted again."""
    ws = workspace
    seen = {}

    def live(request):
        return turn_output(request, state_delta=delta(request))  # interprets, retains nothing

    def finalize(request):
        seen["new"] = [s["segment_id"] for s in request["recent_dialogue"]]
        seen["whole"] = [s["segment_id"] for s in request.get("call_transcript", [])]
        observed = [observation(request, "rundown", RUNDOWN[1], segment="s1")]
        return turn_output(request, state_delta=delta(request, observations=observed))

    cid = start(ws)["id"]
    answered_by_human(ws, cid)
    install_fake_models(app, monkeypatch, {"live": live})
    chunk(ws, cid, "s1", "We have no budget for this right now.")
    helper_tip(ws, clock, cid)
    chunk(ws, cid, "r1", "Understood, thanks for your time.", side="rep")
    monkeypatch.setattr(app.state.service.settings, "simulation", True)  # the carrier side is simulated
    call(ws, "POST", f"/v1/calls/{cid}/hangup")
    install_fake_models(app, monkeypatch, {"finalize": finalize})
    wrap_up(ws, cid, outcome="not_interested", contact_role="right_party")
    run_jobs(ws)
    run_jobs(ws)
    assert seen["new"] == ["r1"] and seen["whole"] == ["s1", "r1"]
    assert get(ws, f"/v1/calls/{cid}/analysis")["notes"]["summary"].split("\n") == [RUNDOWN[1]]


@pytest.mark.slow
def test_a_misnumbered_finalization_with_the_exact_context_is_kept(workspace, app, monkeypatch):
    """Real Cerebras (October 30) wrote a rundown but misnumbered its version."""

    def misnumbered(request):
        return {**finalize_rundown(request), "state_version": request["state_version"] + 1}

    cid = finish_call(workspace, app, monkeypatch, {"finalize": misnumbered})
    assert get(workspace, f"/v1/calls/{cid}")["rundown_status"] == "ready"
    assert get(workspace, f"/v1/calls/{cid}/analysis")["notes"]["summary"].split("\n") == RUNDOWN
