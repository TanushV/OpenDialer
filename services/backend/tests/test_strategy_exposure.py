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
