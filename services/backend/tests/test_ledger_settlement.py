"""Finished work settles to a labeled estimate; budgets sum exact money and never block safety."""

import asyncio
from datetime import timedelta

import pytest
from app.db import get, put, rows, update
from app.providers.inference import InferenceResult
from conftest import save_operating_policy, start
from test_control_completion import principal
from test_inbound_completion import configure, dispatch, incoming, previous_call, saved


def hold_for(service, p, call_id):
    with service.db.transaction(False) as s:
        return dict(service._by_key(s, p, "usage_ledger", "call:" + call_id).data)


def event(w, call, name, at=None):
    from uuid import uuid4

    return w["client"].post(
        "/v1/simulation/event",
        headers=w["headers"],
        json={
            "event_id": str(uuid4()),
            "event_type": name,
            "call_id": call["id"],
            "payload": {"occurred_at": at.isoformat()} if at else {},
        },
    )


def test_ended_call_settles_its_hold_to_a_duration_estimate(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    clock.now += timedelta(seconds=5)
    event(workspace, call, "call.answered")
    clock.now += timedelta(seconds=120)
    workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    hold = hold_for(service, p, call["id"])
    # 125 s billed as 3 leg-minutes on two legs at $0.002, plus two STT streams for 2 minutes.
    assert hold["status"] == "estimated" and hold["reserved_usd"] == 0
    assert hold["cost_usd"] == pytest.approx(0.012 + 0.008)
    assert hold["billing_verified"] is False
    assert hold["estimate_source"] == "call_duration_register_rates"


def test_account_voice_rate_overrides_the_reference_rate(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    saved_policy = save_operating_policy(
        workspace["client"],
        "/v1/admin/operating-policy",
        headers=workspace["headers"],
        json={
            "account_rates": {
                "voice": {
                    "values": {"api_per_minute_usd": "0.01"},
                    "source_reference": "Own carrier invoice",
                    "checked_at": "2026-10-14",
                }
            }
        },
    )
    assert saved_policy.status_code == 200, saved_policy.text
    call = start(workspace)
    clock.now += timedelta(seconds=30)
    workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    assert hold_for(service, p, call["id"])["cost_usd"] == pytest.approx(2 * 1 * 0.01)


def test_unknown_duration_still_settles_at_the_per_call_estimate(workspace, app):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    with service.db.transaction() as s:
        current = get(s, "call_attempts", call["id"])
        update(current, submitted_at=None)
        service.finish_call(s, p, current, "provider_terminal_reconciliation")
    hold = hold_for(service, p, call["id"])
    assert hold["status"] == "estimated" and hold["cost_usd"] == 0.05 and hold["reserved_usd"] == 0
    assert hold["estimate_source"] == "configured_per_call_estimate"


def test_call_that_never_reached_the_carrier_releases_its_hold(workspace, app, monkeypatch):
    from app.providers.simulated import CarrierNotSubmitted

    service, p = app.state.service, principal(workspace)

    async def refused(payload):
        raise CarrierNotSubmitted("connection refused")

    monkeypatch.setattr(service.simulated_carrier, "dial", refused)
    call = start(workspace)
    hold = hold_for(service, p, call["id"])
    assert hold["status"] == "released" and hold["cost_usd"] == 0 and hold["reserved_usd"] == 0


def test_finished_calls_stop_consuming_the_daily_budget(workspace, app, clock):
    with app.state.db.transaction() as s:
        for budget in rows(s, "budgets"):
            # One open 5-cent hold exhausts this budget; a finished call must not keep it.
            update(budget, daily_limit_usd=0.05, monthly_limit_usd=0.05)
    for index in range(2):
        call = start(workspace, key=f"budget-call-{index}")
        clock.now += timedelta(seconds=20)
        workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])


def test_complete_carrier_statement_replaces_the_estimate(workspace, app, clock):
    from test_billing_resolution import cdr, resolve
    from test_billing_resolution import saved as saved_entry

    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    clock.now += timedelta(seconds=30)
    workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    assert cdr(workspace, call).status_code == 200
    with service.db.transaction(False) as s:
        row = service._by_key(s, p, "usage_ledger", "call:" + call["id"])
        hold = {"id": row.id, "version": row.version, **row.data}
    result = resolve(
        workspace,
        hold,
        action="close_call",
        provider="telnyx",
        request_id=None,
        receipt_id="complete-statement",
        cost_usd=0,
        provider_record_ids=["cdr-original"],
        confirm_complete=True,
    )
    assert result.status_code == 200, result.text
    closed = saved_entry(app, hold)
    assert closed["cost_usd"] == 0 and closed["billing_verified"] is True and closed["status"] == "settled"


