"""A20/A49/A50: scoped, bounded, atomic liabilities and exact attempt receipts."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.db import MODELS, get, put, update
from app.providers.inference import InferenceResult
from sqlalchemy import inspect, select
from test_context_reads import count_decrypted
from test_remediation_privacy import principal


def reserve(service, p, key="one", **kwargs):
    return service.reserve_optional_budget(
        p,
        key,
        kwargs.pop("estimate", 0.02),
        object_id=kwargs.pop("object_id", "call"),
        work_kind=kwargs.pop("kind", "live_coach"),
        **kwargs,
    )


def receipt(service, p, key="one", request="provider-one", cost=0.001, **kwargs):
    return service.usage(
        p,
        kwargs.pop("provider", "openrouter"),
        kwargs.pop("kind", "live_coach"),
        InferenceResult({}, {"cost": cost}, request),
        object_id=kwargs.pop("object_id", "call"),
        request_key=key,
        reserved=0,
        **kwargs,
    )


def entries(service, p):
    with service.db.transaction(False) as s:
        return [{"id": r.id, "version": r.version, **r.data} for r in service.scoped(s, "usage_ledger", p)]


@pytest.mark.parametrize(
    "changes", [{"object_id": "other-call"}, {"kind": "research_extract"}, {"estimate": 0.04}]
)
def test_request_identity_cannot_authorize_different_work(workspace, app, changes):
    service, p = app.state.service, principal(workspace)
    assert reserve(service, p)
    before = entries(service, p)
    with pytest.raises(ValueError):
        reserve(service, p, **changes)
    assert entries(service, p) == before


@pytest.mark.parametrize("changes", [{"object_id": "other-call"}, {"kind": "postcall_extract"}])
def test_receipt_cannot_settle_a_different_work_hold(workspace, app, changes):
    service, p = app.state.service, principal(workspace)
    reserve(service, p)
    before = entries(service, p)
    with pytest.raises(ValueError):
        receipt(service, p, **changes)
    assert entries(service, p) == before


@pytest.mark.parametrize("state", ["settled", "released", "uncertain", "submitted"])
def test_finished_or_possibly_submitted_key_never_readmits_new_spend(workspace, app, state):
    service, p = app.state.service, principal(workspace)
    reserve(service, p)
    with service.db.transaction() as s:
        row = service.scoped(s, "usage_ledger", p)[0]
        update(row, status=state)
    assert reserve(service, p) is False


def test_late_receipt_cannot_replace_original_provider_identity(workspace, app):
    service, p = app.state.service, principal(workspace)
    reserve(service, p)
    receipt(service, p, cost=None)
    before = entries(service, p)
    with pytest.raises(ValueError):
        receipt(service, p, request="different-paid-attempt")
    assert entries(service, p) == before


def test_late_unknown_receipt_never_erases_known_request_id(workspace, app):
    service, p = app.state.service, principal(workspace)
    reserve(service, p)
    receipt(service, p, cost=None)
    receipt(service, p, request=None, cost=None)
    assert entries(service, p)[0]["request_id"] == "provider-one"


def test_same_receipt_cannot_be_bound_to_two_local_attempts(workspace, app):
    service, p = app.state.service, principal(workspace)
    reserve(service, p)
    reserve(service, p, "two")
    receipt(service, p)
    before = entries(service, p)
    with pytest.raises(ValueError):
        receipt(service, p, "two")
    assert entries(service, p) == before


def test_unknown_then_actual_receipt_settles_exactly_once(workspace, app):
    service, p = app.state.service, principal(workspace)
    reserve(service, p)
    identity = receipt(service, p, cost=None)
    assert entries(service, p)[0]["reserved_usd"] == 0.02
    assert receipt(service, p) == identity
    before = entries(service, p)
    assert receipt(service, p) == identity
    assert entries(service, p) == before
    assert before[0]["cost_usd"] == 0.001 and before[0]["reserved_usd"] == 0


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True])
def test_inner_reservation_rejects_invalid_estimate_too(workspace, app, value):
    service, p = app.state.service, principal(workspace)
    with pytest.raises(ValueError), service.db.transaction() as s:
        service.reserve_budget_in(s, p, "invalid", value, object_id="call", work_kind="live_coach")
    assert not entries(service, p)


def test_two_jobs_cannot_each_admit_against_the_same_remaining_budget(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        update(service.scoped(s, "budgets", p)[0], daily_limit_usd=0.03, monthly_limit_usd=0.03)
    original = service.budget_ok
    start = threading.Barrier(2)

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        time.sleep(0.08)  # Both unprotected PG transactions can observe the same total.
        return result

    monkeypatch.setattr(service, "budget_ok", delayed)

    def attempt(key):
        start.wait(timeout=5)
        return reserve(service, p, key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["one", "two"]))
    assert sorted(results) == [False, True]
    assert sum(r["reserved_usd"] for r in entries(service, p)) == 0.02


def test_each_admission_in_one_cached_transaction_sees_previous_reservations(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        update(service.scoped(s, "budgets", p)[0], daily_limit_usd=0.03, monthly_limit_usd=0.03)
    with service.db.transaction() as s:
        s.info["row_cache"] = {}
        assert service.reserve_budget_in(s, p, "one", 0.02, object_id="call", work_kind="live_coach")
        assert not service.reserve_budget_in(s, p, "two", 0.02, object_id="call", work_kind="live_coach")
    assert len(entries(service, p)) == 1


def test_concurrent_duplicate_receipts_return_one_identity(workspace, app):
    service, p = app.state.service, principal(workspace)
    reserve(service, p)
    start = threading.Barrier(4)

    def settle(_):
        start.wait(timeout=5)
        return receipt(service, p)

    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(settle, range(4)))
    assert len(set(identities)) == 1 and len(entries(service, p)) == 1


def test_receipts_and_request_keys_are_seller_and_provider_scoped(workspace, app):
    service, p = app.state.service, principal(workspace)
    reserve(service, p)
    receipt(service, p)
    other = {**p, "seller_id": "other-seller"}
    with service.db.transaction() as s:
        put(
            s,
            "budgets",
            p["tenant_id"],
            other["seller_id"],
            {"daily_limit_usd": 10, "monthly_limit_usd": 100},
        )
    reserve(service, other)
    receipt(service, other)
    reserve(service, p, "two")
    receipt(service, p, "two", provider="openai")
    assert len(entries(service, p)) == 2 and len(entries(service, other)) == 1


@pytest.mark.parametrize("operation", ["reserve", "receipt", "release"])
def test_attempt_accounting_does_not_decrypt_unrelated_ledger_history(
    workspace, app, clock, monkeypatch, operation
):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        for i in range(600):
            put(
                s,
                "usage_ledger",
                p["tenant_id"],
                p["seller_id"],
                {
                    "kind": "live_coach",
                    "provider": "openrouter",
                    "request_id": f"old-{i}",
                    "object_id": f"old-call-{i}",
                    "cost_usd": 0.0001,
                    "at": clock.now.isoformat(),
                    "status": "settled",
                    "request_key": f"old-local-{i}",
                },
                key=f"optional:old-local-{i}",
                now=clock.now,
            )
    if operation != "reserve":
        reserve(service, p)
    reads = count_decrypted(app, monkeypatch)
    if operation == "reserve":
        assert reserve(service, p)
    elif operation == "receipt":
        receipt(service, p)
    else:
        service.release_unsubmitted_budget(p, "one")
    assert len(reads) <= 5, f"One attempt decrypted {len(reads)} historical records"


def test_receipt_index_is_hashed_and_backfill_preserves_legacy_identity(workspace, app):
    service, p = app.state.service, principal(workspace)
    reserve(service, p)
    identity = receipt(service, p)
    with service.db.transaction() as s:
        row = get(s, "usage_ledger", identity)
        before = (row.id, dict(row.data), row.version)
        s.execute(
            MODELS["usage_ledger"]
            .__table__.update()
            .where(MODELS["usage_ledger"].id == identity)
            .values(op_index_version=1, op_provider_id=None)
        )
    service.db.backfill_operational_columns()
    with service.db.transaction(False) as s:
        row = get(s, "usage_ledger", identity)
        assert (row.id, dict(row.data), row.version) == before
        assert row.op_provider_id and "provider-one" not in row.op_provider_id
        assert row.op_index_version >= 2
        assert (
            s.scalar(
                select(MODELS["usage_ledger"].id).where(
                    MODELS["usage_ledger"].op_provider_id == row.op_provider_id
                )
            )
            == identity
        )
    assert "ix_usage_ledger_receipt" in {
        i["name"] for i in inspect(service.db.engine).get_indexes("usage_ledger")
    }
