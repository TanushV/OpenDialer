"""E05: policy identities protect explicit funding and concurrent administration."""

from uuid import uuid4

from app.db import update
from test_remediation_privacy import principal

PATH = "/v1/admin/operating-policy"


def test_old_client_cannot_save_unversioned_spending_policy(workspace):
    c, h = workspace["client"], workspace["headers"]
    result = c.put(PATH, headers=h, json={"dnc_source_max_age_days": 2})
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "client_upgrade_required"
    assert c.get(PATH, headers=h).json()["dnc_source_max_age_days"] == 31


def test_policy_replay_preserves_receipt_but_returns_current_policy(workspace, app):
    c, h = workspace["client"], workspace["headers"]
    first = c.get(PATH, headers=h).json()
    body = {"request_id": str(uuid4()), "expected_version": first["version"], "dnc_source_max_age_days": 2}
    saved = c.put(PATH, headers=h, json=body)
    assert saved.status_code == 200, saved.text
    second = c.put(
        PATH,
        headers=h,
        json={
            "request_id": str(uuid4()),
            "expected_version": saved.json()["version"],
            "dnc_source_max_age_days": 3,
        },
    )
    assert second.status_code == 200
    replay = c.put(PATH, headers=h, json=body)
    assert replay.status_code == 200
    assert replay.json()["edit_receipt"] == saved.json()["edit_receipt"]
    assert replay.json()["dnc_source_max_age_days"] == 3
    assert replay.json()["version"] == second.json()["version"]
    assert c.put(PATH, headers=h, json={**body, "dnc_source_max_age_days": 4}).status_code == 409
    assert c.put(PATH, headers=h, json={**body, "request_id": str(uuid4())}).status_code == 409
    p = principal(workspace)
    with app.state.db.transaction() as s:
        seller = app.state.service.required(s, "sellers", p["seller_id"], p)
        update(seller, unrelated_heartbeat="changed")
    assert c.get(PATH, headers=h).json()["version"] == second.json()["version"]


def test_revoked_admin_cannot_replay_policy_edit(workspace, app):
    c, h = workspace["client"], workspace["headers"]
    body = {
        "request_id": str(uuid4()),
        "expected_version": c.get(PATH, headers=h).json()["version"],
        "dnc_source_max_age_days": 2,
    }
    assert c.put(PATH, headers=h, json=body).status_code == 200
    p = principal(workspace)
    with app.state.db.transaction() as s:
        user = app.state.service.required(s, "users", p["user_id"], p)
        update(user, role="rep")
    assert c.put(PATH, headers=h, json=body).status_code == 403
