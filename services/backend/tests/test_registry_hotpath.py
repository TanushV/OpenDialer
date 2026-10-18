"""A40/A50: exact DNC authorization without decrypting unrelated screening history."""

import base64
from datetime import timedelta

import pytest
from app.compliance.operating import screening_key
from app.compliance.policy import authorize
from app.compliance.registry import RegistryBitmap
from app.db import get, put, update
from conftest import ready
from test_context_reads import count_decrypted
from test_remediation_privacy import principal


def add_history(service, p, clock, count=400):
    with service.db.transaction() as s:
        for i in range(count):
            put(
                s,
                "registry_snapshots",
                p["tenant_id"],
                p["seller_id"],
                {
                    "kind": "national",
                    "published": False,
                    "npas": ["415"],
                    "refreshed_at": (clock.now - timedelta(days=i + 1)).isoformat(),
                },
                now=clock.now,
            )
            put(
                s,
                "registry_snapshots",
                p["tenant_id"],
                p["seller_id"],
                {
                    "kind": "prepaid",
                    "status": "completed",
                    "phone": f"+1415556{i:04d}",
                    "checked_at": clock.now.isoformat(),
                    "listed": False,
                },
                key=screening_key(service, f"+1415556{i:04d}"),
                now=clock.now,
            )


def test_reservation_ignores_retired_snapshots_and_unrelated_prepaid_checks(
    workspace, app, clock, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    add_history(service, p, clock)
    ready(workspace)
    decrypted = count_decrypted(app, monkeypatch)
    response = workspace["client"].post(
        "/v1/queue/next", headers=workspace["headers"], json={"campaign_id": workspace["campaign_id"]}
    )
    assert response.status_code == 200, response.text
    assert len(decrypted) <= 180, len(decrypted)


def test_precommand_context_ignores_registry_history(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    add_history(service, p, clock)
    ready(workspace)
    with service.db.transaction(False) as s:
        lead = next(r for r in service.scoped(s, "leads", p) if r.data["phone"] == "+14155550101")
        number = service.scoped(s, "number_inventory", p)[0]
        lead_id, number_id = lead.id, number.id
    decrypted = count_decrypted(app, monkeypatch)
    with service.db.transaction(False) as s:
        context = service.context(s, p, get(s, "leads", lead_id), get(s, "number_inventory", number_id))
        assert context.registry_covered and not context.registry_listed
    assert len(decrypted) <= 20, len(decrypted)


def test_bitmap_is_decoded_once_per_decision_not_per_candidate(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    bitmap = RegistryBitmap.build(["+14155559999"])
    with service.db.transaction() as s:
        registry = service.scoped(s, "registry_snapshots", p)[0]
        update(registry, bitmap=base64.b64encode(bitmap.dump()).decode(), checksum=bitmap.checksum)
        lead = service.scoped(s, "leads", p)[0]
        for i in range(20):
            put(s, "leads", p["tenant_id"], p["seller_id"], dict(lead.data), now=clock.now)
    ready(workspace)
    load, calls = RegistryBitmap.load, []

    def counted(*args):
        calls.append(1)
        return load(*args)

    monkeypatch.setattr(RegistryBitmap, "load", counted)
    service._selection(p, workspace["campaign_id"])
    assert len(calls) == 1, len(calls)


def test_internal_dnc_blocks_before_any_national_bitmap_decode(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    phone = "+14155550101"
    service.suppress(p, phone, "opt_out")
    with service.db.transaction() as s:
        registry = service.scoped(s, "registry_snapshots", p)[0]
        update(registry, bitmap=base64.b64encode(b"unneeded-data").decode(), checksum="invalid")

    def forbidden(*args):
        raise AssertionError("Internal DNC was not checked first")

    monkeypatch.setattr(RegistryBitmap, "load", forbidden)
    with service.db.transaction(False) as s:
        lead = next(r for r in service.scoped(s, "leads", p) if r.data["phone"] == phone)
        number = service.scoped(s, "number_inventory", p)[0]
        decision = authorize(service.context(s, p, lead, number))
    assert not decision.allowed and "INTERNAL_SUPPRESSION" in decision.reasons


@pytest.mark.parametrize("listed", [True, False])
def test_bounded_prefetch_keeps_the_exact_current_prepaid_result(workspace, app, clock, listed):
    service, p = app.state.service, principal(workspace)
    phone = "+14155550101"
    service.vault.write(
        p["tenant_id"],
        {
            "dnc_seller_authorization": {
                "reference": "seller-source",
                "seller_id": p["seller_id"],
                "npas": ["415"],
                "source_access_verified": True,
                "source_refreshed_at": clock.now.isoformat(),
                "expires_at": (clock.now + timedelta(days=1)).isoformat(),
            }
        },
    )
    with service.db.transaction() as s:
        for row in service.scoped(s, "registry_snapshots", p):
            update(row, published=False)
        put(
            s,
            "registry_snapshots",
            p["tenant_id"],
            p["seller_id"],
            {
                "kind": "prepaid",
                "published": False,
                "status": "completed",
                "phone": phone,
                "listed": listed,
                "checked_at": clock.now.isoformat(),
                "authorization_reference": "seller-source",
                "expires_at": (clock.now + timedelta(hours=1)).isoformat(),
            },
            key=screening_key(service, phone),
            now=clock.now,
        )
    with service.db.transaction(False) as s:
        lead = next(r for r in service.scoped(s, "leads", p) if r.data["phone"] == phone)
        service._queue_context(s, p, workspace["campaign_id"], [lead])
        number = service.scoped(s, "number_inventory", p)[0]
        context = service.context(s, p, lead, number)
        assert context.registry_covered is True and context.registry_listed is listed
