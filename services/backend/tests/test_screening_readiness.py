"""E05/C018: setup and final submission must agree on authorized prepaid evidence."""

import pytest
from app.compliance.policy import authorize
from app.db import update
from conftest import ready, save_operating_policy
from test_dnc_recovery import PHONE, screen, screening  # noqa: F401 - shared fixture


@pytest.mark.parametrize("covered", [True, False])
def test_prepaid_preflight_authorization_agree(workspace, screening, covered):  # noqa: F811 - pytest fixture
    service, p, calls, _, config = screening
    ready(workspace)
    if covered:
        assert screen(workspace).status_code == 200
    with service.db.transaction() as s:
        for row in service.scoped(s, "registry_snapshots", p):
            if row.data.get("kind") == "national":
                update(row, published=False)
        lead = next(row for row in service.scoped(s, "leads", p) if row.data["phone"] == PHONE)
        number = service.scoped(s, "number_inventory", p)[0]
        decision = authorize(service.context(s, p, lead, number))
    assert decision.allowed is covered
    before = len(calls)
    for _ in range(2):
        preflight = workspace["client"].get("/v1/admin/preflight", headers=workspace["headers"])
        assert preflight.status_code == 200
        blockers = preflight.json()["blockers"]
        if covered:
            assert "REGISTRY_MISSING" not in blockers, (
                "A snapshot is not required when authorized exact prepaid evidence exists"
            )
        else:
            assert not preflight.json()["ready_to_call"]
        screening_state = preflight.json().get("screening", {})
        assert screening_state.get("mode") == "prepaid"
        assert screening_state.get("cleared_count", 0) == int(covered)
    assert len(calls) == before, "Preflight must never spend prepaid credits"
    if covered:
        ready(workspace)
        result = workspace["client"].post(
            "/v1/queue/next", headers=workspace["headers"], json={"campaign_id": workspace["campaign_id"]}
        )
        assert result.status_code == 200, result.text


@pytest.mark.parametrize("state", ["valid", "pending", "expired", "changed_authorization", "uncovered"])
def test_forecast_uses_current_national_clearance_without_charging(workspace, screening, clock, state):  # noqa: F811
    from datetime import timedelta

    service, p, calls, _, config = screening
    if state != "pending":
        assert screen(workspace).status_code == 200
    if state in {"changed_authorization", "uncovered"}:
        authorization = {**config["dnc_seller_authorization"]}
        authorization.update(
            {"reference": "new-authority"} if state == "changed_authorization" else {"npas": ["212"]}
        )
        service.vault.write(p["tenant_id"], {"dnc_seller_authorization": authorization})
    with service.db.transaction() as s:
        for row in service.scoped(s, "registry_snapshots", p):
            if row.data.get("kind") == "national":
                update(row, published=False)
            elif state == "expired" and row.data.get("kind") == "prepaid":
                update(row, expires_at=clock.now.isoformat())
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p)
        update(
            campaign,
            optimization={
                **campaign.data.get("optimization", {}),
                "max_attempts": 1,
                "staffed_windows": [
                    {
                        "start_at": clock.now.isoformat(),
                        "end_at": (clock.now + timedelta(hours=1)).isoformat(),
                        "rep_ids": [p["user_id"]],
                    }
                ],
            },
        )
    before = len(calls)
    with service.db.transaction(False) as s:
        forecast = service.forecast(s, p, campaign_id=workspace["campaign_id"])
        expected = {
            row.id for row in service.scoped(s, "leads", p) if row.data["phone"] == PHONE and state == "valid"
        }
    assert forecast["imported_leads"] == 3, "Blocked leads remain in the list denominator"
    assert forecast["known_next_attempts"] == len(expected)
    assert {row["lead_id"] for row in forecast["existing_schedule"]} == expected
    assert forecast["screening"]["blocked_count"] == 3 - len(expected)
    assert forecast["screening"]["cleared_count"] == len(expected)
    assert forecast["screening"]["blocked_reasons"], "The capacity shortfall needs its actual remedy"
    assert len(calls) == before, "Planning must never initiate paid screening"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["valid", "expired", "changed_authorization", "uncovered"])
