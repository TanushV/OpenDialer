"""A19-A21/A40/A49: no recurring activation and authorized prepaid screening."""

from datetime import timedelta

import httpx
import pytest
from app.db import update
from conftest import reserve

PHONE = "+14155550101"


@pytest.mark.asyncio
async def test_prepaid_adapter_uses_header_and_verifies_exact_result(clock):
    from app.providers.dnc import PrepaidDNC

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "phone": PHONE[2:],
                "is_dnc": False,
                "status": "NOT DNC",
                "credits_remaining": 99,
                "timestamp": clock.now.isoformat(),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await PrepaidDNC("private-test-key", http=http, clock=clock).check(PHONE)
    assert requests[0].headers["X-API-KEY"] == "private-test-key"
    assert "private-test-key" not in str(requests[0].url)
    assert str(requests[0].url).startswith("https://www.donotcalldnc.com/api/v1/check-1/")
    assert result["listed"] is False and result["checked_at"] == clock.now.isoformat()
    assert result["provider"] == "donotcalldnc" and result["source_authorization_required"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"success": False},
        {"phone": "2125550101"},
        {"is_dnc": "false"},
        {"is_dnc": None},
        {"status": "DNC"},
        {"timestamp": "bad"},
    ],
)
async def test_prepaid_malformed_or_mismatched_response_never_clears(clock, patch):
    from app.providers.dnc import DNCError, PrepaidDNC

    payload = {
        "success": True,
        "phone": PHONE[2:],
        "is_dnc": False,
        "status": "NOT DNC",
        "credits_remaining": 99,
        "timestamp": clock.now.isoformat(),
        **patch,
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as http:
        with pytest.raises(DNCError):
            await PrepaidDNC("test", http=http, clock=clock).check(PHONE)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_prepaid_denial_exhausted_credits_and_outage_do_not_retry_spend(clock, status):
    from app.providers.dnc import DNCError, PrepaidDNC

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={"error": "unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DNCError):
            await PrepaidDNC("test", http=http, clock=clock).check(PHONE)
    assert len(seen) == 1


def test_new_number_purchase_rejects_recurring_commitment(workspace, carrier, monkeypatch):
    c, h = workspace["client"], workspace["headers"]
    requests = []

    async def purchase(*args, **kwargs):
        requests.append(args)
        return {"id": "unwanted-order"}

    # No adapter orders numbers; any future call path is still recorded here.
    monkeypatch.setattr(carrier, "purchase", purchase, raising=False)
    c.put(
        "/v1/admin/budget",
        headers=h,
        json={"daily_limit_usd": 100, "monthly_limit_usd": 1000, "number_purchase_limit_usd": 100},
    )
    quote = c.post("/v1/admin/numbers/search", headers=h, json={"region": "415"}).json()["numbers"][0]
    response = c.post(
        "/v1/admin/numbers/purchase",
        headers=h,
        json={
            "phone_numbers": [quote["phone"]],
            "approved_budget_usd": 10,
            "idempotency_key": "cannot-authorize-subscription",
        },
    )
    assert response.status_code == 403
    assert "RECURRING" in response.text
    assert not requests


def test_no_subscription_capabilities_are_visible_and_not_false_ready(workspace):
    c, h = workspace["client"], workspace["headers"]
    r = c.get("/v1/admin/operating-policy", headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["subscriptions_allowed"] is False
    assert data["recording_enabled"] is False and data["raw_retention_seconds"] == 300
    assert data["external_reputation"]["available"] is False
    assert data["dnc"]["authorization_required"] is True
    assert data["costs"]["number_rental"]["recurring"] is True


def test_national_screening_cannot_be_disabled_by_ordinary_policy_toggle(workspace, app):
    c, h = workspace["client"], workspace["headers"]
    r = c.post(
        "/v1/admin/policies/" + workspace["policy_id"] + "/publish",
        headers=h,
        json={"approved": True, "registry_required": False},
    )
    assert r.status_code in {403, 422}, r.text


def test_dnc_expiry_between_reservation_and_dial_blocks_carrier(workspace, app, clock, carrier):
    packet = reserve(workspace).json()
    service = app.state.service
    p = {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}
    with service.db.transaction() as s:
        for row in service.scoped(s, "registry_snapshots", p):
            update(row, refreshed_at=(clock.now - timedelta(days=32)).isoformat())
    c, h = workspace["client"], workspace["headers"]
    r = c.post(
        "/v1/calls/start",
        headers=h,
        json={"reservation_id": packet["reservation_id"], "idempotency_key": "stale-screen"},
    )
    assert r.status_code == 403, r.text
    assert not carrier.calls


def test_local_optout_still_blocks_even_with_a_fresh_clear_national_check(workspace):
    packet = reserve(workspace).json()
    c, h = workspace["client"], workspace["headers"]
    phone = packet["lead"]["phone"]
    assert (
        c.post("/v1/suppressions", headers=h, json={"phone": phone, "reason": "opt_out"}).status_code == 200
    )
    r = c.post(
        "/v1/calls/start",
        headers=h,
        json={"reservation_id": packet["reservation_id"], "idempotency_key": "after-optout"},
    )
    assert r.status_code in {403, 409}


def test_prepaid_screening_does_not_substitute_for_seller_authorization(workspace):
    result = workspace["client"].post(
        "/v1/admin/dnc/screen", headers=workspace["headers"], json={"phones": [PHONE]}
    )
    assert result.status_code in {403, 409}
    assert "AUTHORIZATION" in result.text or "CONFIGURED" in result.text


def test_dnc_api_key_is_protected_and_not_in_settings_response(workspace):
    c, h = workspace["client"], workspace["headers"]
    response = c.put("/v1/admin/settings", headers=h, json={"dnc_api_key": "test-sensitive-dnc"})
    assert response.status_code == 200, response.text
    assert response.json()["configured"]["dnc_api_key"]
    assert "test-sensitive-dnc" not in response.text


def test_covered_seller_prepaid_screen_is_cached_and_keeps_local_suppression(
    workspace, app, clock, monkeypatch
):
    from app.providers.dnc import PrepaidDNC

    service = app.state.service
    seen = []

    async def check(self, phone):
        seen.append(phone)
        return {
            "phone": phone,
            "listed": False,
            "checked_at": clock.now.isoformat(),
            "provider": "donotcalldnc",
            "source_authorization_required": True,
            "credits_remaining": 99,
        }

    monkeypatch.setattr(PrepaidDNC, "check", check)
    service.vault.write(
        workspace["tenant_id"],
        {
            "dnc_api_key": "fixture",
            "dnc_seller_authorization": {
                "reference": "seller-own-SAN",
                "npas": ["415"],
                "expires_at": (clock.now + timedelta(days=30)).isoformat(),
                "source_access_verified": True,
            },
        },
    )
    c, h = workspace["client"], workspace["headers"]
    for _ in range(2):
        result = c.post("/v1/admin/dnc/screen", headers=h, json={"phones": [PHONE]})
        assert result.status_code == 200, result.text
        assert result.json()["results"][0]["listed"] is False
    assert seen == [PHONE]
    assert (
        c.post("/v1/suppressions", headers=h, json={"phone": PHONE, "reason": "opt_out"}).status_code == 200
    )
    lead = next(x for x in c.get("/v1/workspace", headers=h).json()["leads"] if x["phone"] == PHONE)
    assert lead["suppressed"]


def test_overlapping_prepaid_requests_submit_only_once_and_uncertainty_survives_reload(
    workspace, app, clock, monkeypatch
):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from app.providers.dnc import DNCError, PrepaidDNC

    service = app.state.service
    calls = []

    async def fail_after_send(self, phone):
        calls.append(phone)
        await asyncio.sleep(0.1)
        raise DNCError("lost response")

    monkeypatch.setattr(PrepaidDNC, "check", fail_after_send)
    service.vault.write(
        workspace["tenant_id"],
        {
            "dnc_api_key": "fixture",
            "dnc_seller_authorization": {
                "reference": "SAN",
                "npas": ["415"],
                "source_access_verified": True,
                "expires_at": (clock.now + timedelta(days=30)).isoformat(),
            },
        },
    )

    def screen():
        return workspace["client"].post(
            "/v1/admin/dnc/screen", headers=workspace["headers"], json={"phones": [PHONE]}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: screen(), range(2)))
    responses.append(screen())
    assert all(r.status_code in {409, 503} for r in responses)
    assert calls == [PHONE]
    p = {k: workspace[k] for k in ("tenant_id", "seller_id", "user_id")}
    with service.db.transaction(False) as s:
        entries = [r.data for r in service.scoped(s, "usage_ledger", p) if r.data.get("kind") == "dnc_screen"]
        assert len(entries) == 1