def test_budget_sums_exact_money_not_float_accumulation(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as s:
        for budget in rows(s, "budgets"):
            update(budget, daily_limit_usd=0.3, monthly_limit_usd=0.3)
        for index in range(3):
            put(
                s,
                "usage_ledger",
                p["tenant_id"],
                p["seller_id"],
                {"kind": "receipt", "status": "settled", "cost_usd": 0.1, "at": clock.now.isoformat()},
                key=f"exact-{index}",
                now=clock.now,
            )
    with service.db.transaction(False) as s:
        assert service.budget_ok(s, p, 0)
    with service.db.transaction(False) as s:
        assert not service.budget_ok(s, p, 0.000001)


def test_hangup_optout_stops_and_recovery_never_wait_for_budget(workspace, app, carrier, clock):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    event(workspace, call, "call.answered")
    workspace["client"].post("/v1/admin/control/run", headers=workspace["headers"])
    with service.db.transaction() as s:
        for budget in rows(s, "budgets"):
            update(budget, daily_limit_usd=0, monthly_limit_usd=0)
        phone = get(s, "leads", call["lead_id"]).data["phone"]
    assert (
        workspace["client"]
        .post("/v1/suppressions", headers=workspace["headers"], json={"phone": phone})
        .status_code
        == 200
    )
    workspace["client"].post("/v1/admin/control/run", headers=workspace["headers"])
    assert "transcription_stop" in {a["action"] for a in carrier.actions}
    assert [h["provider_id"] for h in carrier.hangups] == [call["provider_id"]]
    hangup = workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    assert hangup.status_code == 200
    clock.now += timedelta(hours=3)
    asyncio.run(service.recover(p))
    assert workspace["client"].get(f"/v1/calls/{call['id']}", headers=workspace["headers"]).json()[
        "state"
    ] in {
        "ENDED",
        "FINALIZED",
    }


def test_exhausted_routing_budget_keeps_a_durable_callback_task(workspace, carrier):
    _, phone = previous_call(workspace)
    configure(workspace, destination="sip:reception@example.test")
    service = workspace["client"].app.state.service
    with service.db.transaction() as s:
        for budget in rows(s, "budgets"):
            update(budget, daily_limit_usd=0, monthly_limit_usd=0)
    task = incoming(workspace, phone=phone)
    dispatch(workspace)
    current = saved(workspace, "followup_tasks", task["id"])
    assert not any(a["action"] in {"speak", "transfer"} for a in carrier.actions)
    assert any(h["provider_id"] == "inbound-1" for h in carrier.hangups)
    assert current["status"] == "open" and current["action"] == "Return call"
    assert current["owner_id"] == workspace["user_id"] and current["due_at"]
    assert not current.get("review_required")
    assert current["routing_fallback"] == "budget_exhausted_no_cost"


def screening(workspace, app, clock, monkeypatch, credits):
    from app.providers.dnc import PrepaidDNC

    service, p = app.state.service, principal(workspace)
    service.vault.write(
        p["tenant_id"],
        {
            "dnc_api_key": "synthetic-private-key",
            "dnc_seller_authorization": {
                "reference": "own-source-reference",
                "npas": ["415", "212"],
                "seller_id": p["seller_id"],
                "source_access_verified": True,
                "source_refreshed_at": clock.now.isoformat(),
                "expires_at": (clock.now + timedelta(days=30)).isoformat(),
            },
        },
    )
    remaining = iter(credits)

    async def check(provider, phone):
        return {
            "phone": phone,
            "listed": False,
            "checked_at": clock.now.isoformat(),
            "provider": "donotcalldnc",
            "source_authorization_required": True,
            "credits_remaining": next(remaining),
        }

    monkeypatch.setattr(PrepaidDNC, "check", check)
    return service, p


def dnc_holds(service, p):
    with service.db.transaction(False) as s:
        return sorted(
            (r.data for r in service.scoped(s, "usage_ledger", p) if r.data.get("work_kind") == "dnc_screen"),
            key=lambda r: r["at"],
        )


def screen(w, phone):
    return w["client"].post("/v1/admin/dnc/screen", headers=w["headers"], json={"phones": [phone]})


def test_answered_lookup_settles_at_the_configured_query_price(workspace, app, clock, monkeypatch):
    service, p = screening(workspace, app, clock, monkeypatch, [99])
    assert screen(workspace, "+14155550101").status_code == 200
    (hold,) = dnc_holds(service, p)
    assert hold["status"] == "estimated" and hold["reserved_usd"] == 0
    assert hold["cost_usd"] == pytest.approx(100 / 60000)
    assert hold["estimate_source"] == "configured_per_query_rate"


def test_consecutive_credit_balances_price_the_second_lookup(workspace, app, clock, monkeypatch):
    service, p = screening(workspace, app, clock, monkeypatch, [99, 97])
    assert screen(workspace, "+14155550101").status_code == 200
    clock.now += timedelta(seconds=1)
    assert screen(workspace, "+12125550102").status_code == 200
    first, second = dnc_holds(service, p)
    assert second["estimate_source"] == "credits_remaining_delta"
    assert second["cost_usd"] == pytest.approx(2 * 100 / 60000)


def test_priced_provider_receipt_replaces_the_lookup_estimate(workspace, app, clock, monkeypatch):
    service, p = screening(workspace, app, clock, monkeypatch, [99])
    assert screen(workspace, "+14155550101").status_code == 200
    (hold,) = dnc_holds(service, p)
    service.usage(
        p,
        "donotcalldnc",
        "dnc_screen",
        InferenceResult({}, {"cost": 0.0015}, "provider-receipt-1"),
        object_id=hold["object_id"],
        request_key=hold["request_key"],
    )
    (settled,) = dnc_holds(service, p)
    assert settled["status"] == "settled" and settled["billing_verified"] is True
    assert settled["cost_usd"] == 0.0015 and settled["estimate_source"] is None
