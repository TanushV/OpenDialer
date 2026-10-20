"""A49: operator receipts resolve one liability; partial CDRs and credits stay attributable."""

from datetime import timedelta

import pytest
from app.db import get, put, update
from conftest import completed_metrics, disposition_body, start
from test_context_reads import count_decrypted
from test_remediation_privacy import principal
from test_request_accounting import entries, receipt, reserve


def uncertain(workspace, app, key="one", request="provider-one"):
    service, p = app.state.service, principal(workspace)
    reserve(service, p, key)
    identity = receipt(service, p, key, request=request, cost=None)
    return next(row for row in entries(service, p) if row["id"] == identity)


def body(row, **changes):
    return {
        "action": "settle",
        "expected_version": row["version"],
        "provider": "openrouter",
        "receipt_id": "invoice-line-1",
        "source_reference": "Synthetic provider statement line 1",
        "request_id": row.get("request_id"),
        "cost_usd": 0.003,
        "attest_original": True,
        **changes,
    }


def resolve(workspace, row, **changes):
    return workspace["client"].post(
        f"/v1/admin/billing/ledger/{row['id']}/reconcile",
        headers=workspace["headers"],
        json=body(row, **changes),
    )


def ledger(workspace, **params):
    return workspace["client"].get("/v1/admin/billing/ledger", headers=workspace["headers"], params=params)


def saved(app, row):
    with app.state.db.transaction(False) as s:
        target = get(s, "usage_ledger", row["id"])
        return {"id": target.id, "version": target.version, **target.data}


def cdr(workspace, call, **changes):
    return workspace["client"].post(
        "/v1/admin/billing/reconcile",
        headers=workspace["headers"],
        json={
            "source_reference": "Synthetic carrier statement",
            "rows": [
                {
                    "call_id": call["id"],
                    "provider_record_id": "cdr-original",
                    "leg_id": "prospect-leg",
                    "kind": "voice",
                    "duration_seconds": 61,
                    "cost_usd": 0.02,
                    **changes,
                }
            ],
        },
    )


def call_hold(workspace, app):
    call = start(workspace)
    hold = next(
        row
        for row in entries(app.state.service, principal(workspace))
        if row.get("kind") == "call_authorization"
    )
    return call, hold


def test_partial_carrier_receipt_does_not_clear_other_leg_liability(workspace, app):
    call, hold = call_hold(workspace, app)
    assert cdr(workspace, call).status_code == 200
    current = saved(app, hold)
    assert current["reserved_usd"] == 0.05
    assert not current["billing_verified"]


@pytest.mark.parametrize(
    "change",
    [{"cost_usd": 0.03}, {"duration_seconds": 62}, {"leg_id": "agent-leg"}, {"kind": "transcription"}],
)
def test_same_carrier_receipt_cannot_change_attribution(workspace, app, change):
    call, _ = call_hold(workspace, app)
    assert cdr(workspace, call).status_code == 200
    before = entries(app.state.service, principal(workspace))
    assert cdr(workspace, call, **change).status_code == 409
    assert entries(app.state.service, principal(workspace)) == before


def test_settlement_reuses_original_liability_and_is_retryable(workspace, app):
    row = uncertain(workspace, app)
    first = resolve(workspace, row)
    assert first.status_code == 200, first.text
    assert resolve(workspace, row).json() == first.json()
    current = saved(app, row)
    assert current["cost_usd"] == 0.003 and current["reserved_usd"] == 0
    assert current["request_id"] == "provider-one"
    assert first.json()["evidence"] == "operator-imported provider record"
    assert first.json()["invoice_verified"] is False
    assert len(entries(app.state.service, principal(workspace))) == 1
    # A later provider receipt is the same gross cost, not a second charge.
    receipt(app.state.service, principal(workspace), cost=0.003)
    assert saved(app, row)["cost_usd"] == 0.003


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": "unrelated-request"},
        {"provider": "unrelated-provider"},
        {"expected_version": 999},
        {"attest_original": False},
        {"source_reference": ""},
        {"cost_usd": -1},
        {"cost_usd": True},
        {"cost_usd": "NaN"},
    ],
)
def test_invalid_or_changed_settlement_cannot_release_liability(workspace, app, change):
    row = uncertain(workspace, app)
    result = resolve(workspace, row, **change)
    assert result.status_code in {409, 422}, result.text
    assert saved(app, row) == row


def test_one_statement_receipt_cannot_settle_two_attempts(workspace, app):
    first = uncertain(workspace, app)
    second = uncertain(workspace, app, "two", request="provider-two")
    assert resolve(workspace, first).status_code == 200
    assert resolve(workspace, second).status_code == 409
    assert saved(app, second)["reserved_usd"] == 0.02


