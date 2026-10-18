"""A03/A54: restoration is seller scoped and cannot weaken newer suppressions."""

import json

import pytest
from app.db import get, put, update
from fastapi import HTTPException
from test_control_completion import principal


def second_seller(service, p):
    with service.db.transaction() as s:
        other = put(
            s,
            "sellers",
            p["tenant_id"],
            "other-seller",
            {"name": "Separate business"},
            object_id="other-seller",
        )
        lead = put(s, "leads", p["tenant_id"], other.id, {"name": "Other business private contact"})
        return lead.id


def test_backup_contains_only_its_sellers_private_records(workspace, app):
    service, p = app.state.service, principal(workspace)
    other = second_seller(service, p)
    receipt = service.backup(p)
    path = service.settings.data_dir / "backups" / (receipt["backup_id"] + ".backup")
    payload = json.loads(service.vault.cipher.decrypt(path.read_bytes()))
    assert all(
        r["seller_id"] in {"", p["seller_id"]} for values in payload["tables"].values() for r in values
    )
    assert other not in {r["id"] for r in payload["tables"]["leads"]}


def test_restore_does_not_roll_back_another_sellers_records(workspace, app):
    service, p = app.state.service, principal(workspace)
    other = second_seller(service, p)
    receipt = service.backup(p)
    with service.db.transaction() as s:
        update(get(s, "leads", other), name="New protected correction")
        later = put(s, "notes", p["tenant_id"], "other-seller", {"human_text": "Written after the backup"})
        later_id = later.id
    service.restore(p, receipt["backup_id"])
    with service.db.transaction(False) as s:
        assert get(s, "leads", other).data["name"] == "New protected correction"
        assert get(s, "notes", later_id).data["human_text"] == "Written after the backup"


def test_restore_rejects_same_tenant_backup_for_another_seller(workspace, app):
    service, p = app.state.service, principal(workspace)
    second_seller(service, p)
    receipt = service.backup(p)
    with pytest.raises(HTTPException) as error:
        service.restore({**p, "seller_id": "other-seller"}, receipt["backup_id"])
    assert error.value.status_code in {403, 404}


def test_restore_replays_optout_over_an_older_inactive_suppression(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    phone = "+14155550177"
    with service.db.transaction() as s:
        old = put(
            s,
            "suppressions",
            p["tenant_id"],
            p["seller_id"],
            {"phone": phone, "active": False, "reason": "temporary_hold", "permanent": False},
            key=phone,
        )
        original_id = old.id
    receipt = service.backup(p)
    service.suppress(p, phone, "opt_out")
    service.restore(p, receipt["backup_id"])
    with service.db.transaction(False) as s:
        restored = get(s, "suppressions", original_id)
        assert restored.data["active"] is True
        assert restored.data["permanent"] is True
        assert restored.data["reason"] == "opt_out"
