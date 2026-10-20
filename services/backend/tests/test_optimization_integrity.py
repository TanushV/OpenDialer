import asyncio
from datetime import timedelta

import pytest
from app.coaching.context import merge_semantic_state
from app.coaching.engine import CardRejected
from app.coaching.replay import evaluate
from conftest import completed_report, disposition_body, start


def test_trial_denominator_includes_original_eligible_leads_before_any_are_called(workspace):
    report = completed_report(workspace).json()
    assert (
        sum(arm["leads"] for arm in report["trial"]["arms"].values())
        == report["metrics"]["original_eligible_leads"]
    )
    assert report["metrics"]["attempts"] == 0


def test_a_conversion_finishes_the_current_campaign_without_deleting_history(workspace, clock):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json=disposition_body(c, h, call["id"], **{"outcome": "right_party"}),
    )
    result = c.post(
        f"/v1/leads/{call['lead_id']}/conversions",
        headers=h,
        json={
            "event_id": "finish-goal",
            "kind": "meeting_held",
            "call_id": call["id"],
            "occurred_at": clock.now.isoformat(),
            "source_reference": "Confirmed held meeting",
        },
    )
    assert result.status_code == 200
    lead = next(
        lead_item
        for lead_item in c.get("/v1/workspace", headers=h).json()["leads"]
        if lead_item["id"] == call["lead_id"]
    )
    assert lead["status"] == "converted" and lead["conversion_goal_achieved"] == "meeting_held"
    assert c.get(f"/v1/calls/{call['id']}/analysis", headers=h).status_code == 200


def test_conversion_correction_is_audited_and_never_removes_an_optout(workspace, clock):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json=disposition_body(c, h, call["id"], **{"outcome": "right_party"}),
    )
    event = c.post(
        f"/v1/leads/{call['lead_id']}/conversions",
        headers=h,
        json={
            "event_id": "correct-meeting",
            "kind": "meeting_held",
            "call_id": call["id"],
            "occurred_at": clock.now.isoformat(),
            "source_reference": "Correction example",
        },
    ).json()
    phone = next(
        lead_item["phone"]
        for lead_item in c.get("/v1/workspace", headers=h).json()["leads"]
        if lead_item["id"] == call["lead_id"]
    )
    c.post("/v1/suppressions", headers=h, json={"phone": phone, "reason": "opt_out"})
    correction = c.post(
        f"/v1/leads/{call['lead_id']}/conversions/{event['id']}/retract",
        headers=h,
        json={"reason": "Meeting was cancelled before it occurred"},
    )
    assert correction.status_code == 200, correction.text
    report = completed_report(workspace, headers=h).json()
    assert report["metrics"]["unique_conversions"] == 0
    lead = next(
        lead_item
        for lead_item in c.get("/v1/workspace", headers=h).json()["leads"]
        if lead_item["id"] == call["lead_id"]
    )
    assert lead["suppressed"] is True


def test_candidate_trial_review_requires_a_saved_report_not_arbitrary_boolean_flags(workspace, app):
    from app.db import put

    with app.state.db.transaction() as s:
        model = put(
            s,
            "model_versions",
            workspace["tenant_id"],
            workspace["seller_id"],
            {
                "active": False,
                "source_dataset_id": "owned-dataset",
                "goal": "meeting_held",
                "metrics": {"brier": 0.1, "pooled_baseline_brier": 0.2},
            },
            now=app.state.service.clock(),
        )
        model_id = model.id
    result = workspace["client"].post(
        f"/v1/admin/models/{model_id}/trial",
        headers=workspace["headers"],
        json={
            "report_reference": "No saved cohort report",
            "online_trial_passed": True,
            "coverage_delta": 0,
            "policy_violations": 0,
        },
    )
    assert result.status_code == 409


def test_semantic_state_requires_actual_quotes_not_plausible_source_ids():
    state = {"stage": "discovery"}
    update = {
        "stage": "next_step",
        "needs": [
            {
                "text": "Their budget is $10000",
                "quote": "Their budget is $10000",
                "evidence_segment_ids": ["s"],
                "status": "confirmed",
            }
        ],
    }
    with pytest.raises(CardRejected):
        merge_semantic_state(
            state,
            update,
            [{"segment_id": "s", "text": "No budget has been approved.", "is_final": True, "side": "remote"}],
        )


