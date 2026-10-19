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
        assert "account_rate_override" in rate and rate.get("minimum_funding_usd") is not None, name
