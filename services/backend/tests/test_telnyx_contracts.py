"""Documented Telnyx payload shapes: credentials, per-leg speech, AMD, playback and status expiry.

Fixtures follow the published Telnyx OpenAPI schemas (TelephonyCredential, CredentialConnection,
Transcription, CallPlaybackEnded, call.machine.* events). They prove adapter behavior only; a real
account, SIP registration and carrier audio remain separate evidence.
"""

import asyncio
import base64
import json
from datetime import timedelta

import httpx
import pytest
from app.db import get, update
from app.providers.telnyx import CarrierConfigurationError, TelnyxCarrier
from app.telephony.recovery import COMMAND_RETRY_SECONDS, within_command_window
from app.telephony.reducer import reduce_state
from conftest import start
from helpers import voicemail_policy

CREDENTIAL = {
    "id": "c215ade3-0d39-418e-94be-c5f780760199",
    "record_type": "credential",
    "name": "rep credential",
    "resource_id": "connection:1234567890",
    "sip_username": "gencrednCvHU5IYpSBPPsXI2iQsDX",
    "expired": False,
    "user_id": "user-id",
}
CONNECTION = {
    "id": "1234567890",
    "record_type": "credential_connection",
    "sip_uri_calling_preference": "internal",
    "outbound": {"outbound_voice_profile_id": None},
}


def principal(w):
    return {k: w[k] for k in ("tenant_id", "seller_id", "user_id")} | {"role": "owner"}


