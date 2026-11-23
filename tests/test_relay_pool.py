"""Run the real relay migrations in a private, disposable PostgreSQL cluster."""

import base64
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime
from threading import Barrier
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from app.main import create_app
from app.managed_webhooks import deliver
from app.platform.config import Settings
from app.providers.replay_carrier import ReplayCarrier
from fastapi.testclient import TestClient
from relay_test_support import (
    INSTALL,
    NUMBER,
    OTHER,
    POOL,
    PROSPECT,
    PROSPECT_B,
    SPARE,
    WRONG_INSTALL,
    A,
    B,
    RelayRPC,
    context,
    event_owner,
    inbound,
    placed,
    quote_json,
    receipt,
    reserve,
    shared_hold,
    shared_out,
    shared_submit,
    submit,
)
from relay_test_support import pool_db as pool_db
from relay_test_support import relay_db as relay_db
from relay_test_support import shared_db as shared_db

pytestmark = pytest.mark.slow


def test_shared_profile_activation_is_atomic_and_segment_scoped(shared_db):
    sql = shared_db
    rep = str(uuid4())
    bad = str(uuid4())
    sql(f"insert into auth.users values ('{rep}'),('{bad}');")
    sql(f"""
        insert into relay_invitations(hash,workspace_id,label,settings,routes,shared_pool_id,shared_segment_id)
        values('good','{rep}','New rep',
           '{{"telnyx_connection_id":"app-a","sip_username":"rep-new","telephony_credential_id":"cred-new"}}',
           '[]','{POOL}','16'),
        ('bad','{bad}','Duplicate SIP',
           '{{"telnyx_connection_id":"app-a","sip_username":"rep-new","telephony_credential_id":"cred-bad"}}',
           '[]','{POOL}','17');
        set role service_role;
        select relay_activate('good');
        select relay_activate('good');
    """)
    pool = json.loads(sql("select relay_pool_snapshot();", seat=rep))
    assert (pool["segment_id"], pool["shared_connection_id"]) == ("16", "app-a")
    with pytest.raises(subprocess.CalledProcessError, match="returned non-zero"):
        sql("set role service_role; select relay_activate('bad');")
    assert not sql(f"select id from relay_workspaces where id='{bad}';")
    assert sql("select count(*) from relay_routes where connection_id='app-a';") == "1"


def callback_claim(sql, seat, root):
    return json.loads(
        sql(
            f"select relay_callback_claim('{root}','{seat}','{INSTALL}');",
            seat=seat,
        )
    )


def callback_job(sql):
    return json.loads(sql("set role service_role; select relay_callback_take(1);"))


def callback_result(sql, job, result):
    return sql(f"""
      set role service_role;
      select relay_callback_finish({quote_json(job)},'{result}');
    """)


def test_hosted_fallback_and_desktop_cannot_both_claim_an_incoming_call(shared_db):
    sql = shared_db
    sql(f"update relay_private.number_pools set fallback_enabled=true where id='{POOL}';")
    sql(f"select relay_exchange('{A}','{INSTALL}','[]','',true,'Configured rep greeting');", seat=A)
    assert inbound(sql, "open") == [A, None]
    assert callback_job(sql) == []
    assert callback_claim(sql, B, "inbound-open") == {"status": "unavailable"}
    desktop = callback_claim(sql, A, "inbound-open")
    assert desktop["status"] == "desktop"
    assert callback_claim(sql, A, "inbound-open") == desktop
    sql("update relay_private.shared_callbacks set desktop_until=now()-interval '1 second';")
    (job,) = callback_job(sql)
    assert (job["action"], job["command_id"]) == ("answer", desktop["answer_id"])
    assert callback_job(sql) == []
    assert callback_claim(sql, A, "inbound-open")["status"] == "hosted"

    # Lost HTTP result: a signed answer advances the same claim, so the worker won't answer again.
    assert receipt(sql, "answered", "call.answered", {"call_control_id": "inbound-open"}) == "received"
    (job,) = callback_job(sql)
    assert (job["action"], job["greeting"]) == ("speak", "Configured rep greeting")
    callback_result(sql, job, "ok")
    assert callback_job(sql) == []
    assert receipt(sql, "said", "call.speak.ended", {"call_control_id": "inbound-open"}) == "received"
    (job,) = callback_job(sql)
    assert job["action"] == "hangup"
    callback_result(sql, job, "ok")
    assert callback_job(sql) == []
    assert (
        sql("select count(*) from relay_events where event->>'relay_callback'='true' and event_id='open';")
        == "1"
    )


