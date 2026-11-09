"""November 9: Test call is one direct action that always reaches the rep's own phone, and an empty queue says why.

The owner pressed Test call with +12038211101 saved and got "No one left to call": that number already existed as an
archived lead, so it never joined the Test line list, and at 3 AM Eastern the calling window refused it anyway.
"""

from datetime import UTC, datetime

from conftest import ready
from helpers import finish

PHONE = "+12038211101"


def start(workspace):
    client, headers = workspace["client"], workspace["headers"]
    started = client.post("/v1/settings/test-call", headers=headers)
    assert started.status_code == 200, started.text
    ready(workspace)
    packet = client.post("/v1/queue/next", headers=headers, json={"campaign_id": workspace["campaign_id"]})
    return started.json(), packet


def save_phone(workspace, phone=PHONE):
    r = workspace["client"].put(
        "/v1/admin/settings", headers=workspace["headers"], json={"test_phone": phone}
    )
    assert r.status_code == 200, r.text


def test_a_test_call_reaches_the_rep_outside_calling_hours(workspace, clock):
    clock.now = datetime(
        2026, 10, 15, 1, 0, tzinfo=UTC
    )  # 9 PM Eastern, 6 PM Pacific: outside every calling window
    save_phone(workspace)
    started, packet = start(workspace)
    assert packet.status_code == 200, packet.text
    assert packet.json()["lead"]["id"] == started["lead_id"]
    assert packet.json()["lead"]["phone"] == PHONE


def test_a_test_call_revives_an_archived_lead_with_the_same_number(workspace, app):
    client, headers = workspace["client"], workspace["headers"]
    imported = client.post(
        "/v1/leads/import",
        headers=headers,
        json={
            "campaign_id": workspace["campaign_id"],
            "list_name": "Old tests",
            "rows": [{"name": "Owner delivery test", "phone": PHONE, "company": "Old"}],
            "prepare": False,
        },
    )
    assert imported.status_code == 200, imported.text
    with app.state.service.db.transaction(False) as s:
        from helpers import principal

        lead = next(
            m for m in app.state.service.scoped(s, "leads", principal(workspace)) if m.data["phone"] == PHONE
        )
    archived = client.post(
        "/v1/leads/bulk", headers=headers, json={"lead_ids": [lead.id], "action": "archive"}
    )
    assert archived.status_code == 200, archived.text
    save_phone(workspace)
    started, packet = start(workspace)
    assert packet.status_code == 200, packet.text
    assert packet.json()["lead"]["id"] == started["lead_id"] == lead.id


def test_saving_the_test_call_outcome_ends_the_test_session(workspace, app):
    client, headers = workspace["client"], workspace["headers"]
    save_phone(workspace)
    _, packet = start(workspace)
    call = client.post(
        "/v1/calls/start",
        headers=headers,
        json={"reservation_id": packet.json()["reservation_id"], "idempotency_key": "test-call-1"},
    ).json()
    assert call["is_test"] is True
    finish(workspace, call, "no_answer")
    assert app.state.service.in_test_session({"tenant_id": workspace["tenant_id"]}) is False


def test_a_test_session_without_a_test_phone_is_off(workspace, app):
    client, headers = workspace["client"], workspace["headers"]
    assert client.put("/v1/admin/settings", headers=headers, json={"test_session": True}).status_code == 409
    # An older workspace saved with the flag but no phone (the owner's, November 9) reads as off.
    app.state.service.configs.write(workspace["tenant_id"], {"test_session": True})
    assert app.state.service.in_test_session({"tenant_id": workspace["tenant_id"]}) is False


def test_an_empty_queue_says_why(workspace, clock):
    clock.now = datetime(2026, 10, 15, 1, 0, tzinfo=UTC)
    ready(workspace)
    r = workspace["client"].post(
        "/v1/queue/next", headers=workspace["headers"], json={"campaign_id": workspace["campaign_id"]}
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "NO_ELIGIBLE_LEADS"
    assert detail["message"].startswith("Everyone left is outside calling hours"), detail


def test_readiness_says_how_many_can_be_called_or_why_none(workspace, clock):
    client, headers = workspace["client"], workspace["headers"]
    now = client.get("/v1/calling/readiness", headers=headers).json()
    assert now["blockers"] == [] and now["callable_now"] > 0 and now["why_none"] is None
    clock.now = datetime(2026, 10, 15, 1, 0, tzinfo=UTC)
    later = client.get("/v1/calling/readiness", headers=headers).json()
    assert later["callable_now"] == 0
    assert later["why_none"].startswith("Everyone left is outside calling hours"), later