async def test_restored_prepaid_cache_requires_current_authority_before_queue_and_send(
    workspace,
    screening,  # noqa: F811 - imported pytest fixture
    clock,
    tmp_path,
    state,  # noqa: F811
):
    from app.db import get
    from fastapi import HTTPException
    from test_completion_journeys import restore_separate_authority

    service, p, calls, _, config = screening
    assert screen(workspace).status_code == 200
    with service.db.transaction() as session:
        for row in service.scoped(session, "registry_snapshots", p):
            if row.data.get("kind") == "national":
                update(row, published=False)
        for row in service.scoped(session, "leads", p):
            if row.data["phone"] != PHONE:
                update(row, status="needs_review")
    target = restore_separate_authority(service, p, tmp_path / "restored-screening")
    try:
        await target.reconcile_restore(p, "Checked the synthetic original-effect ledger")
        target.presence(
            p,
            {
                "status": "ready",
                "audio_ready": True,
                "telephony_ready": True,
                "agent_call_control_id": "restored-agent",
            },
        )
        if state in {"changed_authorization", "uncovered"}:
            target.vault.write(
                p["tenant_id"],
                {
                    "dnc_seller_authorization": {
                        **config["dnc_seller_authorization"],
                        **(
                            {"reference": "new-authority"}
                            if state == "changed_authorization"
                            else {"npas": ["212"]}
                        ),
                    }
                },
            )
        with target.db.transaction() as session:
            if state == "expired":
                for row in target.scoped(session, "registry_snapshots", p):
                    if row.data.get("kind") == "prepaid":
                        update(row, expires_at=clock.now.isoformat())
            lead = next(row for row in target.scoped(session, "leads", p) if row.data["phone"] == PHONE)
            number = target.scoped(session, "number_inventory", p)[0]
            assert authorize(target.context(session, p, lead, number)).allowed is (state == "valid")
        if state == "valid":
            reservation = target.reserve(p, workspace["campaign_id"])
            # Clearance was valid for selection, then the authorization changed before send.
            target.vault.write(
                p["tenant_id"],
                {"dnc_seller_authorization": {**config["dnc_seller_authorization"], "npas": ["212"]}},
            )
            with pytest.raises(HTTPException):
                await target.start_call(
                    p,
                    {
                        "reservation_id": reservation["reservation_id"],
                        "idempotency_key": "restored-screening-start",
                    },
                )
        else:
            with pytest.raises(HTTPException):
                target.reserve(p, workspace["campaign_id"])
        assert calls == [PHONE], "Recovery and reading cannot spend on another screening request"
        assert target.simulated_carrier.dials == []
        with target.db.transaction(False) as session:
            assert get(session, "sellers", p["seller_id"]).data.get("restore_hold") is False
    finally:
        target.db.engine.dispose()


def test_national_snapshot_uses_the_sellers_shorter_freshness_policy(workspace, app, clock):
    from datetime import timedelta

    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    ready(workspace)
    response = save_operating_policy(
        workspace["client"],
        "/v1/admin/operating-policy",
        headers=workspace["headers"],
        json={"dnc_source_max_age_days": 1},
    )
    assert response.status_code == 200
    with service.db.transaction() as s:
        for row in service.scoped(s, "registry_snapshots", p):
            if row.data.get("kind") == "national":
                update(row, refreshed_at=(clock.now - timedelta(days=2)).isoformat())
        lead = service.scoped(s, "leads", p)[0]
        number = service.scoped(s, "number_inventory", p)[0]
        decision = authorize(service.context(s, p, lead, number))
        assert not decision.allowed and "REGISTRY_STALE" in decision.reasons
    check = workspace["client"].get("/v1/admin/preflight", headers=workspace["headers"]).json()
    assert not check["ready_to_call"] and "REGISTRY_STALE" in check["blockers"]


def test_prepaid_source_change_with_same_reference_invalidates_cached_clearance(workspace, screening, clock):  # noqa: F811
    from datetime import timedelta

    from app.compliance.operating import cached_screen

    service, p, calls, _, config = screening
    assert screen(workspace).status_code == 200
    service.vault.write(
        p["tenant_id"],
        {
            "dnc_seller_authorization": {
                **config["dnc_seller_authorization"],
                "source_refreshed_at": (clock.now - timedelta(hours=1)).isoformat(),
            }
        },
    )
    with service.db.transaction(False) as s:
        assert cached_screen(service, s, p, PHONE) is None
    assert len(calls) == 1


def test_preflight_exposes_pending_coverage_without_spending_on_unseen_leads(workspace, screening):  # noqa: F811
    service, p, calls, _, _ = screening
    with service.db.transaction() as s:
        for row in service.scoped(s, "registry_snapshots", p):
            if row.data.get("kind") == "national":
                update(row, published=False)
    result = workspace["client"].get("/v1/admin/preflight", headers=workspace["headers"]).json()
    assert result["screening"]["configured"] is True
    assert result["screening"]["cleared_count"] == 0
    assert result["screening"]["pending_count"] == 3
    assert not result["ready_to_call"] and not calls


