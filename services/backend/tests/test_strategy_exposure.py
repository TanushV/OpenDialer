"""E08/C033: finite response approaches are distinct from generated sentences."""

import pytest
from app.optimization.learning import learn_distribution
from test_live_memory import setup, turn


def test_finite_strategy_assignment():
    arms = ["control", "clarify-owner", "clarify-impact"]
    history = [
        {
            "action": "clarify-owner",
            "conversion": i < 18,
            "segment": "a",
            "propensity": 1 / 3,
            "window_status": "matured",
        }
        for i in range(20)
    ]
    history += [
        {
            "action": "clarify-impact",
            "conversion": 0,
            "segment": "a",
            "propensity": 1 / 3,
            "window_status": "matured",
        }
        for _ in range(20)
    ]
    result = learn_distribution(arms, history, segment="a", exploration=0.1)
    probabilities = result["probabilities"]
    assert set(probabilities) == set(arms)
    assert sum(probabilities.values()) == pytest.approx(1)
    assert probabilities["clarify-owner"] == pytest.approx(0.9 + 0.1 / 3)
    assert probabilities["control"] == pytest.approx(0.1 / 3)
    assert "sentence_propensity" not in result


@pytest.mark.asyncio
async def test_displayed_guidance_is_not_assumed_used_or_converted(workspace, app, monkeypatch):
    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "exposure")
    await service.coach_turn(p, call["id"], urgent=True)
    with service.db.transaction(False) as s:
        events = [r.data for r in service.scoped(s, "coach_events", p)]
        assert events
        for event in events:
            exposure = event.get("strategy_exposure")
            assert exposure is not None, (
                "Display metadata must retain the actual strategy and exposure boundary"
            )
            assert exposure["used_by_rep"] is None
            assert exposure["conversion"] is None
            assert exposure["sentence_propensity"] is None
        assert not service.scoped(s, "conversion_events", p)


def strategy_protocol(workspace, app, **changes):
    from uuid import uuid4

    from test_coaching_library import publish, source

    call, p = source(workspace, app)
    example = publish(workspace, call)
    assert example.status_code == 200, example.text
    body = {
        "family": "coaching",
        "review_reference": "Reviewed approach comparison, no wording restriction",
        "duration_days": 7,
        "request_id": str(uuid4()),
        "strategy_ids": ["control", example.json()["id"]],
        "strategy_policy": "two_arm",
        "candidate_probability": 0.5,
        **changes,
    }
    response = workspace["client"].post(
        "/v1/admin/optimization/comparisons", headers=workspace["headers"], json=body
    )
    return response, body, call, p


def test_reviewed_strategy_protocol_is_idempotent_and_rejects_changed_request(workspace, app):
    response, body, _, _ = strategy_protocol(workspace, app)
    assert response.status_code == 200, response.text
    c, h = workspace["client"], workspace["headers"]
    replay = c.post("/v1/admin/optimization/comparisons", headers=h, json=body)
    assert replay.status_code == 200 and replay.json()["id"] == response.json()["id"]
    conflict = c.post(
        "/v1/admin/optimization/comparisons", headers=h, json={**body, "candidate_probability": 0.6}
    )
    assert conflict.status_code == 409
    assert response.json()["strategies"][1]["source_version"] >= 1
    assert "sentence_propensity" not in response.json()


@pytest.mark.parametrize("policy,ids", [("two_arm", ["control", "invented"]), ("adaptive", None)])
def test_strategy_protocol_requires_current_reviewed_source_and_adaptive_evidence(
    workspace, app, policy, ids
):
    changes = {"strategy_policy": policy}
    if ids:
        changes["strategy_ids"] = ids
    response, _, _, _ = strategy_protocol(workspace, app, **changes)
    assert response.status_code in {409, 422}
    assert "review" in response.text.lower() or "evidence" in response.text.lower()


