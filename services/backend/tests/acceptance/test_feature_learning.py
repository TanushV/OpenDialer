"""Metrics, work time, statistical estimates, comparisons, challenger training and conversation learning over HTTP."""

from collections import Counter
from datetime import timedelta
from uuid import uuid4

from journey import event, get, post, ready, run_jobs, wrap_up

METRIC_KEYS = (
    "attempts",
    "human_pickups",
    "human_classified_attempts",
    "right_party_successes",
    "right_party_unique",
    "held_meetings",
    "unique_conversions",
    "original_cohort",
    "eligible_now",
)


def settled(ws, path, **params):
    for _ in range(60):
        body = get(ws, path, params=params)
        if body["projection"]["status"] == "ready":
            return body
        run_jobs(ws)
    raise AssertionError(body["projection"])


def metrics(ws):
    return settled(ws, "/v1/metrics")


def lead_rows(count, prefix="L", segment=None):
    return [
        {
            "name": f"{prefix}{i}",
            "phone": f"+1{'415' if i % 2 else '212'}555{i:04d}",
            "company": f"{prefix} Co {i}",
            "timezone": "America/Los_Angeles" if i % 2 else "America/New_York",
            "region": "415" if i % 2 else "212",
            **({"segment": segment(i)} if segment else {}),
        }
        for i in range(count)
    ]


def dial(ws, clock, key, human, outcome, *, talk=30, gap=120):
    packet = post(ws, "/v1/queue/next", {"campaign_id": ws["campaign_id"]})
    call = post(ws, "/v1/calls/start", {"reservation_id": packet["reservation_id"], "idempotency_key": key})
    if human:
        event(ws, call["id"], "call.answered")
        event(ws, call["id"], "human.confirmed")
    clock.now += timedelta(seconds=talk)
    event(ws, call["id"], "call.hangup")
    wrap_up(ws, call["id"], outcome=outcome, contact_role="right_party" if human else "unknown")
    clock.now += timedelta(seconds=gap)
    return {"call": call, "lead": packet["lead"], "number_id": packet["number"]["id"], "human": human}


def ten_calls(ws, clock):
    post(ws, "/v1/leads/import", {"campaign_id": ws["campaign_id"], "rows": lead_rows(12)})
    ready(ws)
    plan = [(True, "right_party"), (False, "no_answer"), (True, "not_interested"), (False, "busy")]
    return [dial(ws, clock, f"m{i}", *plan[i % 4]) for i in range(10)]


def test_reports_count_exactly_the_calls_that_happened(workspace, clock):
    calls = ten_calls(workspace, clock)
    humans = sum(c["human"] for c in calls)
    totals = metrics(workspace)
    assert {k: totals[k] for k in METRIC_KEYS} == {
        "attempts": 10,
        "human_pickups": humans,
        "human_classified_attempts": 10,
        "right_party_successes": humans,
        "right_party_unique": humans,
        "held_meetings": 0,
        "unique_conversions": 0,
        "original_cohort": 15,
        "eligible_now": 15,
    }
    assert totals["unique_reach_per_original_lead"] == humans / 15
    assert totals["pending_outcome_windows"] == 10 and totals["unknown_cost_calls"] == 10
    assert totals["health"]["connect_rate"]["samples"] == 10
    assert totals["health"]["connect_rate"]["rate"] == humans / 10
    home = get(workspace, "/v1/workspace")["metrics"]
    assert {k: home[k] for k in METRIC_KEYS} == {k: totals[k] for k in METRIC_KEYS}

    report = settled(workspace, "/v1/optimization")
    assert report["metrics"]["attempts"] == 10 and report["dataset"]["rows"] == 10
    assert report["dataset"]["pending_rows"] == 10 and report["dataset"]["matured_rows"] == 0
    used = Counter(c["number_id"] for c in calls)
    answered = Counter(c["number_id"] for c in calls if c["human"])
    for number in report["numbers"]:
        health = number["health"]
        assert number["used_today"] == used[number["id"]]
        assert health["samples"] == health["human_classified_samples"] == used[number["id"]]
        assert health["human_answers"] == health["right_party_answers"] == answered[number["id"]]
        assert health["pickup_rate"] == answered[number["id"]] / used[number["id"]]
    assert sorted(d["lead_id"] for d in report["decisions"]) == sorted(c["lead"]["id"] for c in calls)
    assert all(0 < d["propensity"] <= 1 for d in report["decisions"])
    assert report["drift"]["drift_detected"] is False and report["trial"]["sufficient_evidence"] is False

    target = calls[0]
    body = {
        "event_id": "crm-meeting-1",
        "kind": "meeting_held",
        "call_id": target["call"]["id"],
        "occurred_at": clock.now.isoformat(),
        "source_reference": "Calendar confirmation",
    }
    recorded = post(workspace, f"/v1/leads/{target['lead']['id']}/conversions", body)
    totals = metrics(workspace)
    assert totals["held_meetings"] == totals["unique_conversions"] == 1
    assert totals["conversion_per_original_eligible_lead"] == 1 / 15
    post(
        workspace,
        f"/v1/leads/{target['lead']['id']}/conversions/{recorded['id']}/retract",
        {"reason": "Entered against the wrong lead"},
    )
    assert metrics(workspace)["unique_conversions"] == 0


