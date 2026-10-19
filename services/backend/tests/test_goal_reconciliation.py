"""Goal changes update future eligibility without silently redialing old wins."""

from datetime import timedelta

import pytest
from app.db import doc, put, update
from test_remediation_rep_workflow import p


@pytest.mark.parametrize("status", ["no_show", "cancelled"])
def test_rep_meeting_hold_lifecycle(workspace, clock, status):
    """C012: ending a meeting hold requires the rep to choose the next action."""
    c, h = workspace["client"], workspace["headers"]
    lead = c.get("/v1/workspace", headers=h).json()["leads"][0]
    booked = c.post(
        f"/v1/leads/{lead['id']}/meeting",
        headers=h,
        json={
            "expected_version": lead["version"],
            "status": "booked",
            "due_at": (clock.now + timedelta(days=1)).isoformat(),
            "timezone": "America/Los_Angeles",
        },
    )
    assert booked.status_code == 200, booked.text
    current = c.get(f"/v1/leads/{lead['id']}/packet", headers=h).json()["lead"]
    result = c.post(
        f"/v1/leads/{lead['id']}/meeting",
        headers=h,
        json={"expected_version": current["version"], "status": status},
    )
    assert result.status_code == 422
    after = c.get(f"/v1/leads/{lead['id']}/packet", headers=h).json()["lead"]
    assert after["outreach_hold"] == current["outreach_hold"]


@pytest.fixture
def goals(workspace, app, clock):
    service, principal = app.state.service, p(workspace)
    with service.db.transaction() as s:
        leads = service.scoped(s, "leads", principal)
        for lead in leads:
            update(lead, status="converted", conversion_goal_achieved="meeting_held")
            put(
                s,
                "conversion_events",
                principal["tenant_id"],
                principal["seller_id"],
                {
                    "lead_id": lead.id,
                    "kind": "meeting_held",
                    "occurred_at": clock.now.isoformat(),
                    "recorded_at": clock.now.isoformat(),
                    "retracted": False,
                },
                now=clock.now,
            )
        update(leads[1], outreach_hold="meeting_booked")
        update(
            leads[2],
            callback_due_at=(clock.now + timedelta(hours=1)).isoformat(),
            callback_owner_id=principal["user_id"],
        )
        ids = [lead.id for lead in leads]
    return ids


def preview(workspace, **values):
    return workspace["client"].post(
        "/v1/admin/optimization/preview",
        headers=workspace["headers"],
        json={"conversion_goal": "sale_won", **values},
    )


def get_lead(workspace, lead_id):
    return next(
        x
        for x in workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["leads"]
        if x["id"] == lead_id
    )


def test_preview_is_read_only_and_explains_preserved_next_actions(goals, workspace, app):
    service = app.state.service
    with service.db.transaction(False) as s:
        before = [doc(x) for x in service.scoped(s, "leads", p(workspace))]
    result = preview(workspace)
    assert result.status_code == 200, result.text
    value = result.json()
    assert value["from_goal"] == "meeting_held" and value["to_goal"] == "sale_won"
    assert value["completed_before"] == 3 and value["completed_after"] == 0
    assert value["requires_next_action"] == 1 and value["preserved_holds"] == 1
    assert value["preserved_callbacks"] == 1
    with service.db.transaction(False) as s:
        assert [doc(x) for x in service.scoped(s, "leads", p(workspace))] == before


def test_apply_reconciles_completion_and_preserves_callback_and_booking(goals, workspace):
    response = workspace["client"].put(
        "/v1/admin/optimization", headers=workspace["headers"], json={"conversion_goal": "sale_won"}
    )
    assert response.status_code == 200, response.text
    leads = [get_lead(workspace, lead_id) for lead_id in goals]
    assert all(lead["status"] == "eligible" and lead["conversion_goal_achieved"] is None for lead in leads)
    assert leads[0]["outreach_hold"] == "goal_changed"
    assert leads[1]["outreach_hold"] == "meeting_booked"
    assert leads[2]["callback_due_at"] and not leads[2].get("outreach_hold")


def test_preview_version_prevents_stale_goal_application(goals, workspace):
    original = preview(workspace)
    assert original.status_code == 200
    workspace["client"].put("/v1/admin/optimization", headers=workspace["headers"], json={"window_days": 30})
    result = workspace["client"].put(
        "/v1/admin/optimization",
        headers=workspace["headers"],
        json={
            "conversion_goal": "sale_won",
            "expected_campaign_version": original.json()["campaign_version"],
        },
    )
    assert result.status_code == 409, result.text


def test_goal_change_never_removes_optout_or_claims_unobserved_success(goals, workspace):
    lead = get_lead(workspace, goals[0])
    c, h = workspace["client"], workspace["headers"]
    c.post("/v1/suppressions", headers=h, json={"phone": lead["phone"], "reason": "opt_out"})
    c.put("/v1/admin/optimization", headers=h, json={"conversion_goal": "sale_won"})
    after = get_lead(workspace, goals[0])
    assert after["suppressed"] is True and after["status"] != "eligible"
    assert after["conversion_goal_achieved"] is None


