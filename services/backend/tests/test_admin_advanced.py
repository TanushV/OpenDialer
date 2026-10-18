from conftest import reserve


def test_only_reviewed_variants_can_enter_the_action_set(workspace):
    c, h = workspace["client"], workspace["headers"]
    data = {
        "campaign_id": workspace["campaign_id"],
        "name": "Clear purpose",
        "opener": "Hi, this is {rep} from Example Seller. May I share why I called?",
        "review_reference": "Reviewed synthetic language",
        "approved": True,
    }
    created = c.post("/v1/admin/variants", headers=h, json=data)
    assert created.status_code == 200, created.text
    packet = reserve(workspace)
    assert packet.status_code == 200
    lead_id = packet.json()["lead"]["id"]
    script = c.get(f"/v1/leads/{lead_id}/packet", headers=h).json()["script"]
    assert script["treatment_variant_id"] == created.json()["id"]
    assert "May I share why I called?" in script["opener"]


def test_even_quoted_numbers_cannot_create_new_recurring_commitments(workspace, carrier):
    c, h = workspace["client"], workspace["headers"]
    c.put(
        "/v1/admin/budget",
        headers=h,
        json={"daily_limit_usd": 10, "monthly_limit_usd": 100, "number_purchase_limit_usd": 10},
    )
    quoted = c.post("/v1/admin/numbers/search", headers=h, json={"region": "415"})
    assert quoted.status_code == 200, quoted.text
    quote = quoted.json()["numbers"][0]
    result = c.post(
        "/v1/admin/numbers/purchase",
        headers=h,
        json={
            "phone_numbers": [quote["phone"]],
            "approved_budget_usd": quote["initial_commitment_usd"],
            "idempotency_key": "quoted-order",
        },
    )
    assert result.status_code == 403, result.text
    assert "RECURRING" in result.text
    assert not carrier.purchases
    repeated = c.post(
        "/v1/admin/numbers/purchase",
        headers=h,
        json={
            "phone_numbers": [quote["phone"]],
            "approved_budget_usd": quote["initial_commitment_usd"],
            "idempotency_key": "quoted-order",
        },
    )
    assert repeated.status_code == 403
    assert not carrier.purchases


def test_procurement_rejects_unquoted_and_underfunded_orders(workspace, clock):
    from datetime import timedelta

    c, h = workspace["client"], workspace["headers"]
    c.put(
        "/v1/admin/budget",
        headers=h,
        json={"daily_limit_usd": 10, "monthly_limit_usd": 100, "number_purchase_limit_usd": 10},
    )
    body = {"phone_numbers": ["+14155550188"], "approved_budget_usd": 1, "idempotency_key": "order-unquoted"}
    assert c.post("/v1/admin/numbers/purchase", headers=h, json=body).status_code == 403
    c.post("/v1/admin/numbers/search", headers=h, json={"region": "415"})
    assert (
        c.post("/v1/admin/numbers/purchase", headers=h, json={**body, "approved_budget_usd": 0.5}).status_code
        == 403
    )
    clock.now += timedelta(minutes=6)
    assert c.post("/v1/admin/numbers/purchase", headers=h, json=body).status_code == 403


def test_state_registry_bitmap_blocks_listed_phone(workspace):
    c, h = workspace["client"], workspace["headers"]
    service = c.app.state.service
    from app.db import update

    with service.db.transaction() as s:
        for lead in service.scoped(s, "leads", workspace):
            update(lead, jurisdiction="CA")
    r = c.post(
        "/v1/admin/registry/import",
        headers=h,
        json={
            "kind": "state",
            "jurisdiction": "CA",
            "npas": ["415"],
            "subscription_reference": "state-authorized",
            "refreshed_at": "2026-10-14T17:00:00+00:00",
            "numbers": ["+14155550101", "+14155550102", "+14155550103"],
        },
    )
    assert r.status_code == 200, r.text
    c.post(
        f"/v1/admin/policies/{workspace['policy_id']}/publish",
        headers=h,
        json={"approved": True, "state_registry_required": True},
    )
    assert reserve(workspace).status_code == 409