def test_strategy_assignment_is_stable_with_exact_probabilities_and_protected_work(workspace, app, clock):
    from datetime import timedelta

    from app.db import get, update

    response, body, call, p = strategy_protocol(workspace, app, candidate_probability=0.3)
    assert response.status_code == 200, response.text
    service = app.state.service
    with service.db.transaction() as s:
        campaign, lead = get(s, "campaigns", workspace["campaign_id"]), get(s, "leads", call["lead_id"])
        update(
            lead,
            requested_contact=False,
            callback_due_at=None,
            coverage_due_at=(clock() + timedelta(days=2)).isoformat(),
        )
        first = service.comparison_assignment(s, p, campaign, lead, "coaching", rep_id=p["user_id"])
        again = service.comparison_assignment(s, p, campaign, lead, "coaching", rep_id="handoff-rep")
        assert first["id"] == again["id"]
        assert first["strategy_probabilities"] == {"control": 0.7, body["strategy_ids"][1]: 0.3}
        assert first["propensity"] == first["strategy_probabilities"][first["strategy_id"]]
        update(lead, requested_contact=True)
        protected = service.strategy_context(s, p, campaign, lead, first)
        assert protected["strategy_id"] == "control"
        assert protected["probabilities"] == {"control": 1.0}
        assert protected["reason"] == "protected_work"
        assert get(s, "experiment_assignments", first["id"]).data["strategy_id"] == first["strategy_id"]


def test_stopped_or_withdrawn_strategy_keeps_assignment_but_no_treatment(workspace, app, clock):
    from datetime import timedelta

    from app.db import get, update

    response, body, call, p = strategy_protocol(workspace, app)
    assert response.status_code == 200, response.text
    service = app.state.service
    with service.db.transaction() as s:
        campaign, lead = get(s, "campaigns", workspace["campaign_id"]), get(s, "leads", call["lead_id"])
        update(
            lead,
            requested_contact=False,
            callback_due_at=None,
            coverage_due_at=(clock() + timedelta(days=2)).isoformat(),
        )
        assignment = service.comparison_assignment(s, p, campaign, lead, "coaching", rep_id=p["user_id"])
        update(get(s, "snippets", body["strategy_ids"][1]), approved=False)
        context = service.strategy_context(s, p, campaign, lead, assignment)
        assert context["strategy_id"] == "control" and context["reason"] == "source_changed"
        assert (
            get(s, "experiment_assignments", assignment["id"]).data["comparison_id"] == response.json()["id"]
        )


@pytest.mark.asyncio
async def test_display_ack_and_rep_use_are_separate_durable_versioned_receipts(
    workspace, app, monkeypatch, clock
):
    from datetime import timedelta
    from uuid import uuid4

    from app.db import get

    call, p, _, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "shown")
    card = await service.coach_turn(p, call["id"], urgent=True)
    c, h, path = workspace["client"], workspace["headers"], f"/v1/calls/{call['id']}/coach/feedback"
    with service.db.transaction(False) as s:
        before = get(s, "coach_events", card["id"]).data["strategy_exposure"]
        assert before["displayed_at"] is None and before["used_by_rep"] is None
    body = {"coach_event_id": card["id"], "displayed": True, "request_id": str(uuid4())}
    first = c.post(path, headers=h, json=body)
    assert first.status_code == 200, first.text
    replay = c.post(path, headers=h, json=body)
    assert replay.json()["version"] == first.json()["version"]
    used = c.post(
        path,
        headers=h,
        json={
            "coach_event_id": card["id"],
            "used_by_rep": False,
            "expected_version": first.json()["version"],
            "request_id": str(uuid4()),
        },
    )
    assert used.status_code == 200, used.text
    stale = c.post(
        path,
        headers=h,
        json={
            "coach_event_id": card["id"],
            "used_by_rep": True,
            "expected_version": first.json()["version"],
            "request_id": str(uuid4()),
        },
    )
    assert stale.status_code == 409
    clock.now += timedelta(seconds=301)
    assert service.raw.get("help:" + p["tenant_id"] + ":" + card["id"]) is None
    with service.db.transaction(False) as s:
        exposure = get(s, "coach_events", card["id"]).data["strategy_exposure"]
        assert exposure["displayed_at"] is not None and exposure["used_by_rep"] is False
        assert exposure["conversion"] is None and exposure["sentence_propensity"] is None
        assert not service.scoped(s, "conversion_events", p)
    view = c.get(path, headers=h, params={"coach_event_id": card["id"]})
    assert view.status_code == 200 and view.json()["used_by_rep"] is False


