"""Existing purchase liabilities can be reconciled without creating another order."""

import pytest
from app.db import doc, put, update
from fastapi import HTTPException
from test_remediation_privacy import principal


def legacy_order(workspace, app, **extra):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        order = put(
            s,
            "command_outbox",
            p["tenant_id"],
            p["seller_id"],
            {
                "kind": "purchase",
                "status": "uncertain",
                "numbers": ["+14155550198", "+14155550199"],
                "committed_usd": 2,
                "approved_budget_usd": 2,
                **extra,
            },
            key="purchase:legacy",
            now=service.clock(),
        )
        return doc(order)


def test_lost_order_without_reference_survives_reload_with_actionable_state(workspace, app, carrier):
    order = legacy_order(workspace, app)
    c, h = workspace["client"], workspace["headers"]
    path = f"/v1/admin/numbers/orders/{order['id']}/reconcile"
    result = c.post(path, headers=h, json={"expected_version": order["version"]})
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "review_required"
    assert result.json()["committed_usd"] == 2
    assert "provider order" in result.json()["next_action"].lower()
    listed = c.get("/v1/admin/numbers/orders", headers=h)
    assert listed.status_code == 200
    assert [x["id"] for x in listed.json()["orders"]] == [order["id"]]
    assert not carrier.purchases


