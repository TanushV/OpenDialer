"""Readiness names one next step per real problem instead of one combined SIP message."""

import pytest
from app.platform.db import MODELS, update
from helpers import principal
from sqlalchemy import select


def preflight(ws):
    response = ws["client"].get("/v1/admin/preflight", headers=ws["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def set_numbers(app, **values):
    with app.state.db.transaction() as s:
        for number in s.scalars(select(MODELS["number_inventory"])):
            update(number, **values)


def test_no_numbers_is_one_add_a_number_step_without_dependent_noise(workspace, app):
    set_numbers(app, lifecycle="retired_callback_only")
    check = preflight(workspace)
    assert "NO_CALLER_NUMBER" in check["blockers"]
    for noise in ("NUMBER_UNHEALTHY", "NUMBER_NOT_AUTHORIZED", "REP_NOT_READY", "AUDIO_NOT_READY"):
        assert noise not in check["blockers"]
    for paperwork in ("SELLER_NOT_APPROVED", "POLICY_NOT_PUBLISHED", "CARRIER_TRAFFIC_APPROVAL_MISSING"):
        assert paperwork not in check["blockers"]


def test_a_number_without_its_route_asks_for_route_setup(workspace, app):
    set_numbers(app, lifecycle="identity_configured", identity_configured=False)
    assert "CALLER_NUMBER_ROUTE_REQUIRED" in preflight(workspace)["blockers"]


def test_a_ready_workspace_has_no_number_or_paperwork_blockers(workspace):
    check = preflight(workspace)
    assert check["ready_to_call"] is True, check["blockers"]


@pytest.mark.parametrize(
    "config,numbers,code",
    [
        ({}, {}, "AUDIO_SETUP_MISSING"),
        (
            {"sip_username": "rep-line", "telephony_credential_id": "cred"},
            {"lifecycle": "retired_callback_only"},
            "NO_CALLER_NUMBER",
        ),
        (
            {"sip_username": "rep-line", "telephony_credential_id": "cred"},
            {"identity_configured": False},
            "CALLER_NUMBER_ROUTE_REQUIRED",
        ),
    ],
)
def test_going_ready_names_the_missing_piece(workspace, app, config, numbers, code):
    service = app.state.service
    service.settings.simulation = False
    service.configs.write(workspace["tenant_id"], config)
    if numbers:
        set_numbers(app, **numbers)
    response = workspace["client"].post("/v1/telephony/agent", headers=workspace["headers"])
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code and detail["message"]
    assert "SIP credential and an owned healthy caller ID" not in detail["message"]
    assert principal(workspace)