@pytest.mark.asyncio
async def test_expired_or_wrong_rep_card_cannot_be_acknowledged_displayed(workspace, app, monkeypatch, clock):
    from datetime import timedelta
    from uuid import uuid4

    from app.db import get, update

    call, p, _, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "expired-display")
    card = await service.coach_turn(p, call["id"], urgent=True)
    path = f"/v1/calls/{call['id']}/coach/feedback"
    with service.db.transaction() as s:
        update(get(s, "coach_events", card["id"]), user_id="other-rep")
    denied = workspace["client"].post(
        path,
        headers=workspace["headers"],
        json={"coach_event_id": card["id"], "displayed": True, "request_id": str(uuid4())},
    )
    assert denied.status_code in {403, 404}, denied.text
    with service.db.transaction() as s:
        update(get(s, "coach_events", card["id"]), user_id=p["user_id"])
    clock.now += timedelta(seconds=301)
    expired = workspace["client"].post(
        path,
        headers=workspace["headers"],
        json={"coach_event_id": card["id"], "displayed": True, "request_id": str(uuid4())},
    )
    assert expired.status_code == 409


def test_strategy_rewards_use_matured_fixed_goal_not_helpfulness_or_late_conversions(clock):
    from datetime import timedelta

    from app.optimization.experiments import strategy_rewards

    t = clock() - timedelta(days=20)
    study = {
        "id": "study",
        "goal": "meeting_held",
        "window_days": 14,
        "strategy_ids": ["control", "approach"],
        "starts_at": t.isoformat(),
    }
    assignments = [
        {
            "id": "a",
            "comparison_id": "study",
            "lead_id": "lead",
            "strategy_id": "approach",
            "propensity": 0.5,
            "assigned_at": t.isoformat(),
            "originally_eligible": True,
        }
    ]

    def event(**changes):
        return {
            "id": "e",
            "lead_id": "lead",
            "kind": "meeting_held",
            "occurred_at": (t + timedelta(days=1)).isoformat(),
            "label_available_at": (t + timedelta(days=2)).isoformat(),
            **changes,
        }

    assert strategy_rewards(study, assignments, [], now=clock())[0]["conversion"] == 0
    assert strategy_rewards(study, assignments, [event()], now=clock())[0]["conversion"] == 1
    assert strategy_rewards(study, assignments, [event(retracted=True)], now=clock())[0]["conversion"] == 0
    assert (
        strategy_rewards(study, assignments, [event(kind="meeting_booked")], now=clock())[0]["conversion"]
        == 0
    )
    assert (
        strategy_rewards(
            study,
            assignments,
            [event(label_available_at=(clock() + timedelta(days=1)).isoformat())],
            now=clock(),
        )[0]["conversion"]
        == 0
    )
    pending = [{**assignments[0], "assigned_at": (clock() - timedelta(days=1)).isoformat()}]
    assert strategy_rewards(study, pending, [], now=clock())[0]["conversion"] is None


@pytest.mark.asyncio
async def test_protocol_stop_during_generation_fences_the_old_strategy(workspace, app, monkeypatch, clock):
    from datetime import timedelta

    from app.db import get, update
    from test_remediation_coach import ordinary_advice

    response, _, call, p = strategy_protocol(workspace, app)
    assert response.status_code == 200, response.text
    service = app.state.service
    with service.db.transaction() as s:
        campaign, lead = get(s, "campaigns", workspace["campaign_id"]), get(s, "leads", call["lead_id"])
        update(
            lead,
            requested_contact=False,
            callback_due_at=None,
            coverage_due_at=(clock() + timedelta(days=2)).isoformat(),
        )
        assignment = service.comparison_assignment(s, p, campaign, lead, "coaching", rep_id=p["user_id"])
        update(get(s, "call_attempts", call["id"]), coaching_comparison=assignment)
    requests = []

    class Provider:
        last_provider, last_model, last_usage, last_request_id = "Cerebras", "fixture", {}, None

        async def suggest(self, request, **kwargs):
            requests.append(request)
            workspace["client"].post(
                f"/v1/admin/optimization/comparisons/{response.json()['id']}/stop",
                headers=workspace["headers"],
                json={"reason": "Stop reviewed experiment"},
            )
            self.last_request_id, self.last_usage = "stop-race", {"cost": 0.001}
            return ordinary_advice(request, request["recent_dialogue"][-1]["segment_id"])

    monkeypatch.setattr(service, "configured_coach", lambda _: (Provider(), "openrouter"))
    monkeypatch.setattr(service.settings, "simulation", False)
    turn(service, p, call, "protocol-race")
    result = await service.coach_turn(p, call["id"], urgent=True)
    assert requests and "strategy_context" in requests[0]
    assert result["action"] == "NO_ACTION"