def test_settlement_cannot_close_an_active_reservation(workspace, app):
    service, p = app.state.service, principal(workspace)
    reserve(service, p)
    row = entries(service, p)[0]
    assert resolve(workspace, row).status_code == 409
    assert saved(app, row)["reserved_usd"] == 0.02


def test_credit_preserves_gross_and_changes_only_the_original_budget_period(workspace, app, clock):
    row = uncertain(workspace, app)
    assert resolve(workspace, row).status_code == 200
    row = saved(app, row)
    original_time = row["at"]
    clock.now += timedelta(days=1)
    # Refresh the expired UI session after the synthetic one-day jump.
    login = workspace["client"].post(
        "/v1/auth/session", json={"email": "operator@example.test", "password": "a-test-only-password-123"}
    )
    workspace["headers"] = {"Authorization": "Bearer " + login.json()["access_token"]}
    response = resolve(workspace, row, action="credit", receipt_id="credit-original", cost_usd=0.001)
    assert response.status_code == 200, response.text
    assert (
        resolve(workspace, row, action="credit", receipt_id="credit-original", cost_usd=0.001).json()
        == response.json()
    )
    current = saved(app, row)
    assert current["cost_usd"] == 0.003 and current["credited_usd"] == 0.001
    assert current["at"] == original_time
    with app.state.db.transaction(False) as s:
        assert get(s, "usage_ledger", row["id"]).op_amount == pytest.approx(0.002)
    # Replayed gross receipt must not remove the independently recorded credit.
    receipt(app.state.service, principal(workspace), cost=0.003)
    assert saved(app, row)["credited_usd"] == 0.001
    # Credit for yesterday never grants extra daily budget today.
    with app.state.db.transaction() as s:
        update(app.state.service.scoped(s, "budgets", principal(workspace))[0], daily_limit_usd=0.01)
    assert not reserve(app.state.service, principal(workspace), "too-much", estimate=0.011)


def test_credit_cannot_exceed_charge_or_apply_to_uncertainty(workspace, app):
    row = uncertain(workspace, app)
    assert (
        resolve(workspace, row, action="credit", receipt_id="credit-unknown", cost_usd=0.001).status_code
        == 409
    )
    assert resolve(workspace, row).status_code == 200
    row = saved(app, row)
    assert resolve(workspace, row, action="credit", receipt_id="credit-1", cost_usd=0.002).status_code == 200
    row = saved(app, row)
    assert resolve(workspace, row, action="credit", receipt_id="credit-2", cost_usd=0.002).status_code == 409
    assert saved(app, row)["credited_usd"] == 0.002


def test_read_lists_uncertainty_without_prompts_secrets_or_provider_work(workspace, app, monkeypatch):
    row = uncertain(workspace, app)
    with app.state.db.transaction() as s:
        update(get(s, "usage_ledger", row["id"]), usage={"prompt": "secret original speech"})
    monkeypatch.setattr(app.state.service, "usage", lambda *a, **k: pytest.fail("Read attempted settlement"))
    response = ledger(workspace)
    assert response.status_code == 200, response.text
    assert response.json()["entries"][0]["id"] == row["id"]
    assert "secret original speech" not in response.text
    assert "usage" not in response.json()["entries"][0]


def test_ledger_pages_and_cdr_import_do_not_decrypt_history(workspace, app, clock, monkeypatch):
    call, hold = call_hold(workspace, app)
    p = principal(workspace)
    with app.state.db.transaction() as s:
        for i in range(600):
            put(
                s,
                "usage_ledger",
                p["tenant_id"],
                p["seller_id"],
                {
                    "provider": "telnyx",
                    "kind": "voice",
                    "status": "settled",
                    "cost_usd": 0.001,
                    "at": clock.now.isoformat(),
                },
                key=f"cdr:old-{i}",
                now=clock.now,
            )
    reads = count_decrypted(app, monkeypatch)
    assert cdr(workspace, call).status_code == 200
    assert len(reads) <= 12, f"CDR import decrypted {len(reads)} records"
    reads.clear()
    response = ledger(workspace, status="all")
    assert response.status_code == 200
    page = response.json()
    assert len(page["entries"]) == 50 and page["next_cursor"]
    assert len(reads) <= 55
    second = ledger(workspace, status="all", cursor=page["next_cursor"]).json()
    assert not {row["id"] for row in page["entries"]}.intersection(row["id"] for row in second["entries"])