def test_caller_already_gone_and_offline_fallback_retries(shared_db):
    sql = shared_db
    sql(f"update relay_private.number_pools set fallback_enabled=true where id='{POOL}';")
    gone = {"call_control_id": "inbound-gone", "connection_id": "app-a"}
    assert receipt(sql, "gone-before", "call.hangup", gone) == "pending"
    assert inbound(sql, "gone") == [A, None]
    assert callback_claim(sql, A, "inbound-gone")["status"] == "done"
    assert callback_job(sql) == []
    assert inbound(sql, "offline") == [A, None]
    (job,) = callback_job(sql)
    assert job["action"] == "answer"
    callback_result(sql, job, "retry")
    sql("update relay_private.shared_callbacks set next_at=now() where call_id='inbound-offline';")
    (retry,) = callback_job(sql)
    assert retry["command_id"] == job["command_id"]
    callback_result(sql, job, "ok")  # A late completion from the old worker lease changes nothing.
    assert callback_job(sql) == []
    callback_result(sql, retry, "ended")
    assert callback_job(sql) == []


def test_shared_app_routes_two_reps_by_current_lease_and_verified_endpoints(shared_db):
    sql = shared_db
    assert shared_hold(sql, A, "hold-a", PROSPECT)["phone"] == NUMBER
    assert shared_hold(sql, B, "hold-b", PROSPECT_B)["phone"] == NUMBER
    assert (
        shared_hold(sql, B, "other-install", "+14155550103", installation=WRONG_INSTALL)["reason"]
        == "unavailable"
    )
    assert shared_submit(sql, B, "hold-b", "call-b", installation=WRONG_INSTALL)["reason"] == "unavailable"
    assert submit(sql, B, "hold-b", "call-b")["reason"] == "unavailable"
    assert shared_submit(sql, B, "hold-b", "call-b")["submitted"]
    assert shared_submit(sql, A, "hold-a", "call-a")["submitted"]
    sparse = {"connection_id": "app-a", "call_control_id": "carrier-call-b"}
    assert receipt(sql, "b-early", "call.machine.detection.ended", sparse) == "pending"
    assert receipt(sql, "b-early", "call.machine.detection.ended", sparse) == "duplicate"
    assert not event_owner(sql, "b-early")
    assert receipt(sql, "unknown", "call.initiated", shared_out("unsubmitted", PROSPECT)) == "pending"
    assert (
        receipt(
            sql, "mismatch", "call.initiated", shared_out("call-b", PROSPECT, call_control_id="wrong-did")
        )
        == "pending"
    )
    assert not event_owner(sql, "mismatch")
    assert (
        receipt(
            sql,
            "b-confirmed",
            "call.initiated",
            shared_out("call-b", PROSPECT_B, client_state=context(call_id="call-b", tenant_id=A)),
        )
        == "received"
    )
    assert receipt(sql, "a-confirmed", "call.initiated", shared_out("call-a", PROSPECT)) == "received"
    assert [event_owner(sql, event) for event in ("b-early", "b-confirmed", "a-confirmed")] == [B, B, A]
    assert receipt(sql, "b-confirmed", "call.initiated", shared_out("call-b", PROSPECT_B)) == "duplicate"
    sql(f"update relay_workspaces set active=false where id='{A}';")
    route = json.loads(sql("set role service_role; select relay_route('','carrier-call-b');"))
    assert route["connection_id"] == "app-a"
    assert receipt(sql, "b-ended", "call.hangup", {"call_control_id": "carrier-call-b"}) == "received"
    assert event_owner(sql, "b-ended") == B
    assert sql("select count(*) from relay_private.number_pool_attempts where ended_at is not null;") == "1"


