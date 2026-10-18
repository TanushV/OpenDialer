"""A23/A50: rendering and pinning current guidance must not scan completed calls."""

from datetime import timedelta

import pytest
from app.db import put
from test_context_reads import count_decrypted
from test_live_memory import setup, turn


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["view", "pin"])
async def test_live_display_uses_only_the_current_call_and_its_guidance(
    workspace, app, clock, monkeypatch, operation
):
    call, p, _, _ = setup(workspace, app, monkeypatch)
    service = app.state.service
    turn(service, p, call, "display")
    current = await service.coach_turn(p, call["id"], urgent=True, question="What next?")
    with service.db.transaction() as s:
        for i in range(300):
            put(
                s,
                "conversation_states",
                p["tenant_id"],
                p["seller_id"],
                {"call_id": f"old-{i}"},
                key=f"old-{i}",
            )
            put(
                s,
                "coach_events",
                p["tenant_id"],
                p["seller_id"],
                {
                    "call_id": f"old-{i}",
                    "state_version": 1,
                    "expires_at": (clock.now - timedelta(days=1)).isoformat(),
                },
            )
    decoded = count_decrypted(app, monkeypatch)
    url = f"/v1/calls/{call['id']}"
    if operation == "pin":
        response = workspace["client"].patch(
            url + "/coach", headers=workspace["headers"], json={"pinned": True}
        )
        assert response.status_code == 200
        assert response.json()["pinned_event_id"] == current["id"]
    else:
        response = workspace["client"].get(url, headers=workspace["headers"])
        assert response.status_code == 200
        assert response.json()["coach"]["id"] == current["id"]
    assert len(decoded) <= 15, len(decoded)


@pytest.mark.asyncio
async def test_private_answer_takes_precedence_over_an_older_pinned_automatic_tip(
    workspace, app, monkeypatch
):
    call, p, _, changes = setup(workspace, app, monkeypatch)
    service, client, headers = app.state.service, workspace["client"], workspace["headers"]
    turn(service, p, call, "display")
    first = await service.coach_turn(p, call["id"])
    assert (
        client.patch(f"/v1/calls/{call['id']}/coach", headers=headers, json={"pinned": True}).status_code
        == 200
    )
    changes["result"] = {"say_or_ask": "Which decision needs clarification?"}
    latest = await service.coach_turn(p, call["id"], urgent=True, question="What about this decision?")
    assert latest["id"] != first["id"]
    visible = client.get(f"/v1/calls/{call['id']}", headers=headers).json()["coach"]
    assert visible["id"] == latest["id"]