def test_partial_order_reconciles_one_identity_and_preserves_waiting_numbers(
    workspace, app, carrier, monkeypatch
):
    order = legacy_order(workspace, app)
    state, gets = {"complete": False}, []

    async def get_order(identifier):
        gets.append(identifier)
        return {
            "id": identifier,
            "status": "success" if state["complete"] else "pending",
            "phone_numbers": [
                {"phone_number": order["numbers"][0], "status": "success", "requirements_met": True},
                {
                    "phone_number": order["numbers"][1],
                    "status": "success" if state["complete"] else "pending",
                    "requirements_met": state["complete"],
                },
            ],
        }

    monkeypatch.setattr(carrier, "get_number_order", get_order, raising=False)
    c, h = workspace["client"], workspace["headers"]
    path = f"/v1/admin/numbers/orders/{order['id']}/reconcile"
    result = c.post(
        path, headers=h, json={"provider_order_id": "provider-legacy", "expected_version": order["version"]}
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "awaiting_fulfillment"
    assert result.json()["waiting_numbers"] == [order["numbers"][1]]
    assert result.json()["fulfilled_numbers"] == [order["numbers"][0]]
    state["complete"] = True
    again = c.post(path, headers=h, json={"expected_version": result.json()["version"]})
    assert again.status_code == 200, again.text
    assert again.json()["id"] == order["id"] and again.json()["status"] == "completed"
    for _ in range(2):
        again = c.post(path, headers=h, json={"expected_version": again.json()["version"]})
        assert again.status_code == 200
    inventory = c.get("/v1/workspace", headers=h).json()["numbers"]
    for phone in order["numbers"]:
        found = [n for n in inventory if n["phone"] == phone]
        assert len(found) == 1
        assert found[0]["lifecycle"] == "purchased"
        assert found[0]["callback_tested"] is False and found[0]["identity_configured"] is False
        assert not found[0].get("provider_id")  # order item ID is not an inventory ID
    assert len(gets) >= 2 and set(gets) == {"provider-legacy"}
    assert not carrier.purchases


@pytest.mark.parametrize(
    "receipt",
    [
        {"id": "wrong", "status": "success", "phone_numbers": []},
        {
            "id": "provider",
            "status": "success",
            "phone_numbers": [{"phone_number": "+12125550101", "status": "success"}],
        },
        {"id": "provider", "status": "success", "phone_numbers": []},
    ],
)
def test_order_receipt_must_match_the_owned_intent(workspace, app, carrier, monkeypatch, receipt):
    order = legacy_order(workspace, app)

    async def get_order(identifier):
        return receipt

    monkeypatch.setattr(carrier, "get_number_order", get_order, raising=False)
    c, h = workspace["client"], workspace["headers"]
    before = c.get("/v1/workspace", headers=h).json()["numbers"]
    r = c.post(
        f"/v1/admin/numbers/orders/{order['id']}/reconcile",
        headers=h,
        json={"expected_version": order["version"], "provider_order_id": "provider"},
    )
    assert r.status_code == 502, r.text
    assert c.get("/v1/workspace", headers=h).json()["numbers"] == before
    assert not carrier.purchases


def test_order_reconciliation_fences_stale_version_and_other_seller(workspace, app):
    order = legacy_order(workspace, app)
    c, h = workspace["client"], workspace["headers"]
    path = f"/v1/admin/numbers/orders/{order['id']}/reconcile"
    assert c.post(path, headers=h, json={"expected_version": 0}).status_code == 409
    p = {**principal(workspace), "seller_id": "another-seller"}
    with pytest.raises(HTTPException) as missing:
        app.state.service.number_orders(p, intent_id=order["id"])
    assert missing.value.status_code == 404


def test_failed_order_retains_liability_until_billing_reconciles(workspace, app, carrier, monkeypatch):
    order = legacy_order(workspace, app)

    async def get_order(identifier):
        return {
            "id": identifier,
            "status": "failure",
            "phone_numbers": [{"phone_number": phone, "status": "failure"} for phone in order["numbers"]],
        }

    monkeypatch.setattr(carrier, "get_number_order", get_order, raising=False)
    c, h = workspace["client"], workspace["headers"]
    r = c.post(
        f"/v1/admin/numbers/orders/{order['id']}/reconcile",
        headers=h,
        json={"expected_version": order["version"], "provider_order_id": "provider"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed"
    assert r.json()["committed_usd"] == 2 and r.json()["billing_status"] == "unreconciled"


def test_pending_order_capacity_is_waiting_and_not_bought_again(workspace, app):
    legacy_order(
        workspace, app, status="awaiting_fulfillment", waiting_numbers=["+14155550198", "+14155550199"]
    )
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        for number in service.scoped(s, "number_inventory", p):
            update(number, lifecycle="retired_callback_only")
        for lead in service.scoped(s, "leads", p):
            update(lead, region="415")
    r = workspace["client"].post(
        "/v1/admin/numbers/proposal",
        headers=workspace["headers"],
        json={"campaign_id": workspace["campaign_id"]},
    )
    assert r.status_code == 200, r.text
    value = r.json()
    assert value["projection"]["status"] == "pending"
    import asyncio

    asyncio.run(service.run_jobs(p["tenant_id"], ["ml"]))
    value = service.proposal(p, workspace["campaign_id"])
    assert value["projection"]["status"] == "ready"
    assert value["existing_only_capacity"] == 0
    assert sum(x["capacity_waiting_for_review"] for x in value["regions"]) > 0
    assert value["proposed_additional_numbers"] == 0


def test_expired_order_lease_can_be_reconciled_and_unknown_receipt_keeps_same_intent(
    workspace, app, carrier, monkeypatch, clock
):
    from datetime import timedelta

    order = legacy_order(
        workspace,
        app,
        status="reconciling",
        lease_owner="crashed",
        lease_until=(clock.now - timedelta(seconds=1)).isoformat(),
        provider_order_id="provider",
    )

    async def offline(identifier):
        raise TimeoutError("carrier unavailable")

    monkeypatch.setattr(carrier, "get_number_order", offline, raising=False)
    c, h = workspace["client"], workspace["headers"]
    assert (
        c.post(
            f"/v1/admin/numbers/orders/{order['id']}/reconcile",
            headers=h,
            json={"expected_version": order["version"]},
        ).status_code
        == 502
    )
    current = c.get("/v1/admin/numbers/orders", headers=h).json()["orders"]
    assert len(current) == 1 and current[0]["id"] == order["id"]
    assert current[0]["status"] == "uncertain" and current[0]["lease_owner"] is None
    assert current[0]["committed_usd"] == 2
    assert not carrier.purchases