def test_shared_audio_and_return_legs_never_spend_outbound_capacity(shared_db):
    sql = shared_db
    sql(f"update relay_private.number_pool_numbers set daily_limit=1 where phone='{NUMBER}';")
    assert shared_hold(sql, B, "last-slot", PROSPECT)["phone"] == NUMBER
    assert shared_submit(sql, B, "last-slot", "last-call")["submitted"]
    assert receipt(sql, "dial", "call.initiated", shared_out("last-call", PROSPECT)) == "received"
    assert receipt(sql, "dial-ended", "call.hangup", {"call_control_id": "carrier-last-call"}) == "received"
    assert shared_hold(sql, A, "at-cap", PROSPECT) == {"reason": "capacity", "phone": NUMBER}

    def agent(sip):
        return json.loads(
            sql(
                f"select relay_pool_agent('audio-b','{NUMBER}','{sip}','{B}','{INSTALL}');",
                seat=B,
            )
        )

    assert agent("sip:rep-a@sip.telnyx.com")["reason"] == "unavailable"
    assert agent("sip:rep-b@sip.telnyx.com")["submitted"]
    audio = shared_out("audio-b", "sip:rep-b@sip.telnyx.com", client_state=context(agent_leg_id="audio-b"))
    assert receipt(sql, "audio-event", "call.answered", audio) == "received"
    assert event_owner(sql, "audio-event") == B
    assert inbound(sql, "shared-return") == [B, "last-call"]
    transfer = shared_out(
        "transfer",
        "sip:rep-b@sip.telnyx.com",
        client_state=context(inbound_call="inbound-shared-return", inbound_task="task", attempt="transfer"),
    )
    assert (
        receipt(
            sql,
            "wrong-transfer",
            "call.initiated",
            transfer
            | {
                "call_control_id": "wrong-transfer",
                "to": "sip:rep-a@sip.telnyx.com",
            },
        )
        == "pending"
    )
    assert receipt(sql, "transfer", "call.initiated", transfer) == "received"
    assert receipt(sql, "transfer-end", "call.hangup", {"call_control_id": "carrier-transfer"}) == "received"
    assert [event_owner(sql, e) for e in ("transfer", "transfer-end", "wrong-transfer")] == [B, B, ""]
    assert (
        sql("select count(*) from relay_private.number_pool_attempts where submitted_at is not null;") == "1"
    )


def test_shared_number_stays_sticky_when_a_rep_retries_and_reaches_attempt_ninety(shared_db):
    sql = shared_db
    assert shared_hold(sql, A, "first", PROSPECT)["phone"] == NUMBER
    assert shared_submit(sql, A, "first", "a-first")["submitted"]
    assert receipt(sql, "first-dial", "call.initiated", shared_out("a-first", PROSPECT)) == "received"
    assert receipt(sql, "first-end", "call.hangup", {"call_control_id": "carrier-a-first"}) == "received"
    assert shared_hold(sql, B, "no-carrier", PROSPECT)["phone"] == NUMBER
    assert shared_submit(sql, B, "no-carrier", "b-unsent")["submitted"]
    sql(f"select relay_pool_cancel('no-carrier',true,'{B}','{INSTALL}');", seat=B)
    assert inbound(sql, "before-handoff") == [A, "a-first"]
    sql(f"""
      insert into relay_private.number_pool_attempts(
        workspace_id,pool_id,ref,prospect,phone,expires_at,submitted_at,call_ref,ended_at
      ) select '{A}','{POOL}','seed-'||g,'+1212555'||lpad(g::text,4,'0'),'{NUMBER}',
        now(),now(),'seed-'||g,now() from generate_series(1,88) g;
    """)
    hold = json.loads(
        sql(
            f"select relay_pool_reserve('retry','{PROSPECT}','{SPARE}',false,'{B}','{INSTALL}');",
            seat=B,
        )
    )
    assert hold["phone"] == NUMBER
    assert shared_submit(sql, B, "retry", "b-ninety")["submitted"]
    assert receipt(sql, "ninetieth", "call.initiated", shared_out("b-ninety", PROSPECT)) == "received"
    assert receipt(sql, "ninety-end", "call.hangup", {"call_control_id": "carrier-b-ninety"}) == "received"
    assert (
        receipt(sql, "late-old-answer", "call.answered", {"call_control_id": "carrier-a-first"}) == "received"
    )
    assert shared_hold(sql, A, "ninety-one", PROSPECT) == {"reason": "capacity", "phone": NUMBER}
    assert inbound(sql, "at-ninety") == [B, "b-ninety"]


