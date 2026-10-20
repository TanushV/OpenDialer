"""Connected sign-out waits for the admin's own work and seller admin effects only."""

from app.db import put


def principal(workspace):
    from test_remediation_privacy import principal as p

    return {**p(workspace), "role": "owner"}


def outbox(service, p, status, **data):
    with service.db.transaction() as s:
        put(
            s,
            "command_outbox",
            p["tenant_id"],
            p["seller_id"],
            {"status": status, **data},
            now=service.clock(),
        )


def test_another_reps_unresolved_call_command_does_not_block_admin_sign_out(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    monkeypatch.setattr(service.settings, "mode", "connected")
    outbox(service, p, "sending", kind="dial", rep_id="another-rep", call_id="c-other")
    assert service_quiescence(service, p)["reasons"] == []


def test_seller_admin_effect_and_own_command_still_block(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    monkeypatch.setattr(service.settings, "mode", "connected")
    outbox(service, p, "uncertain", kind="number_configure")  # Seller-owned admin effect.
    assert "EXTERNAL_OPERATION_UNRESOLVED" in service_quiescence(service, p)["reasons"]


def test_own_unresolved_command_blocks(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    monkeypatch.setattr(service.settings, "mode", "connected")
    outbox(service, p, "reconciling", kind="hangup", rep_id=p["user_id"], call_id="c-own")
    assert "EXTERNAL_OPERATION_UNRESOLVED" in service_quiescence(service, p)["reasons"]


def service_quiescence(service, p):
    with service.db.transaction(False) as s:
        return service.quiescence_in(s, p)


def test_production_call_commands_belong_to_the_calls_rep(workspace, app, monkeypatch):
    """Hangup and call-action commands block their rep's sign-out, never another admin's."""
    from app.db import MODELS, get, update
    from conftest import start
    from sqlalchemy import select

    service, p = app.state.service, principal(workspace)
    monkeypatch.setattr(service.settings, "mode", "connected")
    call = start(workspace)
    service._claim_hangup(p, call["id"], "hangup-owner")  # Leaves the production hangup in "sending".
    with service.db.transaction() as s:
        stored = get(s, "call_attempts", call["id"])
        rep = stored.data["rep_id"]
        action = service.control_intent(
            s, p, call["id"], "transcription_stop", {}, target=stored.data.get("provider_id")
        )
        update(action, status="uncertain")
    with service.db.transaction(False) as s:
        owners = {
            row.op_kind: row.op_owner_id
            for row in s.scalars(
                select(MODELS["command_outbox"]).where(
                    MODELS["command_outbox"].op_kind.in_(["hangup", "action"])
                )
            )
        }
    assert owners == {"hangup": rep, "action": rep}
    assert service_quiescence(service, {**p, "user_id": "another-admin"})["reasons"] == []
    assert "EXTERNAL_OPERATION_UNRESOLVED" in service_quiescence(service, {**p, "user_id": rep})["reasons"]


def test_legacy_unowned_call_command_does_not_block_admin_sign_out(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    monkeypatch.setattr(service.settings, "mode", "connected")
    outbox(service, p, "uncertain", kind="hangup", call_id="c-legacy")  # Written before rep stamping.
    assert service_quiescence(service, p)["reasons"] == []
