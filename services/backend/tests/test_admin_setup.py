"""Admin setup, trust boundaries and one source of configuration definitions (W7b)."""

from app.db import Job, put
from sqlalchemy import func, select


def _model(app, workspace, **data):
    with app.state.db.transaction() as s:
        model = put(
            s,
            "model_versions",
            workspace["tenant_id"],
            workspace["seller_id"],
            {"active": False, "goal": "meeting_held", **data},
            now=app.state.service.clock(),
        )
        return model.id


def _report(app, workspace, model_id, **data):
    with app.state.db.transaction() as s:
        report = put(
            s,
            "evaluation_reports",
            workspace["tenant_id"],
            workspace["seller_id"],
            {
                "kind": "conversion_trial",
                "simulation": False,
                "sufficient_evidence": True,
                "goal": "meeting_held",
                "trial_model_id": model_id,
                "coverage_delta": 0.0,
                "policy_violations": 0,
                **data,
            },
            now=app.state.service.clock(),
        )
        return report.id


def _model_data(app, workspace, model_id):
    from app.db import get

    with app.state.db.transaction(False) as s:
        return dict(get(s, "model_versions", model_id, workspace["tenant_id"]).data)


def test_legacy_trial_review_cannot_assert_its_own_passing_evidence(workspace, app):
    model_id = _model(app, workspace, metrics={"brier": 0.1, "pooled_baseline_brier": 0.2})
    result = workspace["client"].post(
        f"/v1/admin/models/{model_id}/trial",
        headers=workspace["headers"],
        json={
            "report_reference": "Client-asserted legacy review",
            "online_trial_passed": True,
            "coverage_delta": 0,
            "policy_violations": 0,
        },
    )
    assert result.status_code == 409, result.text
    assert not _model_data(app, workspace, model_id).get("online_trial_passed")


def test_trial_review_derives_every_gate_from_the_saved_cohort_report(workspace, app):
    model_id = _model(app, workspace, source_dataset_id="owned-dataset")
    report_id = _report(app, workspace, model_id, coverage_delta=0.25, policy_violations=0)
    result = workspace["client"].post(
        f"/v1/admin/models/{model_id}/trial",
        headers=workspace["headers"],
        # A stale or hostile client contradicts the saved evidence; only the report counts.
        json={
            "report_reference": "Reviewed saved cohort",
            "cohort_report_id": report_id,
            "online_trial_passed": False,
            "coverage_delta": -5,
            "policy_violations": 7,
        },
    )
    assert result.status_code == 200, result.text
    stored = _model_data(app, workspace, model_id)
    assert stored["online_trial_passed"] is True
    assert stored["coverage_delta"] == 0.25
    assert stored["policy_violations"] == 0
    assert stored["cohort_report_id"] == report_id


def test_trial_review_needs_only_the_report_identity_and_reference(workspace, app):
    model_id = _model(app, workspace, source_dataset_id="owned-dataset")
    report_id = _report(app, workspace, model_id, policy_violations=2)
    result = workspace["client"].post(
        f"/v1/admin/models/{model_id}/trial",
        headers=workspace["headers"],
        json={"report_reference": "Reviewed saved cohort", "cohort_report_id": report_id},
    )
    assert result.status_code == 200, result.text
    assert _model_data(app, workspace, model_id)["policy_violations"] == 2


def test_activation_is_the_admin_request_not_a_client_approval_flag(workspace, app, monkeypatch):
    import app.ml.training as training

    service = app.state.service
    monkeypatch.setattr(service, "model_source_current", lambda *args: True)
    monkeypatch.setattr(training, "predict_funnel", lambda *args: {"human": 0.5})
    reviewed = _model(
        app,
        workspace,
        source_dataset_id="owned-dataset",
        feature_schema_version=2,
        reviewable=True,
        online_trial_passed=True,
        coverage_delta=0.0,
        policy_violations=0,
    )
    c, h = workspace["client"], workspace["headers"]
    activated = c.post(f"/v1/admin/models/{reviewed}/activate", headers=h, json={"approved": False})
    assert activated.status_code == 200, activated.text
    assert activated.json()["active"] is True
    unreviewed = _model(
        app,
        workspace,
        source_dataset_id="owned-dataset",
        feature_schema_version=2,
        reviewable=True,
    )
    refused = c.post(
        f"/v1/admin/models/{unreviewed}/activate",
        headers=h,
        json={"approved": True, "online_trial_passed": True, "coverage_delta": 0, "policy_violations": 0},
    )
    assert refused.status_code == 409, refused.text
    assert not _model_data(app, workspace, unreviewed).get("active")