async def test_superseded_model_work_returns_no_action_instead_of_breaking_the_transcript_request(
    workspace, app, monkeypatch
):
    call = start(workspace)
    service = app.state.service
    app.state.settings.simulation = False
    p = {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}
    started = asyncio.Event()

    class Provider:
        last_usage, last_request_id, model = {}, None, "fixture"

        async def suggest(self, request, deadline_ms=1200):
            if request["state_version"] == 1:
                started.set()
                await asyncio.sleep(10)
            card = request["approved_cards"][0]
            return {
                "action": "SHOW_CARD",
                "kind": "DISCOVERY",
                "say_or_ask": card["text"],
                "reason": "Approved fixture",
                "state_version": request["state_version"],
                "evidence_segment_ids": [request["recent_dialogue"][-1]["segment_id"]],
                "playbook_card_ids": [card["id"]],
                "expires_in_ms": 7000,
            }

    monkeypatch.setattr(service, "configured_coach", lambda p: (Provider(), "openrouter"))
    await service.transcript(
        p,
        call["id"],
        {
            "segment_id": "one",
            "revision": 1,
            "side": "remote",
            "text": "We cannot afford another subscription.",
            "is_final": True,
        },
        defer_coach=True,
    )
    first = asyncio.create_task(service.coach_turn(p, call["id"], urgent=True))
    await asyncio.wait_for(started.wait(), 1)
    await service.transcript(
        p,
        call["id"],
        {
            "segment_id": "two",
            "revision": 1,
            "side": "remote",
            "text": "Can we arrange a meeting?",
            "is_final": True,
        },
        defer_coach=True,
    )
    second = await service.coach_turn(p, call["id"], urgent=True)
    assert second["state_version"] == 2
    assert (await first)["action"] == "NO_ACTION"


async def test_replay_evaluates_expected_card_and_grounding_not_just_show_card_action():
    case = {
        "id": "budget",
        "state_version": 1,
        "expected_action": "SHOW_CARD",
        "expected_card_ids": ["budget"],
        "recent_dialogue": [
            {"segment_id": "s", "text": "We cannot afford that.", "is_final": True, "side": "remote"}
        ],
        "approved_cards": [
            {"id": "budget", "text": "What outcome would make the investment worthwhile?"},
            {"id": "unrelated", "text": "Who maintains your existing equipment?"},
        ],
    }

    class Provider:
        seller_approved = True  # This fixture selects unchanged approved text.

        async def suggest(self, request, deadline_ms):
            return {
                "action": "SHOW_CARD",
                "kind": "DISCOVERY",
                "say_or_ask": "Who maintains your existing equipment?",
                "reason": "Unrelated",
                "state_version": 1,
                "evidence_segment_ids": ["s"],
                "playbook_card_ids": ["unrelated"],
                "expires_in_ms": 7000,
            }

    report = await evaluate([case], Provider(), model="fixture")
    assert report["expected_card_fraction"] == 0
    assert report["expected_action_fraction"] == 1


def test_no_operational_model_trial_can_start_without_a_verified_owned_artifact(workspace, app):
    from app.db import put

    with app.state.db.transaction() as s:
        model = put(
            s,
            "model_versions",
            workspace["tenant_id"],
            workspace["seller_id"],
            {
                "active": False,
                "source_dataset_id": "dataset",
                "goal": "meeting_held",
                "artifact": "/untrusted/model.joblib",
                "checksum": "untrusted",
                "metrics": {"brier": 0.1, "pooled_baseline_brier": 0.2},
            },
            now=app.state.service.clock(),
        )
        model_id = model.id
    result = workspace["client"].post(
        f"/v1/admin/models/{model_id}/start-trial",
        headers=workspace["headers"],
        json={"review_reference": "Reviewed controlled pilot", "max_leads": 100, "duration_days": 7},
    )
    assert result.status_code == 409


def test_partial_coach_feedback_does_not_manufacture_unreviewed_quality_dimensions(workspace, clock):
    call = start(workspace)
    c, h = workspace["client"], workspace["headers"]
    c.post(
        f"/v1/calls/{call['id']}/transcript",
        headers=h,
        json={
            "segment_id": "quality",
            "revision": 1,
            "side": "remote",
            "text": "We use another provider.",
            "is_final": True,
        },
    )
    clock.now += timedelta(milliseconds=301)
    assert c.post("/v1/admin/jobs/run", headers=h).status_code == 200
    card = c.get(f"/v1/calls/{call['id']}", headers=h).json()["coach"]
    result = c.post(
        f"/v1/calls/{call['id']}/coach/feedback",
        headers=h,
        json={"coach_event_id": card["id"], "useful": True},
    )
    assert result.status_code == 200
    quality = completed_report(workspace, headers=h).json()["coaching"]
    assert quality["useful_fraction"] == 1 and quality["grounded_fraction"] is None


async def test_disconnected_live_stream_does_not_raise_a_secondary_close_exception(workspace, app):
    from starlette.websockets import WebSocketDisconnect

    start(workspace)

    class Disconnected:
        headers = {}

        async def accept(self):
            pass

        async def receive_json(self):
            return {"token": workspace["access_token"], "after": 0}

        async def send_json(self, value):
            raise WebSocketDisconnect(1006)

        async def close(self, code):
            raise WebSocketDisconnect(1006)

    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/v1/live")
    await endpoint(Disconnected())