def test_observed_use_requires_post_display_rep_speech_and_independent_review(clock):
    from datetime import timedelta

    from app.coaching.context import merge_semantic_state, upgrade_state
    from app.optimization.experiments import observed_strategy_uses

    segment = {
        "segment_id": "rep-after",
        "revision": 1,
        "side": "rep",
        "is_final": True,
        "text": "Who is involved in approving the next step in this particular process?",
        "server_received_at": clock().isoformat(),
    }
    exposure = {
        "id": "card",
        "strategy_id": "approach",
        "source_version": 2,
        "approach": "Clarify decision ownership",
        "displayed_at": (clock() - timedelta(seconds=1)).isoformat(),
    }
    delta = {
        "stage": "discovery",
        "observations": [
            {
                "kind": "approach",
                "text": "Decision ownership was explored.",
                "approach": "Clarify decision ownership",
                "state": "confirmed",
                "quote": segment["text"],
                "sources": [{"kind": "segment", "id": "rep-after", "version": 1}],
                "replaces": [],
                "commitment": None,
                "observed_guidance_id": "card",
            }
        ],
        "rubric_items": [],
        "tracker_matches": [],
        "interpreted_segments": [],
    }
    state = upgrade_state({}, call_id="call")
    merged = merge_semantic_state(state, delta, [segment], at=clock(), exposures=[exposure])
    item = merged["observations"][0]
    assert item["strategy_id"] == "approach" and item["review_status"] == "pending"
    assert observed_strategy_uses([item], "card") == []
    reviewed = {
        **item,
        "review_status": "reviewed",
        "epistemic_status": "model_reviewed",
        "review_receipt_id": "review",
    }
    uses = observed_strategy_uses([reviewed], "card")
    assert uses == [
        {"id": item["id"], "version": 1, "strategy_id": "approach", "review_receipt_id": "review"}
    ]
    assert observed_strategy_uses([{**reviewed, "invalidated_at": clock().isoformat()}], "card") == []
    for bad in [
        {**segment, "side": "remote"},
        {**segment, "server_received_at": (clock() - timedelta(seconds=5)).isoformat()},
    ]:
        with pytest.raises(ValueError):
            merge_semantic_state(state, delta, [bad], at=clock(), exposures=[exposure])
    with pytest.raises(ValueError):
        merge_semantic_state(state, delta, [segment], at=clock(), exposures=[])


def test_supported_adaptive_policy_retains_exact_mixture_and_unknown_use(clock):
    from app.optimization.experiments import strategy_choice

    arms = ["control", "approach"]
    policy = learn_distribution(
        arms,
        [
            {"action": "approach", "conversion": 1, "propensity": 0.5, "window_status": "matured"}
            for _ in range(20)
        ],
        exploration=0.1,
    )
    study = {
        "id": "approved-study",
        "strategy_ids": arms,
        "strategy_policy": "adaptive",
        "initial_strategy_policy": policy,
    }
    a = strategy_choice(study, "lead", "rep")
    assert a == strategy_choice(study, "lead", "rep")
    assert a["strategy_probabilities"] == {"control": 0.05, "approach": pytest.approx(0.95)}
    assert a["propensity"] == a["strategy_probabilities"][a["strategy_id"]]