def credential_transport(credential=None, connection=None, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        if request.url.path.startswith("/v2/telephony_credentials/"):
            return httpx.Response(200, json={"data": {**CREDENTIAL, **(credential or {})}})
        if request.url.path == "/v2/credential_connections/1234567890":
            return httpx.Response(200, json={"data": {**CONNECTION, **(connection or {})}})
        return httpx.Response(404, json={"errors": [{"code": "10005"}]})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_credential_connection_comes_from_documented_resource_id():
    seen = []
    async with httpx.AsyncClient(transport=credential_transport(seen=seen)) as http:
        carrier = TelnyxCarrier("secret", "call-control-app", http=http)
        assert await carrier.verify_incoming_only_credential(
            CREDENTIAL["id"], sip_username=CREDENTIAL["sip_username"]
        )
    assert [r.url.path for r in seen] == [
        f"/v2/telephony_credentials/{CREDENTIAL['id']}",
        "/v2/credential_connections/1234567890",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential,connection,needle",
    [
        ({"resource_id": None}, {}, "resource_id"),
        ({"resource_id": "fqdn_connection:1234567890"}, {}, "resource_id"),
        ({"resource_id": "connection:"}, {}, "resource_id"),
        ({"sip_username": "another-client"}, {}, "SIP username"),
        ({}, {"sip_uri_calling_preference": "disabled"}, "sip_uri_calling_preference"),
        ({}, {"sip_uri_calling_preference": None}, "sip_uri_calling_preference"),
        ({}, {"sip_uri_calling_preference": "unrestricted"}, "sip_uri_calling_preference"),
        ({}, {"outbound": {"outbound_voice_profile_id": "1293384261075731499"}}, "outbound"),
        ({"expired": True}, {}, "expired"),
    ],
)
async def test_unusable_credential_is_an_actionable_configuration_error(credential, connection, needle):
    async with httpx.AsyncClient(transport=credential_transport(credential, connection)) as http:
        carrier = TelnyxCarrier("secret", "call-control-app", http=http)
        with pytest.raises(CarrierConfigurationError) as error:
            await carrier.verify_incoming_only_credential(
                CREDENTIAL["id"], sip_username=CREDENTIAL["sip_username"]
            )
    assert needle in str(error.value)


@pytest.mark.asyncio
async def test_token_request_accepts_the_documented_plain_text_jwt():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            201, text="eyJhbGciOiJIUzUxMiJ9.e30.sig", headers={"content-type": "text/plain"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        token = await TelnyxCarrier("master", "app", http=http).token(CREDENTIAL["id"])
    assert token == "eyJhbGciOiJIUzUxMiJ9.e30.sig"
    assert seen[0].method == "POST" and seen[0].headers["accept"] == "*/*"
    assert seen[0].url.path == f"/v2/telephony_credentials/{CREDENTIAL['id']}/token"


def live_agent(service, carrier, monkeypatch, p, *, error=None):
    service.settings.simulation = False
    monkeypatch.setattr(service, "carrier", lambda _: carrier)
    service.configs.write(
        p["tenant_id"], {"sip_username": "rep-browser", "telephony_credential_id": "cred-1"}
    )
    checked = []

    async def verify(credential_id, *, sip_username=None):
        checked.append((credential_id, sip_username))
        if error:
            raise CarrierConfigurationError(error)
        return True

    monkeypatch.setattr(carrier, "verify_incoming_only_credential", verify, raising=False)
    return checked


@pytest.mark.asyncio
async def test_agent_leg_dials_the_verified_credential_without_an_undocumented_uri_suffix(
    workspace, app, carrier, monkeypatch
):
    service, p = app.state.service, principal(workspace)
    checked = live_agent(service, carrier, monkeypatch, p)
    await service.create_agent(p)
    assert checked == [("cred-1", "rep-browser")]
    assert carrier.dials[-1]["to"] == "sip:rep-browser@sip.telnyx.com"


@pytest.mark.parametrize("route", ["/v1/telephony/agent", "/v1/telephony/token"])
def test_misconfigured_credential_returns_an_actionable_setup_error(
    workspace, app, carrier, monkeypatch, route
):
    service, p = app.state.service, principal(workspace)
    live_agent(service, carrier, monkeypatch, p, error="Set sip_uri_calling_preference to internal")
    response = workspace["client"].post(route, headers=workspace["headers"])
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "TELEPHONY_CREDENTIAL_MISCONFIGURED"
    assert "sip_uri_calling_preference" in detail["message"]
    assert not carrier.dials


def event(w, call, name, payload=None):
    from uuid import uuid4

    return w["client"].post(
        "/v1/simulation/event",
        headers=w["headers"],
        json={"event_id": str(uuid4()), "event_type": name, "call_id": call["id"], "payload": payload or {}},
    )


def run_controls(w):
    w["client"].post("/v1/admin/control/run", headers=w["headers"])


def transcription_starts(carrier):
    return [a for a in carrier.actions if a["action"] == "transcription_start"]


def test_answer_requests_one_inbound_stream_per_leg(workspace, carrier):
    call = start(workspace)
    event(workspace, call, "call.answered")
    run_controls(workspace)
    starts = transcription_starts(carrier)
    assert sorted(a["provider_id"] for a in starts) == sorted([call["provider_id"], "agent-test-id"])
    assert {a["payload"]["transcription_tracks"] for a in starts} == {"inbound"}
    assert {a["payload"]["transcription_engine"] for a in starts} == {"Soniox"}
    assert len({a["payload"]["command_id"] for a in starts}) == 2


def speech(w, app, call_control_id, text, event_id, **payload):
    service = app.state.service
    receipt = asyncio.run(
        service.accept_webhook(
            w["tenant_id"],
            {
                "id": event_id,
                "event_type": "call.transcription",
                "payload": {
                    "call_control_id": call_control_id,
                    "transcription_data": {"transcript": text, "is_final": True, "confidence": 0.9},
                    **payload,
                },
            },
            "hash-" + event_id,
            verified=True,
        )
    )
    return asyncio.run(service.process_webhook(w["tenant_id"], receipt["receipt_id"]))


def sides(app, w, call):
    service = app.state.service
    with service.db.transaction(False) as s:
        return {row["text"]: row["side"] for row in service.transcript_rows(s, principal(w), call["id"])}


def test_soniox_speech_side_comes_from_the_leg_that_carried_it(workspace, app, carrier):
    call = start(workspace)
    event(workspace, call, "call.answered")
    run_controls(workspace)
    agent_start = next(a for a in transcription_starts(carrier) if a["provider_id"] == "agent-test-id")
    prospect = speech(workspace, app, call["provider_id"], "Who is calling?", "prospect-speech")
    rep = speech(
        workspace,
        app,
        "agent-test-id",
        "This is the example rep.",
        "rep-speech",
        client_state=agent_start["payload"]["client_state"],
    )
    assert prospect["status"] == "processed" and rep["status"] == "processed", (prospect, rep)
    assert sides(app, workspace, call) == {"Who is calling?": "remote", "This is the example rep.": "rep"}


def test_agent_speech_after_the_call_ended_is_not_attributed_to_it(workspace, app, carrier, clock):
    call = start(workspace)
    event(workspace, call, "call.answered")
    run_controls(workspace)
    agent_start = next(a for a in transcription_starts(carrier) if a["provider_id"] == "agent-test-id")
    workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    clock.now += timedelta(seconds=30)
    speech(
        workspace,
        app,
        "agent-test-id",
        "Talking to a colleague after the call.",
        "late-rep-speech",
        client_state=agent_start["payload"]["client_state"],
    )
    assert sides(app, workspace, call) == {}


def test_hangup_stops_the_agent_leg_transcription(workspace, carrier):
    call = start(workspace)
    event(workspace, call, "call.answered")
    run_controls(workspace)
    workspace["client"].post(f"/v1/calls/{call['id']}/hangup", headers=workspace["headers"])
    run_controls(workspace)
    stops = [a for a in carrier.actions if a["action"] == "transcription_stop"]
    assert [a["provider_id"] for a in stops] == ["agent-test-id"]


def test_revoked_transcription_permission_stops_both_streams(workspace, app, carrier):
    call = start(workspace)
    event(workspace, call, "call.answered")
    run_controls(workspace)
    service, p = app.state.service, principal(workspace)
    from app.compliance.consent import revoke_effects

    with service.db.transaction() as s:
        revoke_effects(service, s, p, call["endpoint_id"], ["transcription"])
    run_controls(workspace)
    stops = {a["provider_id"] for a in carrier.actions if a["action"] == "transcription_stop"}
    assert stops == {call["provider_id"], "agent-test-id"}


def test_final_permitted_attempt_requests_beep_detection_without_delaying_bridge(workspace, app, carrier):
    voicemail_policy(app, workspace, 1)
    start(workspace)
    dial = carrier.dials[-1]
    assert dial["answering_machine_detection"] == "detect_beep"
    assert dial["bridge_on_answer"] is True and dial["link_to"] == "agent-test-id"


@pytest.mark.parametrize(
    "change", [{"max_attempts": 4}, {"voicemail_enabled": False}, {"approved_assets": []}]
)
def test_ordinary_attempts_never_pay_for_machine_detection(workspace, app, carrier, change):
    voicemail_policy(app, workspace, 1)
    with app.state.db.transaction() as s:
        update(get(s, "policy_versions", workspace["policy_id"]), **change)
    start(workspace)
    assert "answering_machine_detection" not in carrier.dials[-1]


@pytest.mark.parametrize(
    "state,name,expected",
    [
        ("ANSWERED_UNCLASSIFIED", "call.machine.detection.ended:human", "HUMAN"),
        ("ANSWERED_UNCLASSIFIED", "call.machine.detection.ended:not_sure", "ANSWERED_UNCLASSIFIED"),
        ("VOICEMAIL_GREETING", "call.machine.greeting.ended:not_sure", "VOICEMAIL_GREETING"),
        ("VOICEMAIL_GREETING", "call.machine.greeting.ended:ended", "VOICEMAIL_GREETING"),
        ("ANSWERED_UNCLASSIFIED", "call.machine.premium.detection.ended:human_business", "HUMAN"),
        ("ANSWERED_UNCLASSIFIED", "call.machine.premium.detection.ended:machine", "VOICEMAIL_GREETING"),
        ("ANSWERED_UNCLASSIFIED", "call.machine.premium.detection.ended:silence", "ANSWERED_UNCLASSIFIED"),
        (
            "ANSWERED_UNCLASSIFIED",
            "call.machine.premium.detection.ended:fax_detected",
            "ANSWERED_UNCLASSIFIED",
        ),
        ("ANSWERED_UNCLASSIFIED", "call.machine.premium.detection.ended:screening", "ANSWERED_UNCLASSIFIED"),
        ("ANSWERED_UNCLASSIFIED", "call.machine.premium.call_screening.detected:screening", "SCREENING"),
        ("VOICEMAIL_GREETING", "call.machine.premium.greeting.ended:beep_detected", "VOICEMAIL_READY"),
        ("VOICEMAIL_GREETING", "call.machine.premium.greeting.ended:no_beep_detected", "VOICEMAIL_GREETING"),
        ("VM_PLAYING", "call.playback.failed", "VM_PLAYING"),
    ],
)
def test_reducer_maps_only_documented_machine_detection_results(state, name, expected):
    assert reduce_state(state, name) == expected


def test_premium_human_result_recovers_a_detached_voicemail(workspace, app, carrier):
    call = start(workspace)
    with app.state.db.transaction() as s:
        update(get(s, "call_attempts", call["id"]), detached=True, state="VM_PLAYING")
    event(workspace, call, "call.machine.premium.detection.ended:human_residence")
    run_controls(workspace)
    assert any(a["action"] == "playback_stop" for a in carrier.actions)


@pytest.mark.parametrize(
    "status,result",
    [
        ("completed", "playback_completed_not_mailbox_receipt"),
        ("failed", "playback_failed"),
        ("file_not_found", "playback_failed"),
        ("call_hangup", "playback_failed"),
    ],
)
def test_playback_outcome_comes_from_the_documented_ended_status(workspace, app, status, result):
    call = start(workspace)
    event(workspace, call, "call.playback.ended", {"status": status})
    with app.state.db.transaction(False) as s:
        saved = get(s, "call_attempts", call["id"]).data
    assert saved["voicemail_result"] == result
    assert saved["voicemail_playback_status"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [httpx.Response(404, json={}), httpx.Response(422, json={})])
async def test_expired_call_status_is_reported_as_no_live_leg(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as http:
        result = await TelnyxCarrier("secret", "app", http=http).reconcile("call-id", "v3:ended-leg")
    assert result == {"call_control_id": "v3:ended-leg", "is_alive": False, "status_expired": True}


@pytest.mark.asyncio
async def test_transient_status_failure_is_not_mistaken_for_expiry():
    transport = httpx.MockTransport(lambda r: httpx.Response(503, json={}))
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await TelnyxCarrier("secret", "app", http=http).reconcile("call-id", "v3:leg")


def test_expired_status_for_an_ending_call_finishes_into_review(workspace, app, carrier, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    call = start(workspace)
    with app.state.db.transaction() as s:
        update(get(s, "call_attempts", call["id"]), state="ENDING")
    reads = []

    async def expired(command_id, provider_id=None):
        reads.append(provider_id)
        return {"call_control_id": provider_id, "is_alive": False, "status_expired": True}

    monkeypatch.setattr(carrier, "reconcile", expired)
    clock.now += timedelta(minutes=15)
    asyncio.run(service.recover(p))
    asyncio.run(service.recover(p))
    with app.state.db.transaction(False) as s:
        saved = get(s, "call_attempts", call["id"]).data
    assert saved["state"] == "ENDED" and saved["end_reason"] == "carrier_status_expired"
    assert saved["review_required"] is True and "statement" in saved["recovery_reason"]
    assert reads == [call["provider_id"]], "A terminal review outcome leaves the 30-second loop"


def test_command_retry_window_stays_inside_the_documented_dedupe_window(clock):
    assert 0 < COMMAND_RETRY_SECONDS < 60
    sent = clock.now
    assert within_command_window(sent, sent + timedelta(seconds=COMMAND_RETRY_SECONDS - 1))
    assert not within_command_window(sent, sent + timedelta(seconds=COMMAND_RETRY_SECONDS))
    assert not within_command_window(sent, sent + timedelta(seconds=60))


PHONE = "+14155550101"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,reason,definitive",
    [
        (401, "DNC_PROVIDER_KEY_INVALID", True),
        (403, "DNC_PROVIDER_CREDITS_EXHAUSTED", True),
        (400, "DNC_PHONE_REJECTED", True),
        (429, "DNC_SCREENING_UNAVAILABLE", False),
        (500, "DNC_SCREENING_UNAVAILABLE", False),
    ],
)
async def test_prepaid_provider_refusals_have_distinct_definitive_reasons(clock, status, reason, definitive):
    from app.providers.dnc import DNCError, PrepaidDNC

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={"success": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DNCError) as error:
            await PrepaidDNC("test", http=http, clock=clock).check(PHONE)
    assert (error.value.reason, error.value.definitive) == (reason, definitive)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "reason,status,liability",
    [
        ("DNC_PROVIDER_KEY_INVALID", "failed", "released"),
        ("DNC_PROVIDER_CREDITS_EXHAUSTED", "failed", "released"),
        ("DNC_PHONE_REJECTED", "failed", "estimated"),
    ],
)
def test_definitive_prepaid_refusal_never_clears_and_needs_no_paid_retry(
    workspace, app, clock, monkeypatch, reason, status, liability
):
    from app.providers.dnc import DNCError, PrepaidDNC
    from helpers import screen

    service, p = app.state.service, principal(workspace)
    service.configs.write(
        p["tenant_id"],
        {
            "dnc_api_key": "synthetic-private-key",
            "dnc_seller_authorization": {
                "reference": "own-source-reference",
                "npas": ["415"],
                "seller_id": p["seller_id"],
                "source_access_verified": True,
                "source_refreshed_at": clock.now.isoformat(),
                "expires_at": (clock.now + timedelta(days=30)).isoformat(),
            },
        },
    )

    async def refused(provider, phone):
        raise DNCError("refused", reason=reason, definitive=True)

    monkeypatch.setattr(PrepaidDNC, "check", refused)
    response = screen(workspace)
    assert reason in response.text, response.text
    with service.db.transaction(False) as s:
        rows = [r.data for r in service.scoped(s, "registry_snapshots", p) if r.data.get("kind") == "prepaid"]
        holds = [
            r.data for r in service.scoped(s, "usage_ledger", p) if r.data.get("work_kind") == "dnc_screen"
        ]
    assert [(r["status"], r["listed"], r["recovery_reason"]) for r in rows] == [(status, None, reason)]
    assert [h["status"] for h in holds] == [liability]
    assert all(h.get("reserved_usd", 0) == 0 for h in holds)


def test_agent_leg_client_state_cannot_name_another_tenant_call(workspace, app, carrier):
    call = start(workspace)
    event(workspace, call, "call.answered")
    run_controls(workspace)
    forged = base64.b64encode(
        json.dumps({"transcript_call_id": call["id"], "tenant_id": "another-tenant"}).encode()
    ).decode()
    result = speech(workspace, app, "agent-test-id", "Forged speech.", "forged-speech", client_state=forged)
    assert result["status"] in {"unmatched", "processed"}
    assert sides(app, workspace, call) == {}
