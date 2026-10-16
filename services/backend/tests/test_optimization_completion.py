"""Remaining release gates are written before their implementation."""

from datetime import timedelta

import pytest

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
    c, h = workspace["client"], workspace["headers"]
    # The older card is obsolete after a new state version.
    assert c.get(f"/v1/calls/{call['id']}", headers=h).json()["coach"] is None


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