def test_work_time_follows_presence_and_corrections(workspace, clock):
    started = clock.now
    ready(workspace)
    for _ in range(10):
        clock.now += timedelta(seconds=60)
        post(workspace, "/v1/rep/heartbeat", {"activity": "ready"})
    post(workspace, "/v1/rep/presence", {"status": "paused"})
    clock.now += timedelta(minutes=5)
    interval = get(workspace, "/v1/rep/work")["intervals"][0]
    assert interval["started_at"] == started.isoformat()
    assert interval["ended_at"] == (started + timedelta(minutes=10)).isoformat()
    assert metrics(workspace)["rep_work_seconds"] == 600
    edit = {
        "expected_version": interval["version"],
        "started_at": started.isoformat(),
        "ended_at": (started + timedelta(minutes=4)).isoformat(),
        "reason": "Took an unlogged break",
    }
    path = f"/v1/rep/work/{interval['id']}"
    corrected = workspace["client"].patch(path, headers=workspace["headers"], json=edit)
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["correction_reason"] == "Took an unlogged break"
    stale = workspace["client"].patch(path, headers=workspace["headers"], json=edit)
    assert stale.status_code == 409
    future = {
        **edit,
        "expected_version": corrected.json()["version"],
        "ended_at": clock.now.isoformat() + "1",
    }
    assert workspace["client"].patch(path, headers=workspace["headers"], json=future).status_code == 422
    totals = metrics(workspace)
    assert totals["rep_work_seconds"] == 240 and totals["work_time"]["seconds"] == 240
    assert totals["conversions_per_rep_hour"] == 0


def test_goal_change_preview_save_and_reconciliation(workspace, clock):
    calls = ten_calls(workspace, clock)
    lead = next(
        r for r in get(workspace, "/v1/workspace/leads")["items"] if r["id"] == calls[0]["lead"]["id"]
    )
    hold = {
        "request_id": str(uuid4()),
        "expected_version": lead["version"],
        "status": "booked",
        "due_at": (clock.now + timedelta(days=2)).isoformat(),
        "timezone": "America/Los_Angeles",
        "reference": "Demo booked by the rep",
    }
    booked = workspace["client"].post(
        f"/v1/leads/{calls[0]['lead']['id']}/meeting", headers=workspace["headers"], json=hold
    )
    assert booked.status_code == 200, booked.text
    report = settled(workspace, "/v1/optimization")
    assert report["settings"]["conversion_goal"] == "meeting_held"
    preview = post(workspace, "/v1/admin/optimization/preview", {"conversion_goal": "qualified_conversation"})
    assert preview["from_goal"] == "meeting_held" and preview["to_goal"] == "qualified_conversation"
    assert preview["preserved_holds"] == 1
    stale = {
        "conversion_goal": "qualified_conversation",
        "expected_campaign_version": preview["campaign_version"] - 1,
    }
    response = workspace["client"].put("/v1/admin/optimization", headers=workspace["headers"], json=stale)
    assert response.status_code == 409
    body = {
        "conversion_goal": "qualified_conversation",
        "expected_campaign_version": preview["campaign_version"],
    }
    response = workspace["client"].put("/v1/admin/optimization", headers=workspace["headers"], json=body)
    assert response.status_code == 200, response.text
    report = settled(workspace, "/v1/optimization")
    assert report["settings"]["conversion_goal"] == "qualified_conversation"
    assert report["metrics"]["outcome_goal"] == "qualified_conversation"
    assert metrics(workspace)["outcome_goal"] == "qualified_conversation"
    assert post(workspace, "/v1/admin/optimization/refresh") is not None
    bad = workspace["client"].put(
        "/v1/admin/optimization", headers=workspace["headers"], json={"window_days": 0}
    )
    assert bad.status_code == 422


