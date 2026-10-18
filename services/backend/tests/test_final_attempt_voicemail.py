"""Final-attempt eligibility must survive policy changes and delayed dispatch."""

import asyncio

from app.db import update
from conftest import start
from test_remediation_rep_workflow import p


def voicemail_policy(app, w, maximum):
    with app.state.db.transaction() as s:
        update(
            app.state.service.required(s, "policy_versions", w["policy_id"], p(w)),
            max_attempts=maximum,
            permissions=["live", "prerecorded"],
            voicemail_enabled=True,
            voicemail_optout_supported=True,
            approved_assets=["vm"],
            detached_topology_verified=True,
            late_human_recovery_destination="sip:recovery@example.test",
        )


def mailbox(app, w, call):
    with app.state.db.transaction() as s:
        update(app.state.service.required(s, "call_attempts", call["id"], p(w)), state="VOICEMAIL_READY")


def test_reducing_attempt_policy_does_not_enable_early_voicemail(workspace, app, carrier):
    voicemail_policy(app, workspace, 4)
    call = start(workspace)
    voicemail_policy(app, workspace, 1)
    mailbox(app, workspace, call)
    response = workspace["client"].post(
        "/v1/calls/" + call["id"] + "/voicemail", headers=workspace["headers"], json={"asset_id": "vm"}
    )
    assert response.status_code == 403 and "FINAL_ATTEMPT" in response.text
    assert not carrier.playbacks


def test_first_call_is_final_only_when_original_policy_allows_one_attempt(workspace, app, carrier):
    voicemail_policy(app, workspace, 1)
    call = start(workspace)
    mailbox(app, workspace, call)
    response = workspace["client"].post(
        "/v1/calls/" + call["id"] + "/voicemail", headers=workspace["headers"], json={"asset_id": "vm"}
    )
    assert response.status_code == 200, response.text
    assert len(carrier.playbacks) == 1


def test_pending_playback_rechecks_final_attempt_after_policy_expansion(workspace, app, carrier, monkeypatch):
    voicemail_policy(app, workspace, 1)
    call = start(workspace)
    mailbox(app, workspace, call)
    real_dispatch = app.state.service.dispatch_controls

    async def postpone(*_):
        return None

    monkeypatch.setattr(app.state.service, "dispatch_controls", postpone)
    response = workspace["client"].post(
        "/v1/calls/" + call["id"] + "/voicemail", headers=workspace["headers"], json={"asset_id": "vm"}
    )
    assert response.status_code == 200
    voicemail_policy(app, workspace, 4)
    asyncio.run(real_dispatch(p(workspace)))
    assert not carrier.playbacks
    with app.state.db.transaction(False) as s:
        commands = [
            r.data
            for r in app.state.service.scoped(s, "command_outbox", p(workspace))
            if r.data.get("action") == "playback_start"
        ]
        assert commands[0]["status"] == "cancelled"


def test_pending_playback_rechecks_optout(workspace, app, carrier, monkeypatch):
    voicemail_policy(app, workspace, 1)
    call = start(workspace)
    mailbox(app, workspace, call)
    real_dispatch = app.state.service.dispatch_controls

    async def postpone(*_):
        return None

    monkeypatch.setattr(app.state.service, "dispatch_controls", postpone)
    workspace["client"].post(
        "/v1/calls/" + call["id"] + "/voicemail", headers=workspace["headers"], json={"asset_id": "vm"}
    )
    with app.state.db.transaction(False) as s:
        phone = app.state.service.required(s, "leads", call["lead_id"], p(workspace)).data["phone"]
    app.state.service.suppress(p(workspace), phone, "opt_out")
    asyncio.run(real_dispatch(p(workspace)))
    assert not carrier.playbacks