def test_bindings_follow_the_prospect_and_only_confirmed_calls_change_routing(pool_db):
    sql = pool_db
    assert reserve(sql, B, PROSPECT, "first")["phone"] == NUMBER
    assert reserve(sql, A, PROSPECT, "overlap")["reason"] == "busy"
    placed(sql, B, "first", "b-call")
    assert inbound(sql, "first-return") == [B, "b-call"]
    assert reserve(sql, A, PROSPECT, "handoff", SPARE)["phone"] == NUMBER
    assert submit(sql, A, "handoff", "rejected")["submitted"]
    sql("select relay_pool_cancel('handoff',true);", seat=A)
    assert inbound(sql, "still-b") == [B, "b-call"]
    assert reserve(sql, A, PROSPECT, "retry", SPARE)["phone"] == NUMBER
    placed(sql, A, "retry", "a-call")
    assert inbound(sql, "now-a") == [A, "a-call"]
    assert (
        receipt(
            sql,
            "callback-end",
            "call.hangup",
            {
                "call_control_id": "inbound-first-return",
                "connection_id": "app-a",
            },
        )
        == "received"
    )
    assert sql("select workspace_id from relay_events where event_id='callback-end';") == B


def test_pool_cap_counts_ninety_outbound_attempts_and_callbacks_bypass_it(pool_db):
    sql = pool_db
    assert reserve(sql, B, PROSPECT, "original")["phone"] == NUMBER
    placed(sql, B, "original", "b-call")
    sql(f"""
      insert into relay_private.number_pool_attempts(
        workspace_id,pool_id,ref,prospect,phone,expires_at,submitted_at,call_ref,ended_at
      ) select '{A}','{POOL}','seed-'||g,'+1212555'||lpad(g::text,4,'0'),'{NUMBER}',
        now(), now(), 'seed-'||g,now() from generate_series(1,88) g;
    """)
    assert reserve(sql, A, PROSPECT, "ninetieth", SPARE)["phone"] == NUMBER
    assert reserve(sql, B, PROSPECT, "over")["reason"] == "busy"
    placed(sql, A, "ninetieth", "last-call")
    assert reserve(sql, B, PROSPECT, "over") == {"phone": NUMBER, "reason": "capacity"}
    assert inbound(sql, "at-cap") == [A, "last-call"]
    assert inbound(sql, "again-at-cap") == [A, "last-call"]
    sql(f"update relay_private.number_pool_numbers set outbound_enabled=false where phone='{NUMBER}';")
    assert inbound(sql, "paused-number") == [A, "last-call"]
    sql("update relay_events set received_at=now()-interval '2 days'; select relay_private.cleanup();")
    assert (
        sql("select event->>'callback_call_id' from relay_events where event_id='paused-number';")
        == "last-call"
    )


def test_simultaneous_rep_reservations_share_the_last_outbound_slot(pool_db):
    sql = pool_db
    sql(f"""
      update relay_private.number_pool_numbers set daily_limit=1, outbound_enabled=(phone='{NUMBER}');
    """)
    ready = Barrier(2, timeout=5)

    def race(seat, prospect):
        ready.wait()
        return reserve(sql, seat, prospect, "last-slot")

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = [executor.submit(race, A, PROSPECT), executor.submit(race, B, "+14155550102")]
        results = [attempt.result() for attempt in attempts]
    assert {tuple(result.items()) for result in results} == {(("phone", NUMBER),), (("reason", "capacity"),)}