def test_cost_register_has_units_source_date_and_funding_boundaries(workspace):
    result = workspace["client"].get("/v1/admin/operating-policy", headers=workspace["headers"]).json()
    for name, rate in result["costs"].items():
        assert rate.get("currency") == "USD", name
        assert rate.get("unit") and rate.get("source_url") and rate.get("checked_at"), name
        assert rate.get("classification") in {"included", "payg", "existing_recurring", "excluded"}, name
        assert "account_rate_override" in rate and "minimum_funding_usd" in rate, name
        if rate["minimum_funding_usd"] is None:
            assert rate["funding_status"] == "account_dependent", name


def test_unknown_provider_minimum_is_not_published_as_zero(workspace):
    costs = (
        workspace["client"].get("/v1/admin/operating-policy", headers=workspace["headers"]).json()["costs"]
    )
    assert costs["application"]["minimum_funding_usd"] == 0
    assert costs["dnc"]["minimum_funding_usd"] == 100
    assert costs["voice"]["minimum_funding_usd"] is None
    assert costs["cerebras"]["funding_status"] == "account_dependent"


def test_corrupt_national_source_is_an_actionable_block_without_paid_fallback(workspace, app):
    import base64

    from test_remediation_privacy import principal

    p, service = principal(workspace), app.state.service
    with service.db.transaction() as s:
        row = service.scoped(s, "registry_snapshots", p)[0]
        update(row, bitmap=base64.b64encode(b"invalid").decode(), checksum="invalid")
    result = workspace["client"].get("/v1/admin/preflight", headers=workspace["headers"])
    assert result.status_code == 200
    assert not result.json()["ready_to_call"]
    assert "REGISTRY_SOURCE_INVALID" in result.json()["blockers"]


def test_import_preserves_explicit_access_expiry(workspace, app, clock):
    from datetime import timedelta

    from test_remediation_privacy import principal

    p, service = principal(workspace), app.state.service
    expiry = clock.now + timedelta(hours=1)
    result = workspace["client"].post(
        "/v1/admin/registry/import",
        headers=workspace["headers"],
        json={
            "kind": "national",
            "npas": ["415", "212", "512"],
            "subscription_reference": "expires-today",
            "refreshed_at": clock.now.isoformat(),
            "authorization_expires_at": expiry.isoformat(),
            "numbers": [],
        },
    )
    assert result.status_code == 200
    ready(workspace)
    clock.now = expiry
    with service.db.transaction(False) as s:
        lead, number = service.scoped(s, "leads", p)[0], service.scoped(s, "number_inventory", p)[0]
        decision = authorize(service.context(s, p, lead, number))
        assert not decision.allowed and "REGISTRY_STALE" in decision.reasons


@pytest.mark.parametrize("blocked_by", ["state", "reservation", "completed", "capacity"])
def test_preflight_dialable_count_obeys_current_state_policy_and_claims(workspace, app, blocked_by):
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    ready(workspace)
    if blocked_by == "state":
        with service.db.transaction() as s:
            campaign = service.required(s, "campaigns", workspace["campaign_id"], p)
            update(
                service.required(s, "policy_versions", campaign.data["policy_id"], p),
                state_registry_required=True,
            )
    elif blocked_by == "reservation":
        assert (
            workspace["client"]
            .post(
                "/v1/queue/next", headers=workspace["headers"], json={"campaign_id": workspace["campaign_id"]}
            )
            .status_code
            == 200
        )
    elif blocked_by == "completed":
        with service.db.transaction() as s:
            for lead in service.scoped(s, "leads", p):
                update(lead, status="converted")
    else:
        with service.db.transaction() as s:
            for number in service.scoped(s, "number_inventory", p):
                update(number, daily_limit=0)
    result = workspace["client"].get("/v1/admin/preflight", headers=workspace["headers"]).json()
    assert result["screening"]["cleared_count"] == 3
    assert result["screening"]["dialable_queue_count"] == 0
    assert not result["ready_to_call"]


