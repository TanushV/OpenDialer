"""A20/A21/A40/A49: no-charge reads, fenced results and explicit uncertain retries."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from app.compliance.operating import cached_screen
from app.db import doc, get, update
from app.providers.dnc import DNCError, PrepaidDNC
from conftest import save_operating_policy
from test_remediation_privacy import principal

PHONE = "+14155550101"


@pytest.fixture
def screening(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    config = {
        "dnc_api_key": "synthetic-private-key",
        "dnc_seller_authorization": {
            "reference": "own-source-reference",
            "npas": ["415"],
            "seller_id": p["seller_id"],
            "source_access_verified": True,
            "source_refreshed_at": clock.now.isoformat(),
            "expires_at": (clock.now + timedelta(days=30)).isoformat(),
        },
    }
    service.vault.write(p["tenant_id"], config)
    calls = []

    async def check(provider, phone):
        calls.append(phone)
        return {
            "phone": phone,
            "listed": False,
            "checked_at": clock.now.isoformat(),
            "provider": "donotcalldnc",
            "source_authorization_required": True,
            "credits_remaining": 99,
        }

    monkeypatch.setattr(PrepaidDNC, "check", check)
    return service, p, calls, check, config


def screen(w):
    return w["client"].post("/v1/admin/dnc/screen", headers=w["headers"], json={"phones": [PHONE]})


def test_lookup_uses_saved_account_rate_without_repricing_old_liability(workspace, screening):  # noqa: F811
    service, p, calls, _, _ = screening
    result = save_operating_policy(
        workspace["client"],
        "/v1/admin/operating-policy",
        headers=workspace["headers"],
        json={
            "account_rates": {
                "dnc": {
                    "values": {"estimated_per_query_usd": "0.004"},
                    "source_reference": "Own prepaid invoice",
                    "checked_at": "2026-10-14",
                }
            }
        },
    )
    assert result.status_code == 200, result.text
    assert screen(workspace).status_code == 200
    with service.db.transaction(False) as s:
        ledger = [r for r in service.scoped(s, "usage_ledger", p) if r.data.get("work_kind") == "dnc_screen"]
        assert len(ledger) == 1
        assert ledger[0].data["reserved_usd"] == 0.004
        identity, before = ledger[0].id, dict(ledger[0].data)
    assert (
        save_operating_policy(
            workspace["client"],
            "/v1/admin/operating-policy",
            headers=workspace["headers"],
            json={
                "account_rates": {
                    "dnc": {
                        "values": {"estimated_per_query_usd": "0.005"},
                        "source_reference": "Later invoice",
                        "checked_at": "2026-10-14",
                    }
                }
            },
        ).status_code
        == 200
    )
    with service.db.transaction(False) as s:
        assert get(s, "usage_ledger", identity).data == before


def snapshot(service, p):
    with service.db.transaction(False) as s:
        return next(
            doc(r) for r in service.scoped(s, "registry_snapshots", p) if r.data.get("kind") == "prepaid"
        )


def retry(w, row, **overrides):
    return w["client"].post(
        f"/v1/admin/dnc/screens/{row['id']}/retry",
        headers=w["headers"],
        json={
            "expected_version": row["version"],
            "idempotency_key": "explicit-second-attempt",
            "accept_possible_duplicate_charge": True,
            **overrides,
        },
    )


def lose_response(monkeypatch, calls):
    async def lost(provider, phone):
        calls.append(phone)
        raise DNCError("sensitive raw provider error must not be returned")

    monkeypatch.setattr(PrepaidDNC, "check", lost)


def test_internal_optout_needs_no_provider_key_or_paid_screen(workspace, monkeypatch):
    seen = []
    lose_response(monkeypatch, seen)
    workspace["client"].post(
        "/v1/suppressions", headers=workspace["headers"], json={"phone": PHONE, "reason": "opt_out"}
    )
    response = screen(workspace)
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["internal_suppressed"] is True
    assert not seen


def test_uncertain_screen_is_visible_and_get_never_repeats_paid_lookup(workspace, screening, monkeypatch):
    service, p, calls, _, _ = screening
    lose_response(monkeypatch, calls)
    assert screen(workspace).status_code == 503
    before = snapshot(service, p)
    for _ in range(2):
        response = workspace["client"].get("/v1/admin/dnc/screens", headers=workspace["headers"])
        assert response.status_code == 200, response.text
        item = next(x for x in response.json()["screens"] if x["id"] == before["id"])
        assert item["status"] in {"uncertain", "review_required"} and item["can_retry"]
        assert item["possible_charge_usd"] > 0 and item["listed"] is None
        assert "sensitive raw" not in response.text and "synthetic-private-key" not in response.text
    assert len(calls) == 1 and snapshot(service, p) == before


def test_retry_retains_previous_liability_and_is_idempotent_after_lost_reply(
    workspace, screening, monkeypatch
):
    service, p, calls, check, _ = screening
    lose_response(monkeypatch, calls)
    assert screen(workspace).status_code == 503
    original = snapshot(service, p)
    monkeypatch.setattr(PrepaidDNC, "check", check)
    result = retry(workspace, original)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed"
    assert retry(workspace, original).status_code == 200
    assert len(calls) == 2
    with service.db.transaction(False) as s:
        costs = [
            r.data for r in service.scoped(s, "usage_ledger", p) if r.data.get("work_kind") == "dnc_screen"
        ]
        assert len(costs) == 2 and len({r["request_key"] for r in costs}) == 2
        assert all(r["cost_usd"] is None and r["reserved_usd"] > 0 for r in costs)
        assert cached_screen(service, s, p, PHONE)["listed"] is False


@pytest.mark.parametrize("patch", [{"accept_possible_duplicate_charge": False}, {"expected_version": 999}])
def test_uncertain_retry_requires_current_version_and_explicit_charge_choice(
    workspace, screening, monkeypatch, patch
):
    service, p, calls, _, _ = screening
    lose_response(monkeypatch, calls)
    assert screen(workspace).status_code == 503
    result = retry(workspace, snapshot(service, p), **patch)
    assert result.status_code in {409, 422}, result.text
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["authority", "role", "suppression"])
def test_late_result_never_overrides_changed_authority_or_internal_optout(
    workspace, screening, monkeypatch, change
):
    service, p, calls, check, config = screening

    async def changed(provider, phone):
        result = await check(provider, phone)
        if change == "authority":
            service.vault.write(
                p["tenant_id"],
                {
                    "dnc_seller_authorization": {
                        **config["dnc_seller_authorization"],
                        "reference": "replacement-source",
                    }
                },
            )
        elif change == "role":
            with service.db.transaction() as s:
                update(get(s, "users", p["user_id"]), role="rep")
        else:
            service.suppress(p, PHONE, "opt_out")
        return result

    monkeypatch.setattr(PrepaidDNC, "check", changed)
    result = screen(workspace)
    assert result.status_code in {403, 409}, result.text
    row = snapshot(service, p)
    assert row["status"] != "completed"
    with service.db.transaction(False) as s:
        assert cached_screen(service, s, p, PHONE) is None
        holds = [
            r.data for r in service.scoped(s, "usage_ledger", p) if r.data.get("work_kind") == "dnc_screen"
        ]
        assert len(holds) == 1 and holds[0]["reserved_usd"] > 0
    assert len(calls) == 1


def test_lost_old_result_cannot_overwrite_a_newer_explicit_lookup(workspace, screening, monkeypatch, clock):
    service, p, calls, check, _ = screening
    started, release = threading.Event(), threading.Event()

    async def delayed(provider, phone):
        result = await check(provider, phone)
        if len(calls) == 1:
            started.set()
            assert await asyncio.to_thread(release.wait, 5)
            result["listed"] = True
        return result

    monkeypatch.setattr(PrepaidDNC, "check", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(screen, workspace)
        try:
            assert started.wait(5)
            clock.now += timedelta(seconds=31)
            current = snapshot(service, p)
            response = retry(workspace, current)
            assert response.status_code == 200, response.text
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 409
    assert snapshot(service, p)["listed"] is False and len(calls) == 2
    with service.db.transaction(False) as s:
        assert (
            len([r for r in service.scoped(s, "usage_ledger", p) if r.data.get("work_kind") == "dnc_screen"])
            == 2
        )


def test_live_screen_lease_prevents_paid_overlap(workspace, screening, monkeypatch):
    service, p, calls, check, _ = screening
    started, release = threading.Event(), threading.Event()

    async def delayed(provider, phone):
        started.set()
        assert await asyncio.to_thread(release.wait, 5)
        return await check(provider, phone)

    monkeypatch.setattr(PrepaidDNC, "check", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(screen, workspace)
        try:
            assert started.wait(5)
            assert retry(workspace, snapshot(service, p)).status_code == 409
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
    assert len(calls) == 1


def test_cache_lifetime_uses_source_refresh_and_shorter_admin_policy(workspace, screening, clock):
    service, p, calls, _, config = screening
    save_operating_policy(
        workspace["client"],
        "/v1/admin/operating-policy",
        headers=workspace["headers"],
        json={"dnc_source_max_age_days": 2},
    )
    service.vault.write(
        p["tenant_id"],
        {
            "dnc_seller_authorization": {
                **config["dnc_seller_authorization"],
                "source_refreshed_at": (clock.now - timedelta(days=1)).isoformat(),
            }
        },
    )
    assert screen(workspace).status_code == 200
    row = snapshot(service, p)
    assert row["expires_at"] <= (clock.now + timedelta(days=1)).isoformat()
    clock.now += timedelta(days=1, seconds=1)
    with service.db.transaction(False) as s:
        assert cached_screen(service, s, p, PHONE) is None
    assert len(calls) == 1


def test_tightened_freshness_invalidates_preexisting_cache(workspace, screening, clock):
    service, p, _, _, _ = screening
    assert screen(workspace).status_code == 200
    clock.now += timedelta(days=3)
    with service.db.transaction() as s:
        seller = get(s, "sellers", p["seller_id"])
        update(
            seller, operating_policy={**seller.data.get("operating_policy", {}), "dnc_source_max_age_days": 2}
        )
    with service.db.transaction(False) as s:
        assert cached_screen(service, s, p, PHONE) is None


def test_legacy_uncertain_row_without_lease_remains_reviewable(workspace, screening, monkeypatch, clock):
    service, p, calls, check, _ = screening
    lose_response(monkeypatch, calls)
    assert screen(workspace).status_code == 503
    row = snapshot(service, p)
    with service.db.transaction() as s:
        old = get(s, "registry_snapshots", row["id"])
        update(
            old, status="sending", lease_until=None, started_at=(clock.now - timedelta(hours=1)).isoformat()
        )
    monkeypatch.setattr(PrepaidDNC, "check", check)
    assert retry(workspace, snapshot(service, p)).status_code == 200
    assert len(calls) == 2


def test_retry_denied_budget_keeps_old_uncertainty_and_never_calls(workspace, screening, monkeypatch):
    service, p, calls, _, _ = screening
    lose_response(monkeypatch, calls)
    assert screen(workspace).status_code == 503
    row = snapshot(service, p)
    with service.db.transaction() as s:
        update(service.scoped(s, "budgets", p)[0], daily_limit_usd=0, monthly_limit_usd=0)
    result = retry(workspace, row)
    assert result.status_code == 403, result.text
    assert len(calls) == 1 and snapshot(service, p) == row


def test_cross_seller_recovery_does_not_reveal_or_retry_a_number(workspace, screening, monkeypatch):
    service, p, calls, _, _ = screening
    lose_response(monkeypatch, calls)
    assert screen(workspace).status_code == 503
    row = snapshot(service, p)
    with service.db.transaction() as s:
        get(s, "registry_snapshots", row["id"]).seller_id = "other-seller"
    assert retry(workspace, row).status_code == 404
    response = workspace["client"].get("/v1/admin/dnc/screens", headers=workspace["headers"])
    assert response.status_code == 200 and not response.json()["screens"]
    assert len(calls) == 1


@pytest.mark.parametrize("patch", [{"phone": "+12125550101"}, {"listed": "false"}, {"checked_at": "invalid"}])
def test_invalid_adapter_result_does_not_publish_clearance(workspace, screening, monkeypatch, patch):
    service, p, _, check, _ = screening

    async def invalid(provider, phone):
        return {**await check(provider, phone), **patch}

    monkeypatch.setattr(PrepaidDNC, "check", invalid)
    response = screen(workspace)
    assert response.status_code == 503, response.text
    assert snapshot(service, p)["status"] != "completed"


def test_screening_history_read_is_bounded_and_legacy_index_preserves_rows(
    workspace, screening, app, clock, monkeypatch
):
    from app.db import MODELS, put
    from sqlalchemy import inspect
    from test_context_reads import count_decrypted

    service, p, calls, _, _ = screening
    with service.db.transaction() as s:
        for i in range(600):
            put(
                s,
                "registry_snapshots",
                p["tenant_id"],
                p["seller_id"],
                {
                    "kind": "prepaid",
                    "status": "completed",
                    "phone": PHONE,
                    "listed": False,
                    "checked_at": clock.now.isoformat(),
                    "expires_at": clock.now.isoformat(),
                    "authorization_reference": "old-source",
                    "synthetic": True,
                },
                key=f"historical-fixture-{i}",
                now=clock.now - timedelta(minutes=i + 1),
            )
    assert screen(workspace).status_code == 200
    row = snapshot(service, p)
    with service.db.transaction() as s:
        saved = get(s, "registry_snapshots", row["id"])
        original = (saved.id, dict(saved.data), saved.version)
        assert hasattr(saved, "ctx_index_version")
        s.execute(MODELS["registry_snapshots"].__table__.update().values(ctx_index_version=None))
    service.db.backfill_operational_columns()
    with service.db.transaction(False) as s:
        saved = get(s, "registry_snapshots", row["id"])
        assert (saved.id, dict(saved.data), saved.version) == original and saved.ctx_key == "prepaid"
    assert "ix_registry_snapshots_context" in {
        i["name"] for i in inspect(service.db.engine).get_indexes("registry_snapshots")
    }
    reads = count_decrypted(app, monkeypatch)
    response = workspace["client"].get("/v1/admin/dnc/screens", headers=workspace["headers"])
    assert response.status_code == 200 and len(response.json()["screens"]) <= 120
    assert len(reads) <= 125 and len(calls) == 1


@pytest.mark.parametrize("patch", [{"phone": "+14155550102"}, {"checked_at": "2099-01-01T00:00:00Z"}])
def test_legacy_cache_requires_exact_phone_and_plausible_check_time(workspace, screening, patch):
    service, p, _, _, _ = screening
    assert screen(workspace).status_code == 200
    row = snapshot(service, p)
    with service.db.transaction() as s:
        update(get(s, "registry_snapshots", row["id"]), **patch)
    with service.db.transaction(False) as s:
        assert cached_screen(service, s, p, PHONE) is None


def test_unexpected_provider_failure_is_sanitized_and_recoverable(workspace, screening, monkeypatch):
    service, p, calls, _, _ = screening

    async def broken(provider, phone):
        calls.append(phone)
        raise RuntimeError("sensitive response containing an API key")

    monkeypatch.setattr(PrepaidDNC, "check", broken)
    response = screen(workspace)
    assert response.status_code == 503 and "sensitive" not in response.text
    assert snapshot(service, p)["status"] == "uncertain" and len(calls) == 1


def test_cancelled_lookup_preserves_uncertainty_and_never_retries(screening, monkeypatch):
    from app.compliance.screening import claim, execute

    service, p, calls, _, _ = screening

    async def cancelled(provider, phone):
        calls.append(phone)
        raise asyncio.CancelledError()

    monkeypatch.setattr(PrepaidDNC, "check", cancelled)
    intent, _, config = claim(service, p, PHONE)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(execute(service, p, intent, config))
    row = snapshot(service, p)
    assert row["status"] == "uncertain" and row["lease_owner"] is None and len(calls) == 1
    with service.db.transaction(False) as s:
        costs = [
            r.data for r in service.scoped(s, "usage_ledger", p) if r.data.get("work_kind") == "dnc_screen"
        ]
        assert len(costs) == 1 and costs[0]["status"] == "uncertain" and costs[0]["reserved_usd"] > 0