def test_control_and_candidate_get_explicit_capacity_and_callbacks_remain_first(clock):
    from app.optimization.learning import choose_cohort_candidate

    rows = [
        {"id": "control", "arm": "control", "p_human": 0.2, "score": 0.001},
        {"id": "candidate", "arm": "candidate", "p_human": 0.9, "score": 0.1},
    ]
    # Outside a trial the production holdout (10%) gives the logged probability; a trial
    # passes its declared allocation (default 50/50).
    low = choose_cohort_candidate(rows, now=clock.now, draw=0.01)
    high = choose_cohort_candidate(rows, now=clock.now, draw=0.5)
    assert low["lead_id"] == "control" and low["lead_selection_probability"] == 0.1
    assert high["lead_id"] == "candidate" and high["lead_selection_probability"] == 0.9
    trial = choose_cohort_candidate(rows, now=clock.now, draw=0.4, control_fraction=0.5)
    assert trial["lead_id"] == "control" and trial["lead_selection_probability"] == 0.5
    single = choose_cohort_candidate(rows[1:], now=clock.now, draw=0.01)
    assert single["lead_selection_probability"] == 1
    rows[1]["callback_due_at"] = (clock.now - timedelta(minutes=1)).isoformat()
    urgent = choose_cohort_candidate(rows, now=clock.now, draw=0.01)
    assert urgent["lead_id"] == "candidate" and urgent["lead_selection_probability"] == 1


@pytest.mark.parametrize(
    "reviewable,changed_baseline", [(None, False), (False, False), (True, False), (True, True)]
)
def test_reviewed_owned_candidate_can_run_a_bounded_pilot_and_expire_without_full_activation(
    workspace, app, clock, reviewable, changed_baseline
):
    import hashlib

    import joblib
    from app.db import put, update
    from app.ml.training import FEATURE_ORDER
    from app.scheduling.models import EstimatePolicy
    from sklearn.dummy import DummyClassifier

    root = app.state.settings.data_dir / "models" / "fixture"
    root.mkdir(parents=True)
    artifact = root / "candidate.joblib"
    estimator = DummyClassifier(strategy="prior").fit([[0] * len(FEATURE_ORDER)] * 4, [0, 1, 0, 1])
    joblib.dump(
        {"human": estimator}
        if reviewable is None
        else {
            "feature_schema_version": 2,
            "stages": {
                "human": {
                    "estimator": estimator,
                    "feature_order": ["local_hour"],
                    "vocabulary": {"segments": [], "concepts": []},
                }
            },
        },
        artifact,
    )
    baseline = {"estimate_policy": EstimatePolicy().model_dump(mode="json"), "handling_seconds": 120}
    with app.state.db.transaction() as s:
        dataset = put(
            s,
            "learning_datasets",
            workspace["tenant_id"],
            workspace["seller_id"],
            {
                "goal": "meeting_held",
                "rows": [],
                "as_of": clock.now.isoformat(),
                "baseline": baseline,
                "window_days": 14,
            },
            now=clock.now,
        )
        model = put(
            s,
            "model_versions",
            workspace["tenant_id"],
            workspace["seller_id"],
            {
                "active": False,
                "source_dataset_id": dataset.id,
                "goal": "meeting_held",
                "artifact": str(artifact),
                "checksum": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "feature_order": FEATURE_ORDER,
                **({"feature_schema_version": 2, "reviewable": reviewable} if reviewable is not None else {}),
                "baseline": baseline,
                "window_days": 14,
                "metrics": {"brier": 0.1, "pooled_baseline_brier": 0.2},
            },
            now=clock.now,
        )
        model_id = model.id
        if changed_baseline:
            p = {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}
            campaign = app.state.service.required(s, "campaigns", workspace["campaign_id"], p)
            update(campaign, optimization={**campaign.data.get("optimization", {}), "handling_seconds": 300})
    c, h = workspace["client"], workspace["headers"]
    response = c.post(
        f"/v1/admin/models/{model_id}/start-trial",
        headers=h,
        json={"review_reference": "Explicit synthetic pilot review", "max_leads": 100, "duration_days": 1},
    )
    if reviewable is False or changed_baseline:
        assert response.status_code == 409, response.text
        return
    assert response.status_code == 200, response.text
    assert response.json()["trial_active"] and response.json()["active"] is False
    report = completed_report(workspace, headers=h).json()
    assert report["trial"]["trial_model_id"] == model_id and not report["trial"]["sufficient_evidence"]
    p = {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}
    with app.state.db.transaction(False) as s:
        assert (
            app.state.service.serving_model(s, p, app.state.service.optimization_config(s, p)).id == model_id
        )
    clock.now += timedelta(days=2)
    with app.state.db.transaction(False) as s:
        assert app.state.service.serving_model(s, p, app.state.service.optimization_config(s, p)) is None