def _current_policy(workspace):
    return workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["policy"]


def test_policy_publish_from_a_stale_version_is_a_conflict(workspace):
    c, h = workspace["client"], workspace["headers"]
    original = _current_policy(workspace)
    first = c.post(
        f"/v1/admin/policies/{original['id']}/publish",
        headers=h,
        json={"max_attempts": 4, "reason": "First editor", "expected_version": original["version"]},
    )
    assert first.status_code == 200, first.text
    stale = c.post(
        f"/v1/admin/policies/{original['id']}/publish",
        headers=h,
        json={"max_attempts": 2, "reason": "Second editor saw the old version"},
    )
    assert stale.status_code == 409, stale.text
    assert "POLICY_VERSION_CONFLICT" in stale.text
    current = _current_policy(workspace)
    assert current["id"] == first.json()["id"] and current["max_attempts"] == 4
    mismatched = c.post(
        f"/v1/admin/policies/{current['id']}/publish",
        headers=h,
        json={"max_attempts": 2, "reason": "Wrong version", "expected_version": current["version"] + 1},
    )
    assert mismatched.status_code == 409, mismatched.text
    reloaded = c.post(
        f"/v1/admin/policies/{current['id']}/publish",
        headers=h,
        json={"max_attempts": 2, "reason": "Reloaded editor", "expected_version": current["version"]},
    )
    assert reloaded.status_code == 200, reloaded.text


def test_policy_publish_has_no_national_registry_toggle(workspace):
    policy = _current_policy(workspace)
    result = workspace["client"].post(
        f"/v1/admin/policies/{policy['id']}/publish",
        headers=workspace["headers"],
        json={"registry_required": True, "reason": "A non-setting cannot be published"},
    )
    assert result.status_code == 422, result.text


def test_settings_defaults_carry_the_bounds_the_ui_renders(workspace):
    c, h = workspace["client"], workspace["headers"]
    defaults = c.get("/v1/admin/settings", headers=h).json()["defaults"]
    bounds = defaults["bounds"]
    assert bounds["operating"]["auto_coach_interval_seconds"] == {
        "type": "integer",
        "min": 3,
        "max": 60,
        "default": 8,
    }
    assert bounds["dnc_refresh"]["interval_hours"]["min"] == 24
    assert bounds["dnc_refresh"]["interval_hours"]["max"] == 168
    assert bounds["preparation"]["ahead_count"] == {"type": "integer", "min": 1, "max": 100, "default": 20}
    assert bounds["optimization"]["window_days"] == {"type": "integer", "min": 1, "max": 180}
    assert bounds["optimization"]["conversion_goal"]["enum"] == [
        "qualified_conversation",
        "meeting_held",
        "sale_won",
    ]
    assert bounds["optimization"]["staffing_weekdays"]["items"] == {"type": "integer", "min": 0, "max": 6}
    assert bounds["health_policy"]["material_drop"]["min_exclusive"] == 0
    assert bounds["policy"]["local_end_hour"] == {"type": "integer", "min": 1, "max": 24}
    assert bounds["policy"]["max_attempts"] == {"type": "integer", "min": 1, "max": 20}
    assert bounds["budget"]["daily_limit_usd"]["max"] == 100000
    assert bounds["callback_routing"]["ring_seconds"] == {"type": "integer", "min": 5, "max": 30}
    assert bounds["model_trial"]["max_leads"] == {"type": "integer", "min": 20, "max": 10000, "default": 200}
    assert bounds["comparison"]["candidate_probability"]["max"] == 0.9
    assert bounds["setup"]["password"]["min_length"] == 12
    assert bounds["screening"]["phones"]["max_items"] == 500
    assert bounds["deferred_batch"]["lead_ids"]["max_items"] == 20
    assert defaults["optimization"]["staffing_timezone"] == "UTC"
    assert defaults["optimization"]["conversion_goal"] == "meeting_held"
    # The same definitions are readable by every role for shared forms.
    assert c.get("/v1/definitions", headers=h).json()["bounds"] == bounds