def test_finite_report_preserves_assigned_denominator_and_display_use_distinctions(clock):
    from datetime import timedelta

    from comparison_oracle import comparison_report

    at = (clock() - timedelta(days=30)).isoformat()
    study = {
        "id": "study",
        "family": "coaching",
        "population": ["lead", "other"],
        "goal": "meeting_held",
        "window_days": 14,
        "strategy_ids": ["control", "approach"],
        "strategy_policy": "two_arm",
        "candidate_probability": 0.3,
        "ends_at": (clock() - timedelta(days=20)).isoformat(),
    }
    assignment = {
        "id": "assignment",
        "comparison_id": "study",
        "lead_id": "lead",
        "arm": "candidate",
        "strategy_id": "approach",
        "propensity": 0.3,
        "rep_id": "rep",
        "assigned_at": at,
    }
    card = {
        "id": "card",
        "call_id": "call",
        "strategy_exposure": {
            "comparison_id": "study",
            "strategy_id": "approach",
            "generated_at": at,
            "displayed_at": None,
            "used_by_rep": None,
        },
    }
    report = comparison_report(
        study, [assignment], [], [], [], now=clock(), goal="meeting_held", window_days=14, exposures=[card]
    )
    assert report["assigned_leads"] == 1 and report["unassigned_leads"] == 1
    assert report["strategy_exposure"]["generated"] == 1
    assert report["strategy_exposure"]["displayed"] == 0
    assert report["strategy_exposure"]["use_unknown"] == 1
    card["strategy_exposure"].update(displayed_at=at, used_by_rep=False)
    report = comparison_report(
        study, [assignment], [], [], [], now=clock(), goal="meeting_held", window_days=14, exposures=[card]
    )
    assert report["strategy_exposure"]["displayed"] == 1
    assert report["strategy_exposure"]["reported_used"] == 0
    assert report["strategy_exposure"]["reported_not_used"] == 1
    assert not report["sufficient_evidence"]
    assert sum(report["strategy_policy_snapshot"]["probabilities"].values()) == pytest.approx(1)
    observation = {
        "id": "observed-approach",
        "version": 1,
        "kind": "approach",
        "call_id": "call",
        "observed_guidance_id": "card",
        "strategy_id": "approach",
        "review_status": "reviewed",
        "epistemic_status": "model_reviewed",
        "review_receipt_id": "independent-review",
        "state": "confirmed",
    }
    for invalid in (False, True):
        observed = {**observation, "invalidated_at": at if invalid else None}
        report = comparison_report(
            study,
            [assignment],
            [],
            [],
            [],
            now=clock(),
            goal="meeting_held",
            window_days=14,
            exposures=[card],
            observations=[observed, {**observed, "call_id": "foreign"}],
        )
        assert report["strategy_exposure"]["observed_in_speech"] == int(not invalid)
        assert report["strategy_exposure"]["reported_used"] == 0
        assert report["arms"]["candidate"]["conversions"] == 0


@pytest.mark.parametrize(
    "probabilities",
    [
        {"control": -0.2, "approach": 1.2},
        {"control": float("nan"), "approach": 0.5},
        {"control": float("inf"), "approach": float("-inf")},
    ],
)
def test_invalid_saved_strategy_mixture_cannot_assign(probabilities):
    from app.optimization.experiments import strategy_choice

    with pytest.raises(ValueError):
        strategy_choice(
            {
                "id": "study",
                "strategy_policy": "adaptive",
                "strategy_ids": ["control", "approach"],
                "initial_strategy_policy": {"probabilities": probabilities},
            },
            "lead",
            "rep",
        )


def test_report_keeps_reviewed_speech_metadata_after_context_trim_and_corrects_it(workspace, app, clock):
    from app.coaching.context import upgrade_state
    from app.db import get, put
    from app.optimization.projections import read_comparison_metrics
    from test_projection_replay import project_pending

    response, body, call, p = strategy_protocol(workspace, app)
    assert response.status_code == 200, response.text
    service = app.state.service
    with service.db.transaction() as s:
        card = put(
            s,
            "coach_events",
            p["tenant_id"],
            p["seller_id"],
            {
                "call_id": call["id"],
                "strategy_exposure": {
                    "comparison_id": response.json()["id"],
                    "strategy_id": body["strategy_ids"][1],
                    "displayed_at": clock().isoformat(),
                    "used_by_rep": None,
                },
            },
            now=clock(),
        )
        card_id = card.id
    state = upgrade_state({}, call_id=call["id"])
    for revision, invalid in ((1, False), (2, True)):
        with service.db.transaction() as s:
            record = get(s, "call_attempts", call["id"])
            state["_lineage_updates"] = [
                {
                    "id": "reviewed-match",
                    "version": revision,
                    "kind": "approach",
                    "sources": [],
                    "state": "retracted" if invalid else "confirmed",
                    "observed_guidance_id": card_id,
                    "strategy_id": body["strategy_ids"][1],
                    "review_status": "reviewed",
                    "epistemic_status": "model_reviewed",
                    "review_receipt_id": "review",
                    "available_at": clock().isoformat(),
                }
            ]
            service._store_conversation(s, p, record, state)
        project_pending(service, p)
        report = read_comparison_metrics(service, p)[0]
        assert report["strategy_exposure"]["observed_in_speech"] == int(not invalid)
        assert report["strategy_exposure"]["use_unknown"] == 1