def test_explicit_next_action_clears_only_the_goal_review_hold(goals, workspace):
    c, h = workspace["client"], workspace["headers"]
    c.put("/v1/admin/optimization", headers=h, json={"conversion_goal": "sale_won"})
    lead = get_lead(workspace, goals[0])
    result = c.patch(
        f"/v1/leads/{lead['id']}/outreach",
        headers=h,
        json={
            "expected_version": lead["version"],
            "next_action": "resume",
            "reason": "Rep reviewed next step",
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["outreach_hold"] is None
    booked = get_lead(workspace, goals[1])
    assert (
        c.patch(
            f"/v1/leads/{booked['id']}/outreach",
            headers=h,
            json={
                "expected_version": booked["version"],
                "next_action": "resume",
                "reason": "Cannot bypass booking",
            },
        ).status_code
        == 409
    )


def test_goal_review_can_become_one_dated_callback(goals, workspace, clock):
    c, h = workspace["client"], workspace["headers"]
    c.put("/v1/admin/optimization", headers=h, json={"conversion_goal": "sale_won"})
    lead = get_lead(workspace, goals[0])
    result = c.patch(
        f"/v1/leads/{lead['id']}/outreach",
        headers=h,
        json={
            "expected_version": lead["version"],
            "next_action": "callback",
            "reason": "Agreed followup",
            "due_at": (clock.now + timedelta(days=2)).isoformat(),
            "timezone": "America/Los_Angeles",
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["callback_due_at"] == (clock.now + timedelta(days=2)).isoformat()
    tasks = c.get("/v1/workspace", headers=h).json()["tasks"]
    assert len([t for t in tasks if t.get("lead_id") == lead["id"]]) == 1


def test_changed_outcome_window_retires_models_from_the_old_definition(workspace, app, clock):
    service = app.state.service
    with service.db.transaction() as s:
        model = put(
            s,
            "model_versions",
            workspace["tenant_id"],
            workspace["seller_id"],
            {
                "goal": "meeting_held",
                "active": True,
                "trial_active": True,
            },
            now=clock.now,
        )
    workspace["client"].put("/v1/admin/optimization", headers=workspace["headers"], json={"window_days": 30})
    with service.db.transaction(False) as s:
        value = service.required(s, "model_versions", model.id, p(workspace)).data
        assert value["active"] is False and value["trial_active"] is False


def reviewed_packet(workspace, lead_id):
    return workspace["client"].get(f"/v1/leads/{lead_id}/packet", headers=workspace["headers"]).json()


def test_background_script_preparation_does_not_invalidate_outreach_review(goals, workspace, app):
    c, h = workspace["client"], workspace["headers"]
    c.put("/v1/admin/optimization", headers=h, json={"conversion_goal": "sale_won"})
    packet = reviewed_packet(workspace, goals[0])
    service = app.state.service
    owner = service.claim_preparation(p(workspace), goals[0])
    service.release_preparation(p(workspace), goals[0], owner)
    result = c.patch(
        f"/v1/leads/{goals[0]}/outreach",
        headers=h,
        json={
            "expected_version": packet["lead"]["version"],
            "expected_review_token": packet.get("review_token"),
            "next_action": "resume",
            "reason": "Reviewed next contact",
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["outreach_hold"] is None


@pytest.mark.parametrize("change", ["relationship", "campaign"])
def test_meaningful_change_still_invalidates_review(goals, workspace, app, change):
    c, h = workspace["client"], workspace["headers"]
    c.put("/v1/admin/optimization", headers=h, json={"conversion_goal": "sale_won"})
    packet = reviewed_packet(workspace, goals[0])
    assert packet.get("review_token")
    if change == "campaign":
        c.put("/v1/admin/optimization", headers=h, json={"window_days": 30})
    else:
        with app.state.service.db.transaction() as s:
            lead = app.state.service.required(s, "leads", goals[0], p(workspace))
            update(lead, last_outcome="refused")
    result = c.patch(
        f"/v1/leads/{goals[0]}/outreach",
        headers=h,
        json={
            "expected_version": packet["lead"]["version"],
            "expected_review_token": packet["review_token"],
            "next_action": "resume",
            "reason": "Stale review must not pass",
        },
    )
    assert result.status_code == 409, result.text


def test_background_preparation_does_not_invalidate_meeting_report(goals, workspace, app):
    packet = reviewed_packet(workspace, goals[0])
    service = app.state.service
    owner = service.claim_preparation(p(workspace), goals[0])
    service.release_preparation(p(workspace), goals[0], owner)
    result = workspace["client"].post(
        f"/v1/leads/{goals[0]}/meeting",
        headers=workspace["headers"],
        json={
            "expected_version": packet["lead"]["version"],
            "expected_review_token": packet.get("review_token"),
            "status": "booked",
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["outreach_hold"] == "meeting_booked"


def test_window_only_change_does_not_reopen_a_legacy_completed_relationship(workspace, app):
    with app.state.service.db.transaction() as s:
        lead = app.state.service.scoped(s, "leads", p(workspace))[0]
        update(lead, status="converted", conversion_goal_achieved="meeting_held")
        lead_id = lead.id
    result = workspace["client"].put(
        "/v1/admin/optimization", headers=workspace["headers"], json={"window_days": 30}
    )
    assert result.status_code == 200, result.text
    assert get_lead(workspace, lead_id)["status"] == "converted"
