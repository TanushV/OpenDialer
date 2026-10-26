"""Calling-hour boundaries and removal of prerecorded voicemail drops."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from app.compliance.policy import CallContext, authorize
from journey import get, post, start


@pytest.mark.parametrize("timezone", ["America/New_York", "America/Los_Angeles"])
@pytest.mark.parametrize("day", ["2026-01-15", "2026-07-15"])
@pytest.mark.parametrize(
    "time,allowed", [("07:59", False), ("08:00", True), ("20:59", True), ("21:00", False)]
)
def test_default_calling_window_uses_prospect_timezone(timezone, day, time, allowed):
    now = datetime.fromisoformat(f"{day}T{time}").replace(tzinfo=ZoneInfo(timezone))
    context = CallContext(now=now.astimezone(UTC), timezone=timezone, timezone_confidence="confirmed")
    assert ("OUTSIDE_CALLING_WINDOW" not in authorize(context).reasons) is allowed


def test_new_workspace_hours(workspace):
    policy = get(workspace, "/v1/workspace")["policy"]
    assert (policy["local_start_hour"], policy["local_end_hour"]) == (8, 21)


@pytest.mark.parametrize("detach", [False, True])
def test_voicemail_drop_endpoint_removed(workspace, carrier, detach):
    started = start(workspace)
    post(
        workspace,
        f"/v1/calls/{started['id']}/voicemail",
        {"asset_id": "old-asset", "detach": detach},
        status=404,
    )
    assert not carrier.playbacks


@pytest.mark.parametrize("action", ["playback_start", "gather"])
def test_saved_drop_commands_cannot_play_after_update(workspace, app, carrier, action):
    from app.db import get as row, update

    started = start(workspace)
    service = app.state.service
    p = {key: workspace[key] for key in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}
    with app.state.db.transaction() as s:
        update(row(s, "call_attempts", started["id"]), state="VM_PENDING", voicemail_asset="old-asset")
        service.control_intent(s, p, started["id"], action, {"media_name": "old-asset"}, suffix="old-drop")
    post(workspace, "/v1/admin/control/run", {})
    assert not carrier.playbacks
    assert not any(item["action"] == "gather" for item in carrier.actions)
    assert get(workspace, f"/v1/calls/{started['id']}")["voicemail_result"] == "blocked_before_playback"