def test_adaptive_protocol_accepts_only_current_matching_owned_trial_receipt(workspace, app, clock):
    from datetime import timedelta
    from uuid import uuid4

    from app.db import put

    response, body, _, p = strategy_protocol(workspace, app)
    assert response.status_code == 200, response.text
    clock.now += timedelta(days=30)
    service = app.state.service
    c = workspace["client"]
    auth = c.post(
        "/v1/auth/session", json={"email": "operator@example.test", "password": "a-test-only-password-123"}
    )
    assert auth.status_code == 200, auth.text
    h = {"Authorization": "Bearer " + auth.json()["access_token"]}
    with service.db.transaction() as s:
        _, _, epoch, key = service.report_scope(s, p)
        source = {
            **response.json(),
            "sufficient_evidence": True,
            "strategy_policy_snapshot": {"probabilities": {"control": 0.05, body["strategy_ids"][1]: 0.95}},
        }
        put(
            s,
            "report_projections",
            p["tenant_id"],
            p["seller_id"],
            {
                "report": {"comparisons": [source]},
                "epoch": epoch,
                "source_revision": service.report_revision(s, p),
                "expires_at": (clock() + timedelta(minutes=5)).isoformat(),
            },
            key=key,
            now=clock(),
        )
    body.update(
        request_id=str(uuid4()), strategy_policy="adaptive", evidence_comparison_id=response.json()["id"]
    )
    result = c.post("/v1/admin/optimization/comparisons", headers=h, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["initial_strategy_policy"]["probabilities"]["control"] == 0.05


@pytest.mark.asyncio
@pytest.mark.database
async def test_display_telemetry_does_not_race_the_first_rep_feedback_edit(workspace, app, monkeypatch):
    from uuid import uuid4

    call, p, _, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "feedback-race")
    card = await service.coach_turn(p, call["id"], urgent=True)
    c, h = workspace["client"], workspace["headers"]
    path = f"/v1/calls/{call['id']}/coach/feedback"
    previous = c.get(path, headers=h, params={"coach_event_id": card["id"]}).json()
    ack = c.post(
        path,
        headers=h,
        json={"coach_event_id": card["id"], "request_id": str(uuid4()), "displayed": True, "render_ms": 10},
    )
    assert ack.status_code == 200
    use = c.post(
        path,
        headers=h,
        json={
            "coach_event_id": card["id"],
            "request_id": str(uuid4()),
            "expected_version": previous["version"],
            "used_by_rep": False,
        },
    )
    assert use.status_code == 200, "Rendering telemetry is not a conflicting human correction: " + use.text


@pytest.mark.asyncio
async def test_legacy_availability_control_still_interprets_the_conversation(
    workspace, app, monkeypatch, clock
):
    from datetime import timedelta

    from app.db import get, update

    call, p, requests, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    with service.db.transaction() as s:
        update(get(s, "call_attempts", call["id"]), coaching_comparison={"arm": "control"})
    turn(service, p, call, "control-memory")
    clock.now += timedelta(milliseconds=301)
    result = await service.coach_turn(p, call["id"])
    assert requests, "A display experiment must not discard long-call interpretation"
    assert result["action"] == "NO_ACTION"
    assert service.analysis(p, call["id"])["call_memory"]["rundown_points"]


def test_speech_reviewer_receives_the_reviewed_approach_meaning_not_only_an_id(workspace, app):
    from app.db import get, put, update
    from test_coaching_library import LESSON

    response, body, call, p = strategy_protocol(workspace, app)
    assert response.status_code == 200, response.text
    service = app.state.service
    with service.db.transaction() as s:
        event = put(
            s,
            "coach_events",
            p["tenant_id"],
            p["seller_id"],
            {
                "call_id": call["id"],
                "user_id": p["user_id"],
                "strategy_exposure": {
                    "strategy_id": body["strategy_ids"][1],
                    "source_version": 1,
                    "displayed_at": service.clock().isoformat(),
                },
            },
            now=service.clock(),
        )
        state = service._by_key(s, p, "conversation_states", call["id"])
        update(state, displayed_coach_event_ids=[event.id])
        exposures = service.strategy_exposures_in(s, p, state)
        assert exposures[0].get("approach") == LESSON, "An opaque ID cannot support a meaning review"
        update(get(s, "snippets", body["strategy_ids"][1]), approved=False)
        assert service.strategy_exposures_in(s, p, state)[0]["approach"] is None