def test_only_confirmed_complete_terminal_call_receipts_release_call_hold(workspace, app):
    call, hold = call_hold(workspace, app)
    assert cdr(workspace, call).status_code == 200
    hold = saved(app, hold)
    args = {
        "action": "close_call",
        "provider": "telnyx",
        "request_id": None,
        "receipt_id": "complete-statement",
        "cost_usd": 0,
        "provider_record_ids": ["cdr-original"],
        "confirm_complete": True,
    }
    assert resolve(workspace, hold, **args).status_code == 409
    workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    hold = saved(app, hold)
    assert resolve(workspace, hold, **{**args, "provider_record_ids": []}).status_code == 409
    assert resolve(workspace, hold, **{**args, "confirm_complete": False}).status_code in {409, 422}
    result = resolve(workspace, hold, **args)
    assert result.status_code == 200, result.text
    assert saved(app, hold)["reserved_usd"] == 0
    assert resolve(workspace, hold, **args).json() == result.json()
    # A newly discovered leg reopens the completion claim rather than hiding it.
    assert cdr(workspace, call, provider_record_id="late-agent-cdr", leg_id="agent-leg").status_code == 200
    current = saved(app, hold)
    assert current["reserved_usd"] == 0.05 and current["status"] == "uncertain"


def test_another_seller_or_rep_cannot_settle_original_liability(workspace, app):
    row = uncertain(workspace, app)
    p = principal(workspace)
    with app.state.db.transaction() as s:
        update(get(s, "users", p["user_id"]), role="rep")
    assert resolve(workspace, row).status_code == 403
    with app.state.db.transaction() as s:
        update(get(s, "users", p["user_id"]), role="owner")
        target = get(s, "usage_ledger", row["id"])
        target.seller_id = "unrelated-seller"
    assert resolve(workspace, row).status_code == 404


def test_credit_is_used_consistently_by_metrics_and_operational_learning(workspace, app):
    call, hold = call_hold(workspace, app)
    c, h = workspace["client"], workspace["headers"]
    c.post(f"/v1/calls/{call['id']}/hangup", headers=h)
    c.post(
        f"/v1/calls/{call['id']}/disposition",
        headers=h,
        json=disposition_body(c, h, call["id"], **{"outcome": "no_answer"}),
    )
    assert cdr(workspace, call).status_code == 200
    p = principal(workspace)
    row = next(
        row for row in entries(app.state.service, p) if row.get("provider_record_id") == "cdr-original"
    )
    assert (
        resolve(
            workspace, row, action="credit", provider="telnyx", receipt_id="carrier-credit", cost_usd=0.01
        ).status_code
        == 200
    )
    metrics = completed_metrics(workspace)
    assert metrics["gross_cost_usd"] == 0.02 and metrics["credited_cost_usd"] == 0.01
    assert metrics["verified_cost_usd"] == 0.01
    with app.state.db.transaction(False) as s:
        dataset = app.state.service.operational_dataset(s, p)
    assert len(dataset) == 1
    assert dataset[0]["observed_cost_usd"] is None, "One CDR is not proof of complete call cost"
    complete = resolve(
        workspace,
        saved(app, hold),
        action="close_call",
        provider="telnyx",
        request_id=None,
        receipt_id="complete-credit-statement",
        cost_usd=0,
        provider_record_ids=["cdr-original"],
        confirm_complete=True,
    )
    assert complete.status_code == 200, complete.text
    with app.state.db.transaction(False) as s:
        dataset = app.state.service.operational_dataset(s, p)
    assert dataset[0]["observed_cost_usd"] == 0.01


def test_legacy_partial_statement_is_reopened_once_without_losing_records(workspace, app):
    call, hold = call_hold(workspace, app)
    with app.state.db.transaction() as s:
        target = get(s, "usage_ledger", hold["id"])
        update(target, reserved_usd=0, billing_verified=True, reconciled_to_provider_records=True)
        from app.db import MODELS

        s.flush()
        s.execute(
            MODELS["usage_ledger"]
            .__table__.update()
            .where(MODELS["usage_ledger"].id == hold["id"])
            .values(op_index_version=2)
        )
    app.state.db.backfill_operational_columns()
    repaired = saved(app, hold)
    assert repaired["reserved_usd"] == 0.05 and repaired["status"] == "uncertain"
    assert repaired["call_id"] == call["id"]
    assert not repaired["billing_verified"]
    app.state.db.backfill_operational_columns()
    assert saved(app, hold) == repaired