def test_corrupt_required_state_registry_blocks_without_crashing(workspace, app, clock):
    from app.db import put
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    ready(workspace)
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p)
        update(
            service.required(s, "policy_versions", campaign.data["policy_id"], p),
            state_registry_required=True,
        )
        for lead in service.scoped(s, "leads", p):
            update(lead, jurisdiction="CA")
        put(
            s,
            "registry_snapshots",
            p["tenant_id"],
            p["seller_id"],
            {
                "kind": "state",
                "jurisdiction": "CA",
                "npas": ["415", "212", "512"],
                "refreshed_at": clock.now.isoformat(),
                "published": True,
                "bitmap": "broken",
                "checksum": "broken",
                "subscription_id": "state-authority",
            },
            now=clock.now,
        )
    response = workspace["client"].get("/v1/admin/preflight", headers=workspace["headers"])
    assert response.status_code == 200
    assert not response.json()["ready_to_call"]
    assert "STATE_REGISTRY_MISSING" in response.json()["blockers"]


@pytest.mark.parametrize("boundary", ["expired", "access_expired", "future", "malformed", "valid"])
def test_state_source_honors_original_access_and_freshness(workspace, app, clock, boundary):
    from datetime import timedelta

    from app.db import put
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    ready(workspace)
    data = {
        "kind": "state",
        "jurisdiction": "CA",
        "npas": ["415", "212", "512"],
        "refreshed_at": clock.now.isoformat(),
        "published": True,
        "subscription_reference": "state-authority",
        "listed_numbers": [],
    }
    if boundary == "expired":
        data["expires_at"] = clock.now.isoformat()
    elif boundary == "access_expired":
        data["authorization_expires_at"] = clock.now.isoformat()
    elif boundary == "future":
        data["refreshed_at"] = (clock.now + timedelta(seconds=1)).isoformat()
    elif boundary == "malformed":
        data["expires_at"] = "not-a-date"
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p)
        update(
            service.required(s, "policy_versions", campaign.data["policy_id"], p),
            state_registry_required=True,
        )
        for lead in service.scoped(s, "leads", p):
            update(lead, jurisdiction="CA")
        put(s, "registry_snapshots", p["tenant_id"], p["seller_id"], data, now=clock.now)
        lead = service.scoped(s, "leads", p)[0]
        number = service.scoped(s, "number_inventory", p)[0]
        decision = authorize(service.context(s, p, lead, number))
        assert decision.allowed is (boundary == "valid")
        if boundary != "valid":
            assert "STATE_REGISTRY_MISSING" in decision.reasons
    response = workspace["client"].get("/v1/admin/preflight", headers=workspace["headers"])
    assert response.status_code == 200
    # Two fixture leads are inside their current calling windows.
    assert response.json()["screening"]["dialable_queue_count"] == (2 if boundary == "valid" else 0)


@pytest.mark.asyncio
async def test_restored_national_chunks_keep_membership_and_recheck_replaced_source(
    workspace, app, clock, tmp_path
):
    from uuid import uuid4

    from app.compliance.registry import publish_registry, registry_contains
    from fastapi import HTTPException
    from test_completion_journeys import restore_separate_authority
    from test_remediation_privacy import principal

    service, p = app.state.service, principal(workspace)
    blocked, allowed = "+14155550101", "+12125550102"
    body = {
        "request_id": str(uuid4()),
        "kind": "national",
        "npas": ["415", "212"],
        "subscription_reference": "synthetic-current-seller-access",
        "refreshed_at": clock.now.isoformat(),
        "numbers": [blocked],
    }
    published = publish_registry(service, p, body)
    with service.db.transaction() as session:
        for lead in service.scoped(session, "leads", p):
            if lead.data["phone"] != allowed:
                update(lead, status="needs_review")
    target = restore_separate_authority(service, p, tmp_path / "national-recovery")
    try:
        await target.reconcile_restore(p, "Checked original synthetic effects")
        target.presence(
            p,
            {
                "status": "ready",
                "audio_ready": True,
                "telephony_ready": True,
                "agent_call_control_id": "restored-agent",
            },
        )
        with target.db.transaction(False) as session:
            row = target.required(session, "registry_snapshots", published["id"], p)
            assert row.data["chunks"] and registry_contains(session, row, blocked)
            assert not registry_contains(session, row, allowed)
        reservation = target.reserve(p, workspace["campaign_id"])
        assert reservation["lead"]["phone"] == allowed
        publish_registry(target, p, {**body, "request_id": str(uuid4()), "numbers": [blocked, allowed]})
        with pytest.raises(HTTPException) as error:
            await target.start_call(
                p, {"reservation_id": reservation["reservation_id"], "idempotency_key": "restored-national"}
            )
        assert error.value.status_code == 403
        assert target.simulated_carrier.dials == []
    finally:
        target.db.engine.dispose()