@pytest.mark.parametrize("correction", ["future", "current", "provisional", "unknown_time"])
def test_strategy_report_uses_disposition_known_at_report_time(clock, correction):
    from datetime import timedelta

    from comparison_oracle import comparison_report

    start = clock() - timedelta(days=30)
    known = (start + timedelta(days=1)).isoformat()
    study = {
        "id": "study",
        "family": "coaching",
        "population": ["lead"],
        "goal": "meeting_held",
        "window_days": 14,
        "strategy_ids": ["control", "approach"],
        "strategy_policy": "two_arm",
        "candidate_probability": 0.5,
        "ends_at": known,
    }
    assignment = {
        "id": "a",
        "comparison_id": "study",
        "lead_id": "lead",
        "strategy_id": "approach",
        "propensity": 0.5,
        "arm": "candidate",
        "rep_id": "rep",
        "assigned_at": start.isoformat(),
    }
    original = {"outcome": "meeting_held", "outcome_source": "rep", "outcome_available_at": known}
    call = {
        "id": "call",
        "lead_id": "lead",
        "started_at": start.isoformat(),
        "ended_at": known,
        "submitted_at": start.isoformat(),
        **original,
    }
    if correction in {"future", "current"}:
        call.update(
            outcome="not_interested",
            outcome_history=[original],
            outcome_available_at=(clock() + timedelta(days=1 if correction == "future" else -1)).isoformat(),
        )
    elif correction == "provisional":
        call["outcome_source"] = "llm_provisional"
    else:
        call["outcome_available_at"] = None
    report = comparison_report(
        study, [assignment], [call], [], [], now=clock(), goal="meeting_held", window_days=14
    )
    assert report["arms"]["candidate"]["matured_conversions"] == int(correction == "future")


def test_adaptive_seed_includes_confirmed_call_only_goal_evidence(workspace, app, clock):
    from datetime import timedelta

    from app.db import get, put, update
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    start = clock() - timedelta(days=30)
    known = (start + timedelta(days=1)).isoformat()
    with service.db.transaction() as s:
        lead = service.scoped(s, "leads", p)[0]
        base = {
            "id": "source-study",
            "family": "coaching",
            "population": [lead.id],
            "goal": "meeting_held",
            "window_days": 14,
            "strategy_ids": ["control", "approach"],
            "strategy_policy": "two_arm",
            "candidate_probability": 0.5,
            "ends_at": known,
        }
        adaptive = {
            **base,
            "id": "adaptive-study",
            "strategy_policy": "adaptive",
            "evidence_comparison_id": base["id"],
        }
        campaign = get(s, "campaigns", workspace["campaign_id"])
        update(campaign, controlled_comparisons=[base, adaptive])
        put(
            s,
            "experiment_assignments",
            p["tenant_id"],
            p["seller_id"],
            {
                "comparison_id": base["id"],
                "lead_id": lead.id,
                "strategy_id": "approach",
                "propensity": 0.5,
                "arm": "candidate",
                "rep_id": p["user_id"],
                "assigned_at": start.isoformat(),
            },
            now=clock(),
        )
        put(
            s,
            "call_attempts",
            p["tenant_id"],
            p["seller_id"],
            {
                "lead_id": lead.id,
                "started_at": start.isoformat(),
                "ended_at": known,
                "submitted_at": start.isoformat(),
                "outcome": "meeting_held",
                "outcome_source": "rep",
                "outcome_available_at": known,
            },
            now=clock(),
        )
    from app.optimization.projections import read_comparison_metrics
    from test_projection_replay import project_pending

    project_pending(service, p)
    reports = read_comparison_metrics(service, p)
    prior, ongoing = [r["strategy_policy_snapshot"] for r in reports]
    assert prior["observations"]["approach"] == ongoing["observations"]["approach"] == 1
    assert ongoing["estimates"]["approach"] == prior["estimates"]["approach"], (
        "The same frozen goal evidence must inform the original trial and the approved adaptive seed"
    )