def test_legacy_callback_receipt_cannot_be_reassigned_without_its_request_key(workspace, app, clock):
    call, _ = call_hold(workspace, app)
    p = principal(workspace)
    with app.state.db.transaction() as s:
        old = put(
            s,
            "usage_ledger",
            p["tenant_id"],
            p["seller_id"],
            {
                "provider": "telnyx",
                "provider_record_id": "cdr-original",
                "kind": "callback_transfer",
                "object_id": "original-inbound",
                "request_key": "old-transfer",
                "status": "settled",
                "cost_usd": 0.1,
                "at": clock.now.isoformat(),
                "duration_seconds": 0,
            },
            key="optional:old-transfer",
            now=clock.now,
        )
        from app.db import MODELS

        s.execute(
            MODELS["usage_ledger"]
            .__table__.update()
            .where(MODELS["usage_ledger"].id == old.id)
            .values(op_index_version=2, op_provider_id=None)
        )
    app.state.db.backfill_operational_columns()
    assert cdr(workspace, call).status_code == 409


def test_explicit_zero_charge_statement_can_close_a_terminal_call_without_cdr(workspace, app):
    call, hold = call_hold(workspace, app)
    workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    hold = saved(app, hold)  # The terminal call settled its hold to an estimate; reload it.
    assert hold["status"] == "estimated"
    args = {
        "action": "close_call",
        "provider": "telnyx",
        "receipt_id": "zero-charge-confirmation",
        "request_id": None,
        "cost_usd": 0,
        "confirm_complete": True,
        "provider_record_ids": [],
    }
    assert resolve(workspace, hold, **args).status_code == 409
    result = resolve(workspace, hold, **args, confirm_no_charge=True)
    assert result.status_code == 200, result.text
    closed = saved(app, hold)
    assert closed["reserved_usd"] == 0 and closed["cost_usd"] == 0 and closed["billing_verified"]


def test_same_carrier_receipt_cannot_move_to_a_different_call(workspace, app, clock):
    call, _ = call_hold(workspace, app)
    assert cdr(workspace, call).status_code == 200
    p = principal(workspace)
    with app.state.db.transaction() as s:
        other = put(s, "call_attempts", p["tenant_id"], p["seller_id"], {"state": "ENDED"}, now=clock.now)
    assert cdr(workspace, {"id": other.id}).status_code == 409


@pytest.mark.database
def test_concurrent_credits_cannot_over_refund_one_charge(workspace, app):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    row = uncertain(workspace, app)
    assert resolve(workspace, row).status_code == 200
    row = saved(app, row)
    barrier = threading.Barrier(2)

    def attempt(identity):
        barrier.wait(timeout=5)
        return resolve(workspace, row, action="credit", receipt_id=identity, cost_usd=0.002).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(attempt, ["credit-one", "credit-two"]))
    assert sorted(statuses) == [200, 409]
    assert saved(app, row)["credited_usd"] == 0.002


def test_reimporting_legacy_cdr_repairs_its_original_call_receipt_set(workspace, app, clock):
    call, hold = call_hold(workspace, app)
    p = principal(workspace)
    with app.state.db.transaction() as s:
        put(
            s,
            "usage_ledger",
            p["tenant_id"],
            p["seller_id"],
            {
                "provider": "telnyx",
                "provider_record_id": "cdr-original",
                "call_id": call["id"],
                "kind": "voice",
                "leg_id": "prospect-leg",
                "duration_seconds": 61,
                "cost_usd": 0.02,
                "billing_verified": True,
                "status": "settled",
                "at": clock.now.isoformat(),
            },
            key="cdr:cdr-original",
            now=clock.now,
        )
    result = cdr(workspace, call)
    assert result.status_code == 200 and result.json()["duplicates"] == 1
    assert saved(app, hold)["provider_record_ids"] == ["cdr-original"]
    assert saved(app, hold)["reserved_usd"] == 0.05


def test_surrounding_whitespace_cannot_create_a_second_credit_identity(workspace, app):
    row = uncertain(workspace, app)
    assert resolve(workspace, row).status_code == 200
    row = saved(app, row)
    result = resolve(workspace, row, action="credit", receipt_id="credit-exact", cost_usd=0.001)
    assert result.status_code == 200
    retry = resolve(workspace, row, action="credit", receipt_id=" credit-exact ", cost_usd=0.001)
    assert retry.status_code == 200 and retry.json() == result.json()
    assert saved(app, row)["credited_usd"] == 0.001


def test_carrier_statement_conflict_rolls_back_the_whole_import(workspace, app):
    call, hold = call_hold(workspace, app)
    c, h = workspace["client"], workspace["headers"]
    row = {
        "call_id": call["id"],
        "provider_record_id": "conflicting",
        "cost_usd": 0.01,
        "duration_seconds": 1,
    }
    response = c.post(
        "/v1/admin/billing/reconcile",
        headers=h,
        json={
            "source_reference": "Conflicting synthetic export",
            "rows": [row, {**row, "cost_usd": 0.02}],
        },
    )
    assert response.status_code == 409
    assert entries(app.state.service, principal(workspace)) == [hold]
