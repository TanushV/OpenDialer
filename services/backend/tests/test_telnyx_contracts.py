"""Documented Telnyx payload shapes: credentials, per-leg speech, AMD, playback and status expiry.

Fixtures follow the published Telnyx OpenAPI schemas (TelephonyCredential, CredentialConnection,
Transcription, CallPlaybackEnded, call.machine.* events). They prove adapter behavior only; a real
account, SIP registration and carrier audio remain separate evidence.
"""

import asyncio
import base64
import json
from datetime import timedelta
from time import monotonic
from types import SimpleNamespace

import httpx
import pytest
from app import ops
from app.calling import audio_leg
from app.calling.state import transition
from app.platform.db import get, put, update
from app.providers.telnyx import CarrierConfigurationError, TelnyxCarrier
from app.telephony.recovery import COMMAND_RETRY_SECONDS, within_command_window
from conftest import start
from helpers import bound, voicemail_policy

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
    monkeypatch.setattr(ops, "carrier", bound(lambda _: carrier))
    monkeypatch.setattr(carrier, "instant_agent_legs", False, raising=False)
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
    await audio_leg.create(service.ctx, p)
    assert checked == [("cred-1", "rep-browser")]
    assert carrier.dials[-1]["to"] == "sip:rep-browser@sip.telnyx.com"


@pytest.mark.asyncio
async def test_the_audio_credential_is_checked_once_per_session_not_per_leg(
    workspace, app, carrier, monkeypatch
):
    """R4.4: each call gets a fresh leg; only the first leg of the session pays for a credential check."""
    service, p = app.state.service, principal(workspace)
    checked = live_agent(service, carrier, monkeypatch, p)
    first = await audio_leg.create(service.ctx, p)
    with service.db.transaction() as s:
        audio_leg.on_event(
            s, p["tenant_id"], {"call_control_id": first["agent_call_control_id"]}, "call.hangup"
        )
    second = await audio_leg.create(service.ctx, p)
    assert first != second and len(carrier.dials) >= 2
    assert checked == [("cred-1", "rep-browser")]


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