def test_number_comparison_assigns_arms_and_reports_them(workspace, clock):
    post(workspace, "/v1/leads/import", {"campaign_id": workspace["campaign_id"], "rows": lead_rows(12)})
    study = post(
        workspace,
        "/v1/admin/optimization/comparisons",
        {"family": "number_allocation", "review_reference": "Reviewed number comparison", "duration_days": 7},
    )
    assert study["control"] == "general_first" and study["candidate"] == "regional_first"
    again = workspace["client"].post(
        "/v1/admin/optimization/comparisons",
        headers=workspace["headers"],
        json={"family": "number_allocation", "review_reference": "Second protocol", "duration_days": 7},
    )
    assert again.status_code == 409
    ready(workspace)
    plan = [(True, "right_party"), (False, "no_answer")]
    calls = [dial(workspace, clock, f"c{i}", *plan[i % 2]) for i in range(10)]
    report = settled(workspace, "/v1/optimization")
    comparison = next(c for c in report["comparisons"] if c["id"] == study["id"])
    arms = comparison["arms"]
    assert arms["control"]["leads"] + arms["candidate"]["leads"] == 15 == comparison["pending_leads"]
    assigned = Counter(
        d["allocation"]["comparison_arm"]
        for d in report["decisions"]
        if d["allocation"].get("comparison_id") == study["id"]
    )
    assert sum(assigned.values()) == 10 and set(assigned) <= {"control", "candidate"}
    assert all(assigned[arm] <= arms[arm]["leads"] for arm in assigned)
    assert comparison["sufficient_evidence"] is False
    trial = report["trial"]["arms"]
    assert trial["control"]["leads"] + trial["candidate"]["leads"] == report["metrics"]["original_cohort"]
    assert len(calls) == 10
    stopped = post(
        workspace,
        f"/v1/admin/optimization/comparisons/{study['id']}/stop",
        {"reason": "Ending the protocol early"},
    )
    assert stopped == {"stopped": True}
    replacement = post(
        workspace,
        "/v1/admin/optimization/comparisons",
        {"family": "number_allocation", "review_reference": "Second protocol", "duration_days": 7},
    )
    assert replacement["id"] != study["id"]
    strategies = get(workspace, "/v1/optimization/strategies")["strategies"]
    assert [s["id"] for s in strategies] == ["control"]


def test_conversation_vocabulary_edits_are_versioned(workspace):
    report = get(workspace, "/v1/optimization/conversations")
    assert report["vocabulary"] == {"version": 1, "aliases": []}
    assert report["campaign_id"] == workspace["campaign_id"]
    body = {
        "expected_version": 1,
        "aliases": [{"field": "concept", "source": "spreadsheet", "target": "manual tracking"}],
        "reason": "Merge the rep's wording",
    }
    path = "/v1/admin/optimization/conversation-vocabulary"
    saved = workspace["client"].put(path, headers=workspace["headers"], json=body)
    assert saved.status_code == 200, saved.text
    vocabulary = get(workspace, "/v1/optimization/conversations")["vocabulary"]
    assert vocabulary["version"] == 2
    assert [(a["source"], a["target"]) for a in vocabulary["aliases"]] == [("spreadsheet", "manual tracking")]
    assert workspace["client"].put(path, headers=workspace["headers"], json=body).status_code == 409