def test_existing_desktop_caller_id_waits_if_its_first_pool_reservation_hits_capacity(pool_db):
    sql = pool_db
    sql(f"update relay_private.number_pool_numbers set daily_limit=1 where phone='{NUMBER}';")
    assert reserve(sql, B, "+14155550103", "takes-last-slot") == {"phone": NUMBER}
    retained = json.loads(sql(f"select relay_pool_reserve('upgrade','{PROSPECT}','{NUMBER}',true);", seat=A))
    assert retained == {"phone": NUMBER, "reason": "capacity"}
    assert reserve(sql, B, PROSPECT, "handoff", SPARE) == retained
    sql("select relay_pool_cancel('takes-last-slot');", seat=B)
    assert reserve(sql, B, PROSPECT, "resume", SPARE) == {"phone": NUMBER}


def test_callback_ownership_survives_attempt_cleanup_and_pins_the_transfer_leg(pool_db):
    sql = pool_db
    assert reserve(sql, B, PROSPECT, "original") == {"phone": NUMBER}
    placed(sql, B, "original", "b-call")
    sql("""
      update relay_private.number_pool_attempts
      set submitted_at=now()-interval '40 days', expires_at=now()-interval '40 days';
      select relay_private.cleanup_number_pool();
    """)
    assert sql("select count(*) from relay_private.number_pool_attempts;") == "0"
    assert inbound(sql, "late-return") == [B, "b-call"]

    context = {"inbound_task": "task-b", "attempt": "transfer-b", "inbound_call": "inbound-late-return"}
    state = base64.b64encode(json.dumps(context).encode()).decode()
    leg = {"call_control_id": "transfer-leg", "connection_id": "app-a"}
    assert receipt(sql, "leg-start", "call.initiated", {**leg, "client_state": state}) == "received"
    assert receipt(sql, "leg-bridge", "call.bridged", leg) == "received"
    assert json.loads(
        sql("""select jsonb_build_array(workspace_id,event->'payload'->>'client_state')
               from relay_events where event_id='leg-bridge';""")
    ) == [B, state]


def test_pool_membership_and_segment_are_scoped_to_the_authenticated_workspace(pool_db):
    sql = pool_db
    assert json.loads(sql("select relay_pool_snapshot();", seat=A))["segment_id"] == "14"
    assert json.loads(sql("select relay_pool_snapshot();", seat=B))["segment_id"] == "15"
    assert sql("select relay_pool_snapshot() is null;", seat=OTHER) == "t"
    assert reserve(sql, OTHER, PROSPECT, "outside") == {"reason": "unavailable"}
    with pytest.raises(subprocess.CalledProcessError):
        sql("select * from relay_private.number_pool_attempts;", seat=B)


