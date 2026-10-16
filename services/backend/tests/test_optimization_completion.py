"""Remaining release gates are written before their implementation."""

from datetime import timedelta

from app.db import put, update
from conftest import start


def principal(workspace):
    return {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


def test_number_experiment_changes_only_new_allocation_and_keeps_a_stable_identity():
    from app.optimization.numbering import select_number

    inventory = [
        {"id": "regional", "region": "415", "lifecycle": "healthy", "daily_limit": 20},
        {"id": "general", "region": "general", "lifecycle": "healthy", "daily_limit": 20},
    ]
    assert select_number({"region": "415"}, inventory, strategy="general_first")["number_id"] == "general"
    assert (
        select_number({"region": "415"}, inventory, bound_id="regional", strategy="general_first")[
            "number_id"
        ]
        == "regional"
    )
    assert select_number({"region": "415"}, inventory, strategy="regional_first")["number_id"] == "regional"


def test_experiment_assignments_balance_within_reps_and_cluster_regions():
    from app.optimization.experiments import assign_arm

    assignments = []
    for rep in ("r1", "r2"):
        for index in range(10):
            arm = assign_arm("coaching", "epoch", "lead-" + rep + str(index), rep, assignments)
            assignments.append({"family": "coaching", "rep_id": rep, "arm": arm})
        assert sum(a["arm"] == "candidate" for a in assignments if a["rep_id"] == rep) == 5
    assert len({assign_arm("number_allocation", "epoch", str(i), "415", []) for i in range(30)}) == 1


def test_controlled_comparisons_are_reviewed_off_by_default_and_report_all_population(workspace):
    c, h = workspace["client"], workspace["headers"]
    report = c.get("/v1/optimization", headers=h).json()
    assert report["comparisons"] == []
    for family in ("number_allocation", "coaching"):
        response = c.post(
            "/v1/admin/optimization/comparisons",
            headers=h,
            json={
                "family": family,
                "review_reference": "Reviewed synthetic protocol",
                "duration_days": 7,
            },
        )
        assert response.status_code == 200, response.text
    result = c.get("/v1/optimization", headers=h).json()
    assert {r["family"] for r in result["comparisons"]} == {"number_allocation", "coaching"}
    assert all(
        r["original_eligible_leads"] == result["metrics"]["original_eligible_leads"]
        for r in result["comparisons"]
    )
    assert all(not r["sufficient_evidence"] for r in result["comparisons"])
    call = start(workspace)
    result = c.get("/v1/optimization", headers=h).json()
    coach = next(r for r in result["comparisons"] if r["family"] == "coaching")
    assert coach["assigned_leads"] == 1 and coach["rep_strata"]
    number = next(r for r in result["comparisons"] if r["family"] == "number_allocation")
    assert number["cluster_count"] >= 1
    assert (
        c.post(
            "/v1/admin/optimization/comparisons/" + coach["id"] + "/stop",
            headers=h,
            json={"reason": "Finish synthetic review"},
        ).status_code
        == 200
    )
    assert c.get(f"/v1/calls/{call['id']}", headers=h).status_code == 200


def test_cohort_history_is_available_after_goal_changes(workspace):
    c, h = workspace["client"], workspace["headers"]
    original = c.get("/v1/optimization", headers=h).json()["trial"]
    c.put("/v1/admin/optimization", headers=h, json={"conversion_goal": "sale_won"})
    history = c.get("/v1/optimization", headers=h, params={"epoch": original["epoch"]})
    assert history.status_code == 200, history.text
    assert history.json()["trial"]["epoch"] == original["epoch"]
    assert history.json()["trial"]["goal"] == "meeting_held"
    assert sum(a["leads"] for a in history.json()["trial"]["arms"].values()) > 0


def test_rollback_cannot_activate_an_unreviewed_candidate(workspace, app, clock):
    with app.state.db.transaction() as s:
        model = put(
            s,
            "model_versions",
            workspace["tenant_id"],
            workspace["seller_id"],
            {
                "active": False,
                "source_dataset_id": "owned",
                "goal": "meeting_held",
                "metrics": {"brier": 0.1, "pooled_baseline_brier": 0.2},
            },
            now=clock.now,
        )
        model_id = model.id
    response = workspace["client"].post(
        f"/v1/admin/models/{model_id}/rollback",
        headers=workspace["headers"],
        json={"reason": "Attempt an unreviewed bypass"},
    )
    assert response.status_code == 409


def test_legacy_training_route_cannot_import_arbitrary_historical_features(workspace):
    response = workspace["client"].post(
        "/v1/admin/models/train",
        headers=workspace["headers"],
        json={"rows": [{"human": 1, "features": {"future_outcome": 1}}] * 100},
    )
    assert response.status_code == 422


def test_trial_cannot_start_from_a_nonexistent_owned_dataset(workspace, app, clock):
    with app.state.db.transaction() as s:
        model = put(
            s,
            "model_versions",
            workspace["tenant_id"],
            workspace["seller_id"],
            {
                "active": False,
                "source_dataset_id": "missing-source",
                "goal": "meeting_held",
                "metrics": {"brier": 0.1, "pooled_baseline_brier": 0.2},
            },
            now=clock.now,
        )
        model_id = model.id
    response = workspace["client"].post(
        f"/v1/admin/models/{model_id}/start-trial",
        headers=workspace["headers"],
        json={"review_reference": "Missing source must fail"},
    )
    assert response.status_code == 409


def test_model_trial_exposure_report_preserves_uncalled_population(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        campaign = service.scoped(s, "campaigns", p)[0]
        study = {
            "model_id": "fixture-model",
            "starts_at": clock.now.isoformat(),
            "ends_at": (clock.now + timedelta(days=1)).isoformat(),
            "max_leads": 100,
        }
        update(campaign, optimization_trial=study)
    report = service.optimization_report(p)
    assert report["trial"]["model_exposure"]["candidate_tested_model_attempts"] == 0
    assert not report["trial"]["sufficient_evidence"]
    assert (
        sum(r["leads"] for r in report["trial"]["arms"].values())
        == report["metrics"]["original_eligible_leads"]
    )


def test_later_conversions_have_a_scoped_auditable_list(workspace, clock):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    event = c.post(
        f"/v1/leads/{call['lead_id']}/conversions",
        headers=h,
        json={
            "event_id": "auditable-result",
            "kind": "meeting_held",
            "call_id": call["id"],
            "occurred_at": clock.now.isoformat(),
            "source_reference": "Observed result",
        },
    ).json()
    response = c.get(f"/v1/leads/{call['lead_id']}/conversions", headers=h)
    assert response.status_code == 200
    assert response.json()[0]["id"] == event["id"]


async def test_semantic_card_selection_and_prior_state_survive_two_live_turns(
    workspace, app, clock, monkeypatch
):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    text = "Where do duplicated entries most affect your team's work?"
    quote = "Duplicated entries consume two hours every week."
    with service.db.transaction() as s:
        playbook = service.approved_playbook(s, p)
        update(
            playbook,
            cards=playbook.data["cards"] + [{"id": "duplication", "tags": ["data_quality"], "text": text}],
        )
    requests = []

    class Provider:
        last_usage, last_request_id, model = {}, None, "fixture"

        async def suggest(self, request, deadline_ms=1200):
            requests.append(request)
            if len(requests) == 2:
                return {"action": "NO_ACTION", "state_version": request["state_version"]}
            assert any(card["id"] == "duplication" for card in request["approved_cards"])
            return {
                "action": "SHOW_CARD",
                "kind": "DISCOVERY",
                "say_or_ask": text,
                "reason": "The stated duplication needs clarification.",
                "evidence_segment_ids": ["one"],
                "playbook_card_ids": ["duplication"],
                "state_version": request["state_version"],
                "expires_in_ms": 7000,
                "state_update": {
                    "stage": "discovery",
                    "needs": [
                        {
                            "text": quote,
                            "quote": quote,
                            "evidence_segment_ids": ["one"],
                            "status": "confirmed",
                        }
                    ],
                },
            }

    app.state.settings.simulation = False
    monkeypatch.setattr(service, "configured_coach", lambda p: (Provider(), "openrouter"))
    for identifier, utterance in (("one", quote), ("two", "Can we arrange a meeting?")):
        await service.transcript(
            p,
            call["id"],
            {"segment_id": identifier, "revision": 1, "side": "remote", "text": utterance, "is_final": True},
            defer_coach=True,
        )
        await service.coach_turn(p, call["id"], urgent=True)
    assert requests[1]["state"]["needs"][0]["text"] == quote
    assert requests[0]["prior_call_context"] == []
    c, h = workspace["client"], workspace["headers"]
    # The older card is obsolete after a new state version.
    assert c.get(f"/v1/calls/{call['id']}", headers=h).json()["coach"] is None


def test_paid_replay_attributes_each_actual_provider_attempt(workspace, app, monkeypatch):
    class Provider:
        model, last_provider, last_model = "fixture", "Cerebras", "fixture"
        last_usage, last_request_id = {}, None

        async def suggest(self, request, deadline_ms=1200):
            self.last_usage = {"prompt_tokens": 2, "cost": 0.001}
            self.last_request_id = "fixture-" + request["id"]
            return {"action": "NO_ACTION"}

    monkeypatch.setattr(app.state.service, "configured_coach", lambda p: (Provider(), "openrouter"))
    app.state.settings.simulation = False
    cases = [
        {
            "id": str(i),
            "state_version": 1,
            "expected_action": "NO_ACTION",
            "recent_dialogue": [],
            "approved_cards": [],
        }
        for i in range(2)
    ]
    response = workspace["client"].post(
        "/v1/admin/coaching/replay",
        headers=workspace["headers"],
        json={"cases": cases, "review_reference": "Permissioned fixture without network"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["valid_fraction"] == 1
    with app.state.db.transaction(False) as s:
        usage = [
            r.data
            for r in app.state.service.scoped(s, "usage_ledger", principal(workspace))
            if r.data.get("kind") == "coach_replay"
        ]
    assert len(usage) == 2 and all(row["object_id"] for row in usage)


def test_baseline_calling_and_planning_do_not_import_the_optional_ml_runtime(workspace, app, monkeypatch):
    import sys

    from conftest import reserve

    monkeypatch.setitem(sys.modules, "app.ml.training", None)
    result = reserve(workspace)
    assert result.status_code == 200, result.text
    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        lead = service.scoped(s, "leads", p)[0]
        assert service.slot_predictor(s, p, lead, service.optimization_config(s, p)) is None


def test_comparison_keeps_its_declared_goal_when_the_campaign_goal_later_changes(clock):
    from app.optimization.experiments import comparison_report

    assigned_at = (clock.now - timedelta(days=3)).isoformat()
    ended_at = (clock.now - timedelta(days=2)).isoformat()
    study = {
        "id": "study",
        "family": "coaching",
        "population": ["lead"],
        "starts_at": assigned_at,
        "ends_at": ended_at,
        "goal": "meeting_held",
        "window_days": 14,
    }
    assignments = [
        {
            "comparison_id": "study",
            "lead_id": "lead",
            "assigned_at": assigned_at,
            "family": "coaching",
            "arm": "candidate",
            "rep_id": "r",
        }
    ]
    calls = [
        {
            "id": "call",
            "lead_id": "lead",
            "started_at": assigned_at,
            "ended_at": ended_at,
            "outcome": "meeting_held",
            "submitted_at": assigned_at,
            "submission_allowed": True,
        }
    ]
    result = comparison_report(
        study, assignments, calls, [], [], now=clock.now, goal="meeting_held", window_days=14
    )
    assert result["arms"]["candidate"]["conversions"] == 1


def test_home_metrics_use_the_same_funnel_and_later_conversion_evidence(workspace, clock):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    c.post(f"/v1/calls/{call['id']}/disposition", headers=h, json={"outcome": "qualified_conversation"})
    metrics = c.get("/v1/workspace", headers=h).json()["metrics"]
    assert metrics["human_pickups"] == 1 and metrics["right_party_unique"] == 1
    event = c.post(
        f"/v1/leads/{call['lead_id']}/conversions",
        headers=h,
        json={
            "event_id": "home-evidence",
            "kind": "meeting_held",
            "call_id": call["id"],
            "occurred_at": clock.now.isoformat(),
            "source_reference": "Observed held meeting",
        },
    ).json()
    assert c.get("/v1/workspace", headers=h).json()["metrics"]["held_meetings"] == 1
    c.post(
        f"/v1/leads/{call['lead_id']}/conversions/{event['id']}/retract",
        headers=h,
        json={"reason": "Incorrect entry"},
    )
    assert c.get("/v1/workspace", headers=h).json()["metrics"]["held_meetings"] == 0
    c.post(
        "/v1/suppressions", headers=h, json={"phone": "+12125550190", "reason": "opt_out"}
    ).raise_for_status()
    c.post(
        "/v1/leads/import",
        headers=h,
        json={
            "campaign_id": workspace["campaign_id"],
            "rows": [{"name": "Excluded existing opt-out", "phone": "+12125550190"}],
        },
    )
    metrics = c.get("/v1/workspace", headers=h).json()["metrics"]
    assert metrics["original_cohort"] == 4 and metrics["original_eligible_denominator"] == 3


def test_visible_card_ttl_is_reported_by_the_authority(workspace, clock):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(
        f"/v1/calls/{call['id']}/transcript",
        headers=h,
        json={
            "segment_id": "budget",
            "revision": 1,
            "side": "remote",
            "text": "We have no budget for this.",
            "is_final": True,
        },
    )
    clock.now += timedelta(seconds=3)
    card = c.get(f"/v1/calls/{call['id']}", headers=h).json()["coach"]
    assert 0 < card["remaining_ms"] <= 4000
    assert card["latency"]["stt_ms"] is None


def test_render_measurements_do_not_count_as_human_quality_reviews(workspace):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(
        f"/v1/calls/{call['id']}/transcript",
        headers=h,
        json={
            "segment_id": "timing",
            "revision": 1,
            "side": "remote",
            "text": "We have no budget.",
            "is_final": True,
        },
    )
    card = c.get(f"/v1/calls/{call['id']}", headers=h).json()["coach"]
    c.post(
        f"/v1/calls/{call['id']}/coach/feedback",
        headers=h,
        json={"coach_event_id": card["id"], "render_ms": 12, "event_to_render_ms": 80},
    )
    report = c.get("/v1/optimization", headers=h).json()["coaching"]
    assert report["reviewed_cards"] == 0 and report["useful_fraction"] is None
    assert report["p95_render_ms"] == 12
    assert report["p95_event_to_render_ms"] == 80


def test_won_sale_does_not_invent_a_held_meeting():
    from app.optimization.learning import derive_labels

    assert derive_labels("sale_won")["sale_won"]
    assert not derive_labels("sale_won")["meeting_held"]


def test_calibration_and_subgroup_reports_include_sample_and_seen_group_boundaries():
    from app.ml.training import calibration_diagnostics

    rows = [
        {"human": i % 2, "features": {"segment": "s" + str(i % 2)}, "rep_id": "r", "number_id": "n"}
        for i in range(20)
    ]
    result = calibration_diagnostics(rows, [0.4] * 20, train=rows[:5])
    assert sum(bin["samples"] for bin in result["bins"]) == 20
    assert result["subgroups"]["rep_id"]["r"]["seen_during_training"]
    assert result["subgroups"]["number_id"]["n"]["samples"] == 20
    assert result["expected_calibration_error"] >= 0
