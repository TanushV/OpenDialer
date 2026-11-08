"""Metrics, work time, statistical estimates, comparisons, challenger training and conversation learning over HTTP."""

from collections import Counter
from datetime import timedelta
from uuid import uuid4

import pytest
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
    # Home metrics are gone (R2.2); the optimization report carries the same counted totals.
    return settled(ws, "/v1/optimization")["metrics"]


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


@pytest.mark.slow
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


@pytest.mark.slow
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
    # R8.1: the default goal is a booked meeting, known at wrap-up.
    assert report["settings"]["conversion_goal"] == "meeting_booked"
    preview = post(workspace, "/v1/admin/optimization/preview", {"conversion_goal": "qualified_conversation"})
    assert preview["from_goal"] == "meeting_booked" and preview["to_goal"] == "qualified_conversation"
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