def test_settings_read_carries_current_campaign_goal_and_schedule(workspace):
    c, h = workspace["client"], workspace["headers"]
    saved = c.put(
        "/v1/admin/optimization",
        headers=h,
        json={"staffing_timezone": "America/Chicago", "staffing_start_hour": 8, "staffing_end_hour": 17},
    )
    assert saved.status_code == 200, saved.text
    current = c.get("/v1/admin/settings", headers=h).json()["optimization"]
    assert current["staffing_timezone"] == "America/Chicago"
    assert (current["staffing_start_hour"], current["staffing_end_hour"]) == (8, 17)
    assert current["conversion_goal"] == "meeting_held" and current["window_days"] == 14


def test_definitions_are_readable_by_a_rep_but_settings_are_not(workspace):
    c, h = workspace["client"], workspace["headers"]
    created = c.post(
        "/v1/admin/users",
        headers=h,
        json={"email": "setup-rep@example.test", "password": "rep-setup-password-1", "role": "rep"},
    )
    assert created.status_code == 200, created.text
    token = c.post(
        "/v1/auth/session", json={"email": "setup-rep@example.test", "password": "rep-setup-password-1"}
    ).json()["access_token"]
    rep = {"Authorization": f"Bearer {token}"}
    definitions = c.get("/v1/definitions", headers=rep)
    assert definitions.status_code == 200, definitions.text
    assert definitions.json()["bounds"]["conversion"]["kind"]["enum"] == [
        "qualified_conversation",
        "meeting_held",
        "sale_won",
    ]
    assert c.get("/v1/admin/settings", headers=rep).status_code == 403
    assert c.get("/v1/admin/numbers/summary", headers=rep).status_code == 403


def test_numbers_summary_counts_the_whole_inventory_without_queueing_work(workspace, app):
    c, h = workspace["client"], workspace["headers"]
    for phone, lifecycle in [
        ("+13105550111", "purchased"),
        ("+13105550112", "identity_configured"),
        ("+13105550113", "limited_real_use"),
        ("+13105550114", "paused_for_outbound"),
        ("+13105550115", "retired_callback_only"),
        ("+13105550116", "investigating"),
    ]:
        added = c.post("/v1/admin/numbers", headers=h, json={"phone": phone, "lifecycle": lifecycle})
        assert added.status_code == 200, added.text
    with app.state.db.transaction(False) as s:
        jobs_before = s.scalar(select(func.count()).select_from(Job))
    summary = c.get("/v1/admin/numbers/summary", headers=h)
    assert summary.status_code == 200, summary.text
    body = summary.json()
    with app.state.db.transaction(False) as s:
        assert s.scalar(select(func.count()).select_from(Job)) == jobs_before
    inventory = body["inventory"]
    assert inventory["total"] == 8
    assert inventory["pending_setup"] == 2
    assert inventory["limited_use"] == 1
    assert inventory["paused_outbound"] == 2
    assert inventory["investigating"] == 1
    assert inventory["lifecycles"]["healthy"] == 2
    page = c.get("/v1/workspace/numbers", headers=h).json()
    assert inventory["ready"] == sum(bool(n.get("outbound_ready")) for n in page["items"])
    assert isinstance(body["stable_assignments"], int)
    assert body["forecast"]["projection"]["status"] in {"pending", "stale", "ready"}
    assert "refresh_status" in body["forecast"]["projection"]


def test_reviewed_variant_approval_is_the_admin_publication(workspace):
    c, h = workspace["client"], workspace["headers"]
    created = c.post(
        "/v1/admin/variants",
        headers=h,
        json={
            "campaign_id": workspace["campaign_id"],
            "name": "Clear purpose",
            "opener": "Hi, this is {rep} from Example Seller. May I share why I called?",
            "review_reference": "Reviewed synthetic language",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["approved"] is True
    assert created.json()["reviewed_by"] == workspace["user_id"]


def test_business_identity_review_records_approval_without_a_client_flag(workspace):
    c, h = workspace["client"], workspace["headers"]
    result = c.post(
        "/v1/admin/seller/approve", headers=h, json={"identity_reference": "Registration 12345 reviewed"}
    )
    assert result.status_code == 200, result.text
    assert result.json()["approved"] is True
    assert c.get("/v1/workspace", headers=h).json()["seller"]["approved"] is True