def test_challenger_training_backtests_calibration_and_review_gates(workspace, clock):
    for phone in ("+14155550101", "+12125550102", "+15125550103"):
        post(workspace, "/v1/suppressions", {"phone": phone, "reason": "temporary_hold"})
    early = post(workspace, "/v1/admin/optimization/train")
    assert early["status"] == "insufficient_data" and early["required_rows"] == 100
    ready(workspace)
    for k in range(100):
        rows = [
            {
                "name": f"T{k}{segment}",
                "phone": f"+1{'415' if segment == 'alpha' else '212'}55{k:05d}",
                "company": f"Train {k} {segment}",
                "timezone": "America/Los_Angeles" if segment == "alpha" else "America/New_York",
                "region": "415" if segment == "alpha" else "212",
                "segment": segment,
            }
            for segment in (("alpha", "beta") if k % 2 else ("beta", "alpha"))
        ]
        post(workspace, "/v1/leads/import", {"campaign_id": workspace["campaign_id"], "rows": rows})
        for j in range(2):
            packet = post(workspace, "/v1/queue/next", {"campaign_id": workspace["campaign_id"]})
            human = packet["lead"]["phone"].startswith("+1415")
            key = {"reservation_id": packet["reservation_id"], "idempotency_key": f"t{k}-{j}"}
            call = post(workspace, "/v1/calls/start", key)
            if human:
                event(workspace, call["id"], "call.answered")
                event(workspace, call["id"], "human.confirmed")
            event(workspace, call["id"], "call.hangup")
            outcome = "not_interested" if human else "no_answer"
            wrap_up(
                workspace, call["id"], outcome=outcome, contact_role="right_party" if human else "unknown"
            )
            clock.now += timedelta(seconds=20)
    queued = post(workspace, "/v1/admin/optimization/train")
    assert queued["status"] == "queued" and queued["rows"] == 200
    run_jobs(workspace, rounds=2)
    [model] = get(workspace, "/v1/workspace")["models"]
    assert model["source_dataset_id"] == queued["dataset_id"] and model["goal"] == "meeting_held"
    assert model["artifact_format"] == "opendialer.funnel-trees.v1" and len(model["checksum"]) == 64
    assert model["vocabulary"]["segments"] == ["alpha", "beta"]
    assert model["active"] is False and model["manual_approved"] is False
    assert model["online_trial_passed"] is False and model["reviewable"] is True
    assert "evidence_scope" in model and "conversion" in model["pooled_stages"][-1]
    human = model["rolling_evaluations"][0]["stages"]["human"]
    assert human["status"] == "fitted" and human["calibration_min_class"] >= 10
    assert human["support"]["calibration"] == {"positive": 10, "negative": 10}
    assert human["brier"] < 0.05 and human["calibration"]["bins"]
    assert set(human["calibration"]["subgroups"]["segment"]) == {"alpha", "beta"}
    assert model["bootstrap"]["resamples"] == 1000 and len(model["bootstrap"]["interval"]) == 2

    activate = workspace["client"].post(
        f"/v1/admin/models/{model['id']}/activate",
        headers=workspace["headers"],
        json={"review_reference": "early"},
    )
    assert activate.status_code == 409
    trial = post(
        workspace,
        f"/v1/admin/models/{model['id']}/start-trial",
        {"review_reference": "Reviewed challenger trial", "max_leads": 20, "duration_days": 1},
    )
    assert trial["id"] == model["id"]
    report = post(workspace, "/v1/admin/optimization/trial")
    assert report["kind"] == "conversion_trial" and report["simulation"] is True
    assert report["eligible_for_activation"] is False
    review = workspace["client"].post(
        f"/v1/admin/models/{model['id']}/trial",
        headers=workspace["headers"],
        json={"report_reference": "Simulated trial", "cohort_report_id": report["id"]},
    )
    assert review.status_code == 409
    stopped = post(workspace, f"/v1/admin/models/{model['id']}/stop-trial", {"reason": "Simulation only"})
    assert stopped["id"] == model["id"]
    rollback = workspace["client"].post(
        f"/v1/admin/models/{model['id']}/rollback",
        headers=workspace["headers"],
        json={"reason": "No active model"},
    )
    assert rollback.status_code in {404, 409}
    drift = settled(workspace, "/v1/optimization")["drift"]
    assert drift["policy"]["minimum_support"] == 20 and drift["drift_detected"] is False