@pytest.mark.parametrize("shared", [False, True], ids=["legacy", "one-application"])
def test_two_desktop_authorities_dial_from_one_pool_and_receive_only_their_own_callback(
    request, tmp_path, shared
):
    sql = request.getfixturevalue("shared_db" if shared else "pool_db")
    if shared:
        sql(f"update relay_private.number_pools set fallback_enabled=true where id='{POOL}';")
    sql(f"update relay_private.number_pool_numbers set outbound_enabled=false where phone='{SPARE}';")
    second = "+14155550102"
    contacts = {"14": (PROSPECT, "Jordan"), "15": (second, "Casey")}
    stored_phones = {
        "14": {"phone": "(415) 555-0101", "mobilephone": "+14155550109"},
        "15": {"phone": "Not provided", "mobilephone": "415.555.0102"},
    }
    clock = datetime(2026, 11, 12, 18, tzinfo=UTC)  # A working day: November 11 is a federal holiday.

    def hubspot(request):
        path = request.url.path
        if path == "/dnclookup/validate":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "number": number,
                            "federal_dnc": False,
                            "state_dnc": {},
                            "tcpa_litigator": False,
                            "invalid": False,
                        }
                        for number in json.loads(request.content)["numbers"]
                    ]
                },
            )
        if path.endswith("/lists/search"):
            return httpx.Response(200, json={"lists": [{"listId": s, "name": f"Rep {s}"} for s in contacts]})
        if path.endswith("/memberships"):
            return httpx.Response(200, json={"results": [{"recordId": path.split("/")[-2]}]})
        if path.endswith("/contacts/batch/read"):
            segment = json.loads(request.content)["inputs"][0]["id"]
            _, name = contacts[segment]
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": segment,
                            "properties": {**stored_phones[segment], "firstname": name, "company": name},
                        }
                    ],
                },
            )
        return httpx.Response(404)

    def desktop(stack, seat, segment, connection):
        directory = tmp_path / segment
        directory.mkdir()
        settings = Settings(
            data_dir=directory,
            database_url=f"sqlite:///{directory}/opendialer.db",
            worker_enabled=False,
            simulation=True,
            local_token="pool-fixture-local",
        )
        carrier = ReplayCarrier()
        app = create_app(settings, clock=lambda: clock, carrier=carrier)
        stack.callback(app.state.db.engine.dispose)
        client = stack.enter_context(TestClient(app))
        session = client.post(
            "/v1/setup",
            headers={"X-Local-Token": "pool-fixture-local"},
            json={
                "seller_name": f"Rep {segment}",
                "campaign_name": "Pool pilot",
            },
        )
        assert session.is_success, session.text
        identity = session.json()
        client.headers["authorization"] = f"Bearer {identity['access_token']}"

        def send(method, path, data=None):
            response = client.request(method, path, json=data)
            assert response.is_success, response.text
            return response.json()

        seeded = send("POST", "/v1/simulation/seed")
        service = app.state.service
        service.http = httpx.AsyncClient(transport=httpx.MockTransport(hubspot))
        stack.callback(client.portal.call, service.http.aclose)
        service.managed.client = RelayRPC(sql, seat)
        stack.callback(setattr, service.managed, "client", None)
        pool = json.loads(sql("select relay_pool_snapshot();", seat=seat))
        service.managed.apply_pool_snapshot(identity["tenant_id"], pool)
        phone, name = contacts[segment]
        send(
            "PUT",
            "/v1/admin/settings",
            {
                "hubspot_service_key": "fixture",
                "telnyx_connection_id": connection,
                "sip_username": f"rep-{segment}",
                "realvalidito_api_key": "fixture",
                "realvalidito_api_secret": "fixture",
            },
        )
        if shared:
            service.managed.config = {
                "workspace_id": seat,
                "tenant_id": identity["tenant_id"],
                "store_id": seat,
            }
            service.managed.installation = INSTALL
            service.managed.last_exchange = monotonic()
            service.managed.caught_up = True
        assert send("GET", "/v1/hubspot/lists")["lists"] == [{"id": segment, "name": f"Rep {segment}"}]
        send("POST", "/v1/hubspot/sync", {"list_id": segment, "list_name": f"Rep {segment}"})
        send("POST", "/v1/admin/jobs/run")
        prepared = send("POST", "/v1/preparation/lists", {"list_name": f"Rep {segment}"})
        assert prepared["marked"] == 1
        send("POST", "/v1/admin/jobs/run")
        send(
            "POST",
            "/v1/rep/presence",
            {
                "status": "ready",
                "audio_ready": True,
                "telephony_ready": True,
                "agent_call_control_id": "agent-fixture",
            },
        )
        reservation = send(
            "POST", "/v1/queue/next", {"campaign_id": seeded["campaign_id"], "list_names": [f"Rep {segment}"]}
        )
        assert reservation["lead"]["name"] == name and reservation["lead"]["external_id"] == segment
        assert reservation["lead"]["phone"] == phone and reservation["number"]["phone"] == NUMBER
        started = send(
            "POST",
            "/v1/calls/start",
            {
                "reservation_id": reservation["reservation_id"],
                "idempotency_key": str(uuid4()),
            },
        )
        payload = {
            **carrier.dials[0],
            "call_control_id": started["provider_id"],
            "connection_id": connection,
            "direction": "outgoing",
        }
        assert payload["to"] == phone and started["is_test"] is False
        assert (
            receipt(sql, f"out-{segment}", "call.initiated", payload, seat=A if shared else seat)
            == "received"
        )
        return SimpleNamespace(
            send=send,
            scope=identity["tenant_id"],
            client=client,
            service=service,
            call=started,
            payload=payload,
        )

    with ExitStack() as stack:
        a = desktop(stack, A, "14", "app-a")
        b = desktop(stack, B, "15", "app-a" if shared else "app-b")
        for seat, rep in [(A, a), (B, b)]:
            rep.send("POST", f"/v1/calls/{rep.call['id']}/hangup")
            rep.send("POST", "/v1/rep/presence", {"status": "paused"})
            rep.send("POST", "/v1/rep/reachable", {"registered": True})
            assert (
                receipt(sql, f"ended-{seat}", "call.hangup", rep.payload, seat=A if shared else seat)
                == "received"
            )
            if shared:
                sql(f"select relay_exchange('{seat}','{INSTALL}','[]','',true);", seat=seat)
        sql(f"update relay_private.number_pool_numbers set daily_limit=2 where phone='{NUMBER}';")
        assert inbound(sql, "first-app", caller=PROSPECT) == [A, a.call["id"]]
        assert inbound(sql, "second-app", caller=second) == [B, b.call["id"]]

        for seat, rep, tag, name in [(A, a, "first-app", "Jordan"), (B, b, "second-app", "Casey")]:
            batch = json.loads(
                sql(
                    f"select relay_exchange('{seat if shared else uuid4()}','{INSTALL if shared else uuid4()}');",
                    seat=seat,
                )
            )
            rep.service.managed.apply_pool_snapshot(rep.scope, batch["pool"])
            rep.client.portal.call(deliver, rep.service, rep.scope, batch["events"])
            rep.send("POST", "/v1/admin/jobs/run")
            rep.send("POST", "/v1/admin/control/run")
            tasks = [t for t in rep.send("GET", "/v1/workspace")["tasks"] if t.get("inbound")]
            assert len(tasks) == 1 and tasks[0]["provider_id"] == f"inbound-{tag}"
            assert tasks[0]["lead_name"] == name and tasks[0]["routed_rep_id"]
            assert rep.send("GET", f"/v1/leads/{tasks[0]['lead_id']}/packet")["lead"]["name"] == name
        if shared:
            b.send("POST", "/v1/rep/reachable", {"registered": False})
            sql(f"select relay_exchange('{B}','{INSTALL}','[]','',false);", seat=B)
            assert inbound(sql, "closed-b", caller=second) == [B, b.call["id"]]
            (job,) = callback_job(sql)
            assert job["call_id"] == "inbound-closed-b"
            callback_result(sql, job, "ended")
            batch = json.loads(sql(f"select relay_exchange('{B}','{INSTALL}');", seat=B))
            for _ in range(2):
                b.client.portal.call(deliver, b.service, b.scope, batch["events"])
                b.send("POST", "/v1/admin/jobs/run")
                b.send("POST", "/v1/admin/control/run")
            tasks = [
                t
                for t in b.send("GET", "/v1/workspace")["tasks"]
                if t.get("provider_id") == "inbound-closed-b"
            ]
            assert len(tasks) == 1 and tasks[0]["lead_name"] == "Casey"
            assert tasks[0]["route"] == "missed_offline" and tasks[0]["inbound_state"] == "ended"

        sql("update relay_private.number_pool_numbers set outbound_enabled=false;")
        a.service.managed.apply_pool_snapshot(
            a.scope, json.loads(sql("select relay_pool_snapshot();", seat=A))
        )
        blockers = {item["code"]: item for item in a.send("GET", "/v1/blockers")["blockers"]}
        assert "pool operator" in blockers["CALLER_NUMBER_PAUSED"]["message"]
        sql(f"update relay_private.number_pool_numbers set outbound_enabled=true where phone='{NUMBER}';")
        a.service.managed.apply_pool_snapshot(
            a.scope, json.loads(sql("select relay_pool_snapshot();", seat=A))
        )
        assert "CALLER_NUMBER_PAUSED" not in {
            item["code"] for item in a.send("GET", "/v1/blockers")["blockers"]
        }