@pytest.mark.parametrize(
    "mode", ["pooled-at-cap", "paused-pool", "offline-pool", "standalone", "shared-app", "lease-lost"]
)
def test_fresh_rep_audio_uses_the_ready_relay_route(client, app, monkeypatch, mode):
    created = client.post(
        "/v1/setup",
        headers={"X-Local-Token": "test-local-secret"},
        json={"seller_name": "Fresh rep", "campaign_name": "Test"},
    )
    assert created.status_code == 200, created.text
    profile = created.json()
    scope = profile["tenant_id"]
    service = app.state.service
    service.settings.simulation = False
    service.configs.write(
        scope,
        {
            "telnyx_api_key": "fixture",
            "telnyx_connection_id": "call-control-app",
            "sip_username": CREDENTIAL["sip_username"],
            "telephony_credential_id": CREDENTIAL["id"],
        },
    )
    phone = "+12025550101"
    intents = []
    if mode == "standalone":
        with service.db.transaction() as s:
            put(
                s,
                "number_inventory",
                scope,
                profile["seller_id"],
                {"phone": phone, "lifecycle": "healthy", "identity_configured": True},
                key=phone,
                now=service.clock(),
            )
    else:
        managed = service.managed
        managed.config = {"tenant_id": scope, "workspace_id": "fixture", "store_id": "test-store"}
        managed.last_exchange = monotonic()
        managed.caught_up = mode != "offline-pool"
        managed.apply_pool_snapshot(
            scope,
            {
                "id": "shared",
                "segment_id": "14",
                "shared_connection_id": "call-control-app" if mode in {"shared-app", "lease-lost"} else None,
                "numbers": [
                    {"phone": phone, "daily_limit": 90, "used": 90, "outbound_enabled": mode != "paused-pool"}
                ],
            },
        )

        def rpc(name, parameters):
            async def execute():
                return SimpleNamespace(
                    data={"reason": "unavailable"} if mode == "lease-lost" else {"submitted": True}
                )

            intents.append((name, parameters))
            return SimpleNamespace(execute=execute)

        monkeypatch.setattr(managed, "client", SimpleNamespace(rpc=rpc))
    if mode == "shared-app":
        # Re-importing a managed profile over an existing rep must replace its old audio identity.
        with service.db.transaction() as s:
            user = get(s, "users", profile["user_id"], scope)
            update(user, sip_username="previous-rep", telephony_credential_id="previous-credential")
    seen = []
    checks = credential_transport()

    def http(request):
        seen.append(request)
        if request.url.path == "/v2/calls":
            if mode in {"shared-app", "lease-lost"}:
                name, intent = intents[0]
                body = json.loads(request.content)
                assert name == "relay_pool_agent"
                assert intent == {
                    "agent_ref": body["command_id"],
                    "number": phone,
                    "destination": body["to"],
                    "store": "test-store",
                    "installation": managed.installation,
                }
            return httpx.Response(200, json={"data": {"call_control_id": "v3:agent"}})
        return checks.handle_request(request)

    monkeypatch.setattr(
        service, "http", httpx.AsyncClient(transport=httpx.MockTransport(http)), raising=False
    )
    response = client.post(
        "/v1/telephony/agent", headers={"Authorization": f"Bearer {profile['access_token']}"}
    )
    if mode == "lease-lost":
        assert response.status_code == 409, response.text
        assert [req.url.path for req in seen] == [
            f"/v2/telephony_credentials/{CREDENTIAL['id']}",
            "/v2/credential_connections/1234567890",
        ]
        assert len(intents) == 1
    elif mode in {"paused-pool", "offline-pool"}:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == (
            "CALLER_NUMBER_PAUSED" if mode == "paused-pool" else "CONNECTION_DELIVERY_GAP"
        )
        assert not seen
    else:
        assert response.status_code == 200, response.text
        assert [r.url.path for r in seen] == [
            f"/v2/telephony_credentials/{CREDENTIAL['id']}",
            "/v2/credential_connections/1234567890",
            "/v2/calls",
        ]
        dial = json.loads(seen[-1].content)
        assert (dial["from"], dial["to"], dial["connection_id"]) == (
            phone,
            f"sip:{CREDENTIAL['sip_username']}@sip.telnyx.com",
            "call-control-app",
        )


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
        ops.accept_webhook(
            service,
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
    return asyncio.run(ops.process_webhook(service, w["tenant_id"], receipt["receipt_id"]))


def sides(app, w, call):
    service = app.state.service
    with service.db.transaction(False) as s:
        return {row["text"]: row["side"] for row in ops.transcript_rows(service, s, principal(w), call["id"])}


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
    from app.compliance.suppressions import revoke_effects

    with service.db.transaction() as s:
        revoke_effects(service, s, p, call["endpoint_id"], ["transcription"])
    run_controls(workspace)
    stops = {a["provider_id"] for a in carrier.actions if a["action"] == "transcription_stop"}
    assert stops == {call["provider_id"], "agent-test-id"}


def test_every_attempt_requests_voicemail_detection_and_bridges_the_rep(workspace, app, carrier):
    voicemail_policy(app, workspace, 1)
    start(workspace)
    dial = carrier.dials[-1]
    assert dial["answering_machine_detection"] == "premium"
    assert dial["timeout_secs"] == 40
    assert dial["bridge_on_answer"] is True and dial["link_to"] == "agent-test-id"


def test_a_machine_result_alone_never_hangs_up(workspace, app, carrier):
    call = start(workspace)
    event(workspace, call, "call.answered")
    event(workspace, call, "call.machine.premium.detection.ended:machine")
    run_controls(workspace)
    assert carrier.hangups == []
    with app.state.db.transaction(False) as s:
        saved = get(s, "call_attempts", call["id"]).data
    assert saved["machine_detected"] is True and not saved.get("voicemail_turn")


def test_a_detected_voicemail_ends_an_ordinary_attempt_at_seven_seconds(workspace, carrier, clock):
    """D6, R6.3: skipped at detection, but never sooner than 7 seconds after the answer."""
    from datetime import timedelta

    call = start(workspace)
    event(workspace, call, "call.answered")
    event(workspace, call, "call.machine.premium.detection.ended:machine")
    clock.now += timedelta(seconds=3)
    run_controls(workspace)
    assert carrier.hangups == []
    clock.now += timedelta(seconds=5)
    run_controls(workspace)
    assert [h["provider_id"] for h in carrier.hangups] == [call["provider_id"]]


def test_a_rep_can_stay_before_the_beep(workspace, carrier):
    call = start(workspace)
    event(workspace, call, "call.answered")
    response = workspace["client"].post(f"/v1/calls/{call['id']}/stay", headers=workspace["headers"])
    assert response.status_code == 200
    event(workspace, call, "call.machine.premium.greeting.ended:beep_detected")
    run_controls(workspace)
    assert carrier.hangups == []


def test_outbound_ring_time_can_be_changed(workspace, carrier):
    response = workspace["client"].put(
        "/v1/admin/settings", headers=workspace["headers"], json={"outbound_ring_seconds": 45}
    )
    assert response.status_code == 200
    start(workspace)
    assert carrier.dials[-1]["timeout_secs"] == 45


def test_a_late_beep_ends_the_call_at_once(workspace, carrier, clock):
    from datetime import timedelta

    call = start(workspace)
    event(workspace, call, "call.answered")
    event(workspace, call, "call.machine.premium.detection.ended:machine")
    clock.now += timedelta(seconds=12)
    event(workspace, call, "call.machine.premium.greeting.ended:beep_detected")
    run_controls(workspace)
    assert [h["provider_id"] for h in carrier.hangups] == [call["provider_id"]]


def test_the_final_attempt_stays_on_the_line_to_leave_a_voicemail(workspace, app, carrier, clock):
    from datetime import timedelta

    from app.platform.db import MODELS
    from sqlalchemy import select

    with app.state.db.transaction() as s:
        for lead in s.scalars(select(MODELS["leads"])):
            update(lead, attempt_count=3)
    call = start(workspace)
    event(workspace, call, "call.answered")
    event(workspace, call, "call.machine.premium.detection.ended:machine")
    clock.now += timedelta(seconds=12)
    event(workspace, call, "call.machine.premium.greeting.ended:beep_detected")
    run_controls(workspace)
    assert carrier.hangups == []
    with app.state.db.transaction(False) as s:
        saved = get(s, "call_attempts", call["id"]).data
    assert saved["voicemail_turn"] is True and saved["attempt_ordinal"] == 4


@pytest.mark.parametrize(
    "name",
    [
        "call.machine.premium.call_screening.detected:screening",
        "call.machine.premium.greeting.ended:prompt_ended",
    ],
)
@pytest.mark.slow
def test_call_screening_withdraws_a_pending_voicemail_skip(workspace, carrier, clock, name):
    from datetime import timedelta

    call = start(workspace)
    event(workspace, call, "call.answered")
    event(workspace, call, "call.machine.premium.detection.ended:machine")
    event(workspace, call, name)
    clock.now += timedelta(seconds=60)
    run_controls(workspace)
    assert carrier.hangups == []


def test_screening_is_not_counted_as_voicemail(workspace, app, carrier):
    call = start(workspace)
    event(workspace, call, "call.answered")
    event(workspace, call, "call.machine.premium.detection.ended:machine")
    event(workspace, call, "call.machine.premium.call_screening.detected:screening")
    with app.state.db.transaction(False) as s:
        saved = get(s, "call_attempts", call["id"]).data
    assert saved["screening_detected"] is True and saved["machine_detected"] is False


@pytest.mark.slow
def test_a_human_answer_is_never_hung_up_by_a_late_machine_result(workspace, app, carrier):
    call = start(workspace)
    event(workspace, call, "call.answered")
    event(workspace, call, "call.machine.detection.ended:human")
    event(workspace, call, "call.machine.detection.ended:machine")
    event(workspace, call, "call.machine.greeting.ended:beep_detected")
    run_controls(workspace)
    assert carrier.hangups == []


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
    ],
)
def test_reducer_maps_only_documented_machine_detection_results(state, name, expected):
    assert transition(state, name) == expected


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
    asyncio.run(ops.recover(service, p))
    asyncio.run(ops.recover(service, p))
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
        rows = [
            r.data for r in ops.scoped(service, s, "registry_snapshots", p) if r.data.get("kind") == "prepaid"
        ]
        holds = [
            r.data
            for r in ops.scoped(service, s, "usage_ledger", p)
            if r.data.get("work_kind") == "dnc_screen"
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
    # State naming another tenant is not ours: ignored at once, never attached and never retried.
    assert result["status"] == "ignored"
    assert sides(app, workspace, call) == {}


@pytest.mark.asyncio
async def test_inventory_reads_every_page_before_returning_numbers():
    seen = []

    def handler(request):
        if request.url.path.endswith("/voice"):
            return httpx.Response(
                200, json={"data": {"call_recording": {"inbound_call_recording_enabled": False}}}
            )
        page = int(request.url.params["page[number]"])
        seen.append(page)
        assert request.url.params["page[size]"] == "250"
        return httpx.Response(
            200,
            json={
                "data": [{"id": f"number-{page}", "phone_number": f"+1415555010{page}"}],
                "meta": {"page_number": page, "total_pages": 2},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await TelnyxCarrier("test", "app", http=http).list_numbers()
    assert seen == [1, 2]
    assert all(item["call_recording_enabled"] is False for item in result)
    assert [item["id"] for item in result] == ["number-1", "number-2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second",
    [
        {"data": [], "meta": {"page_number": 2, "total_pages": 3}},
        {"data": [], "meta": {"page_number": 1, "total_pages": 2}},
        {"data": [], "meta": {"page_number": 2, "total_pages": "2"}},
        {"data": [], "meta": {"page_number": 2, "total_pages": 101}},
        {"data": [], "errors": [{"code": "test"}], "meta": {"page_number": 2, "total_pages": 2}},
        {"data": []},
    ],
)
async def test_incomplete_inventory_never_returns_a_partial_success(second):
    def handler(request):
        if request.url.params["page[number]"] == "1":
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "number-1"}],
                    "meta": {"page_number": 1, "total_pages": 2},
                },
            )
        return httpx.Response(200, json=second)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError, match="inventory"):
            await TelnyxCarrier("test", "app", http=http).list_numbers()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False, None])
