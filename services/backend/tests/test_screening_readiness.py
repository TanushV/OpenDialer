"""E05/C018: setup and final submission must agree on authorized prepaid evidence."""

import pytest
from app.compliance.policy import authorize
from app.db import update
from conftest import ready
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
