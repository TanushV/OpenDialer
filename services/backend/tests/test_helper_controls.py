"""Pacing and rep controls must be real behavior, independent of provider availability."""

from datetime import timedelta

import pytest
from app.db import update
from conftest import start
from test_remediation_coach import advice
from test_remediation_privacy import post_turn, principal


def test_pinned_help_survives_display_timeout_but_not_raw_expiry(workspace, clock):
    call = start(workspace)
    post_turn(workspace, call, text="We have no budget for this.")
    c, h = workspace["client"], workspace["headers"]
    clock.now += timedelta(milliseconds=301)
    assert c.post("/v1/admin/jobs/run", headers=h, json={"queues": ["live_coach"]}).status_code == 200
    assert c.patch("/v1/calls/" + call["id"] + "/coach", headers=h, json={"pinned": True}).status_code == 200
    clock.now += timedelta(seconds=35)
    live = c.get("/v1/calls/" + call["id"], headers=h).json()
    assert live["coach"] is not None and live["coach"]["pinned"] is True
    clock.now += timedelta(seconds=266)
    assert c.get("/v1/calls/" + call["id"], headers=h).json()["coach"] is None


@pytest.mark.asyncio
async def test_configured_quiet_interval_limits_paid_work_but_manual_help_bypasses(
    workspace, app, clock, monkeypatch
):
    call = start(workspace)
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        update(
            service.required(s, "sellers", p["seller_id"], p),
            operating_policy={"auto_coach_interval_seconds": 60},
        )
    requests = []

    class Provider:
        last_usage, last_request_id = {}, None

        async def suggest(self, request, **kwargs):
            requests.append(request)
            return advice(request["state_version"], request["recent_dialogue"][-1]["segment_id"])

    monkeypatch.setattr(service, "configured_coach", lambda p: (Provider(), "openrouter"))
    monkeypatch.setattr(service.settings, "simulation", False)
    for i in range(2):
        await service.transcript(
            p,
            call["id"],
            {
                "segment_id": str(i),
                "revision": 1,
                "side": "rep",
                "is_final": True,
                "text": "An unfamiliar constraint needs explaining.",
            },
        )
        clock.now += timedelta(seconds=10)
        await service.run_jobs(p["tenant_id"], queues=["live_coach"])
    assert len(requests) == 1
    await service.coach_turn(p, call["id"], urgent=True, question="What should I clarify?")
    assert len(requests) == 2


def test_private_help_and_preferences_reject_another_reps_call(workspace, app):
    from app.db import put
    from app.security import issue_session

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    with service.db.transaction() as s:
        user = put(
            s,
            "users",
            p["tenant_id"],
            p["seller_id"],
            {"role": "rep", "email": "other@example.test", "disabled": False},
        )
        session = issue_session(s, service.settings, user, service.clock())
    h = {"Authorization": "Bearer " + session["access_token"]}
    c = workspace["client"]
    assert c.post(
        "/v1/calls/" + call["id"] + "/coach", headers=h, json={"question": "private question"}
    ).status_code in {403, 404}
    assert c.patch("/v1/calls/" + call["id"] + "/coach", headers=h, json={"muted": True}).status_code in {
        403,
        404,
    }


def test_correction_unpins_advice_that_depended_on_revised_words(workspace, clock):
    call = start(workspace)
    post_turn(workspace, call, text="We have no budget for this.")
    c, h = workspace["client"], workspace["headers"]
    path = "/v1/calls/" + call["id"]
    c.patch(path + "/coach", headers=h, json={"pinned": True, "muted": True})
    post_turn(workspace, call, revision=2, text="Correction: the obstacle is timing.")
    live = c.get(path, headers=h).json()
    assert live["coach"] is None
    assert not live.get("coach_preferences", {}).get("pinned_event_id")