async def test_number_recording_comes_from_voice_settings(enabled):
    def handler(request):
        if request.url.path.endswith("/voice"):
            return httpx.Response(
                200, json={"data": {"call_recording": {"inbound_call_recording_enabled": enabled}}}
            )
        return httpx.Response(200, json={"data": {"id": "number-1", "call_recording_enabled": False}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await TelnyxCarrier("test", "app", http=http).get_number("number-1")
    assert result["data"]["call_recording_enabled"] is enabled


@pytest.mark.asyncio
async def test_an_area_code_without_stock_is_an_empty_search_not_an_error():
    # Recorded from the live API on November 2, 2026 for area code 415.
    sold_out = {
        "errors": [
            {
                "code": "10031",
                "title": "Invalid request filter",
                "detail": "No numbers found for the given filters. Please try again with best_effort=true.",
            }
        ]
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(400, json=sold_out))
    ) as http:
        assert await TelnyxCarrier("key", "app", http=http).search_numbers("415") == []
    asked = []

    def nearby(request):
        asked.append(dict(request.url.params))
        return httpx.Response(200, json={"data": [{"phone_number": "+16285611507"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(nearby)) as http:
        found = await TelnyxCarrier("key", "app", http=http).search_numbers("415")
    assert [n["phone_number"] for n in found] == ["+16285611507"]
    # Telnyx only offers numbers near a sold-out area code when asked for best effort.
    assert asked[0]["filter[national_destination_code]"] == "415"
    assert asked[0]["filter[best_effort]"] == "true"
    other = {"errors": [{"code": "10015", "title": "Bad request"}]}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(400, json=other))
    ) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await TelnyxCarrier("key", "app", http=http).search_numbers("415")


def test_number_search_can_filter_by_city_in_the_area_codes_state():
    from urllib.parse import parse_qs, urlsplit

    from app.providers.telnyx import TelnyxCarrier

    seen = []

    def handler(request):
        seen.append(parse_qs(urlsplit(str(request.url)).query))
        return httpx.Response(200, json={"data": []})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            carrier = TelnyxCarrier("synthetic", "route", http=http)
            await carrier.search_numbers("628", city="San Francisco")
            await carrier.search_numbers("929", limit=250)

    asyncio.run(run())
    assert seen[0]["filter[locality]"] == ["San Francisco"]
    assert seen[0]["filter[administrative_area]"] == ["CA"]
    assert seen[0]["filter[limit]"] == ["10"]
    assert "filter[locality]" not in seen[1] and seen[1]["filter[limit]"] == ["250"]


def test_the_daily_spend_cap_is_set_on_the_apps_voice_profile():
    from app.providers.telnyx import TelnyxCarrier

    patched = []

    def handler(request):
        if request.method == "GET" and request.url.path == "/v2/call_control_applications/route":
            return httpx.Response(200, json={"data": {"outbound": {"outbound_voice_profile_id": "ovp"}}})
        if request.method == "GET" and request.url.path == "/v2/outbound_voice_profiles/ovp":
            return httpx.Response(200, json={"data": {"id": "ovp", "name": "Default"}})
        assert request.method == "PATCH" and request.url.path == "/v2/outbound_voice_profiles/ovp"
        patched.append(json.loads(request.content))
        return httpx.Response(200, json={"data": {"id": "ovp"}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await TelnyxCarrier("synthetic", "route", http=http).set_daily_spend_limit(25)

    asyncio.run(run())
    assert patched == [{"name": "Default", "daily_spend_limit": "25.00", "daily_spend_limit_enabled": True}]


def test_a_blank_caller_name_turns_the_listing_off():
    from app.providers.telnyx import TelnyxCarrier

    patched = []

    def handler(request):
        if request.method == "PATCH":
            patched.append(json.loads(request.content))
        return httpx.Response(200, json={"data": {"cnam_listing": {"cnam_listing_enabled": False}}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await TelnyxCarrier("synthetic", "route", http=http).list_caller_name("number", None)

    asyncio.run(run())
    assert patched == [{"cnam_listing": {"cnam_listing_enabled": False}}]


def test_repeated_distinct_carrier_blocks_pause_outbound_and_keep_the_attempt(workspace, app):
    call = start(workspace)
    event(workspace, call, "call.hangup", {"sip_hangup_cause": "603+"})
    event(workspace, call, "call.hangup", {"sip_hangup_cause": "603+"})
    with app.state.db.transaction() as s:
        saved = get(s, "call_attempts", call["id"])
        assert saved.data["sip_block_code"] == "603+"
        assert saved.data["attempt_ordinal"] == 1
        number = get(s, "number_inventory", saved.data["number_id"])
        assert number.data["lifecycle"] != "paused_for_outbound"
        assert number.data["recent_sip_blocks"] == 1
        # Seed one earlier distinct incident to exercise the same-number threshold.
        update(saved, sip_block_code=None)
    event(workspace, call, "call.hangup", {"sip_hangup_cause": "603"})
    with app.state.db.transaction(False) as s:
        number = get(s, "number_inventory", call["number_id"])
        assert number.data["lifecycle"] == "paused_for_outbound"
        assert number.data["callback_retained"] is True


@pytest.mark.asyncio
async def test_a_leg_left_answered_by_an_earlier_run_is_never_reused(workspace, app, carrier, monkeypatch):
    """Live check, November 10: three calls were tied to an audio leg from November 6 that the store still called
    answered, so no audio reached the rep and no transcript started. A leg is reused only when this run of the app
    made it and the carrier says it is still live; anything else is ended and a fresh leg is dialed."""
    service, p = app.state.service, principal(workspace)
    live_agent(service, carrier, monkeypatch, p)
    first = await audio_leg.create(service.ctx, p)
    with service.db.transaction() as s:
        audio_leg.on_event(
            s, p["tenant_id"], {"call_control_id": first["agent_call_control_id"]}, "call.answered"
        )
    # Same run, leg still live at the carrier: reused.
    assert await audio_leg.create(service.ctx, p) == first
    # A new run of the app (the backend restarted): the old leg belongs to no audio client now.
    service.ctx.memo.pop("agent_legs", None)
    dials = len(carrier.dials)
    fresh = await audio_leg.create(service.ctx, p)
    assert fresh != first and len(carrier.dials) == dials + 1
    with service.db.transaction() as s:
        old = audio_leg.find(s, p, provider_id=first["agent_call_control_id"])
        assert old.data["state"] == "ended"
