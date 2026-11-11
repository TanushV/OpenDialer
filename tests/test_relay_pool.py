"""Run the real relay migrations in a private, disposable PostgreSQL cluster."""

import base64
import json
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from app.main import create_app
from app.managed_webhooks import deliver
from app.platform.config import Settings
from app.providers.replay_carrier import ReplayCarrier
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
POOL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NUMBER = "+12025550101"
SPARE = "+12025550102"
PROSPECT = "+14155550101"
PROSPECT_B = "+14155550102"
INSTALL = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
WRONG_INSTALL = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

pytestmark = pytest.mark.slow


def sql_string(value):
    return "'" + value.replace("'", "''") + "'"


def quote_json(value):
    return sql_string(json.dumps(value))


@pytest.fixture(scope="module")
def relay_db():
    initdb = shutil.which("initdb")
    if not initdb and (pg_config := shutil.which("pg_config")):
        bindir = subprocess.check_output([pg_config, "--bindir"], text=True).strip()  # noqa: S603
        candidate = Path(bindir) / "initdb"
        initdb = str(candidate) if candidate.is_file() else None
    if not initdb:
        if os.environ.get("OPENDIALER_REQUIRE_RELAY_TESTS"):
            pytest.fail("PostgreSQL server binaries are required in relay CI")
        pytest.skip("PostgreSQL server binaries are needed for the relay integration tests")
    bin_dir = Path(initdb).parent
    with tempfile.TemporaryDirectory(prefix="od-relay-") as directory:
        data = Path(directory) / "data"

        def run(tool, *args, **kwargs):
            return subprocess.run(  # noqa: S603 - PostgreSQL executables in the disposable fixture
                [str(bin_dir / tool), *map(str, args)], text=True, capture_output=True, check=True, **kwargs
            )

        def sql(query, *, seat=None):
            if seat:
                query = (
                    "set role authenticated; "
                    f"select set_config('request.jwt.claim.sub', '{seat}', false) as ignored \\gset\n" + query
                )
            try:
                return run(
                    "psql",
                    "-XAtq",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-h",
                    directory,
                    "-U",
                    "postgres",
                    "-d",
                    "postgres",
                    input=query,
                ).stdout.strip()
            except subprocess.CalledProcessError as exc:
                exc.add_note(exc.stderr)
                raise

        run("initdb", "-D", data, "-U", "postgres", "--auth=trust", "--no-locale", "-E", "UTF8")
        run(
            "pg_ctl",
            "-D",
            data,
            "-l",
            Path(directory) / "log",
            "-o",
            f"-c listen_addresses='' -c unix_socket_directories='{directory}'",
            "-w",
            "start",
        )
        try:
            sql("""
              create role anon;
              create role authenticated;
              create role service_role bypassrls;
              create schema auth;
              create table auth.users(id uuid primary key);
              create function auth.uid() returns uuid language sql stable as
                $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
              grant usage on schema auth to authenticated, service_role;
              create schema realtime;
              create table realtime.messages(extension text);
              create function realtime.topic() returns text language sql as $$ select ''::text $$;
              create function realtime.send(jsonb, text, text, boolean) returns void language sql as $$ select $$;
              grant usage on schema realtime to service_role;
              create schema cron;
              create function cron.schedule(text, text, text) returns int language sql as $$ select 1 $$;
            """)
            for path in sorted((ROOT / "supabase/migrations").glob("*.sql")):
                sql(path.read_text().replace("create extension if not exists pg_cron;", ""))
            yield sql
        finally:
            run("pg_ctl", "-D", data, "-m", "immediate", "-w", "stop")


@pytest.fixture
def pool_db(relay_db):
    relay_db(f"""
      truncate auth.users cascade;
      truncate relay_private.number_pools cascade;
      insert into auth.users values ('{A}'), ('{B}'), ('{OTHER}');
      insert into relay_workspaces(id,label) values ('{A}','A'), ('{B}','B'), ('{OTHER}','Other');
      insert into relay_routes(connection_id,account_id,workspace_id,public_key) values
        ('app-a','account','{A}','fixture'), ('app-b','account','{B}','fixture'),
        ('other-app','other-account','{OTHER}','fixture');
      insert into relay_private.number_pools(id,account_id) values ('{POOL}','account');
      insert into relay_private.number_pool_members(workspace_id,pool_id,segment_id) values
        ('{A}','{POOL}','14'), ('{B}','{POOL}','15');
      insert into relay_private.number_pool_numbers(phone,pool_id,connection_id) values
        ('{NUMBER}','{POOL}','app-a'), ('{SPARE}','{POOL}','app-a');
    """)
    return relay_db


def reserve(sql, seat, prospect, ref, preferred=NUMBER):
    return json.loads(sql(f"select relay_pool_reserve('{ref}','{prospect}','{preferred}');", seat=seat))


def submit(sql, seat, ref, call, number=NUMBER):
    return json.loads(sql(f"select relay_pool_submit('{ref}','{call}','{number}');", seat=seat))


def receipt(sql, identity, event_type, payload, *, seat=A):
    event = {
        "workspace_id": seat,
        "account_id": "account",
        "hash": identity,
        "received_at": "2026-11-11T18:00:00Z",
        "event": {"id": identity, "event_type": event_type, "payload": payload},
    }
    return sql(f"set role service_role; select relay_receive({quote_json(event)});")


def placed(sql, seat, ref, call, number=NUMBER, prospect=PROSPECT):
    assert submit(sql, seat, ref, call, number)["submitted"]
    payload = {
        "call_control_id": f"carrier-{call}",
        "connection_id": "app-a" if seat == A else "app-b",
        "direction": "outgoing",
        "from": number,
        "to": prospect,
        "client_state": base64.b64encode(json.dumps({"call_id": call}).encode()).decode(),
    }
    assert receipt(sql, call, "call.initiated", payload, seat=seat) == "received"
    assert receipt(sql, call + "-end", "call.hangup", payload, seat=seat) == "received"


def inbound(sql, tag, *, phone=NUMBER, caller=PROSPECT):
    payload = {
        "call_control_id": f"inbound-{tag}",
        "connection_id": "app-a",
        "direction": "incoming",
        "from": caller,
        "to": phone,
    }
    assert receipt(sql, tag, "call.initiated", payload) == "received"
    return json.loads(
        sql(
            f"select jsonb_build_array(workspace_id,event->>'callback_call_id') "
            f"from relay_events where event_id='{tag}';"
        )
    )


@pytest.fixture
def shared_db(pool_db):
    sql = pool_db
    sql(f"""
      update relay_private.number_pools set shared_connection_id='app-a' where id='{POOL}';
      update relay_workspaces set settings=jsonb_build_object(
        'telnyx_connection_id','app-a','sip_username',case when id='{A}' then 'rep-a' else 'rep-b' end
      ) where id in ('{A}','{B}');
    """)
    for seat in (A, B):
        sql(f"select relay_exchange('{seat}','{INSTALL}');", seat=seat)
    return sql


def shared_hold(sql, seat, ref, prospect, *, installation=INSTALL):
    return json.loads(
        sql(
            f"select relay_pool_reserve('{ref}','{prospect}','{NUMBER}',false,'{seat}','{installation}');",
            seat=seat,
        )
    )


def shared_submit(sql, seat, ref, call, *, installation=INSTALL):
    return json.loads(
        sql(
            f"select relay_pool_submit('{ref}','{call}','{NUMBER}','{seat}','{installation}');",
            seat=seat,
        )
    )


def context(**fields):
    return base64.b64encode(json.dumps(fields).encode()).decode()


def shared_out(call, target, **payload):
    return {
        "call_control_id": f"carrier-{call}",
        "connection_id": "app-a",
        "direction": "outgoing",
        "from": NUMBER,
        "to": target,
        "client_state": context(call_id=call),
        **payload,
    }


def event_owner(sql, event_id):
    return sql(f"select workspace_id from relay_events where event_id='{event_id}';")


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


def test_two_desktop_authorities_dial_from_one_pool_and_receive_only_their_own_callback(pool_db, tmp_path):
    sql = pool_db
    sql(f"update relay_private.number_pool_numbers set outbound_enabled=false where phone='{SPARE}';")
    second = "+14155550102"
    contacts = {"14": (PROSPECT, "Jordan"), "15": (second, "Casey")}
    clock = datetime(2026, 11, 11, 18, tzinfo=UTC)

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
            phone, name = contacts[segment]
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": segment, "properties": {"phone": phone, "firstname": name, "company": name}}
                    ],
                },
            )
        return httpx.Response(404)

    class RelayRPC:
        def __init__(self, seat):
            self.seat = seat

        def rpc(self, name, args):
            assert name in {"relay_pool_reserve", "relay_pool_submit", "relay_pool_cancel"}
            values = ",".join(sql_string(v) if isinstance(v, str) else str(v).lower() for v in args.values())

            async def execute():
                return SimpleNamespace(data=json.loads(sql(f"select {name}({values});", seat=self.seat)))

            return SimpleNamespace(execute=execute)

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
        service.managed.client = RelayRPC(seat)
        stack.callback(setattr, service.managed, "client", None)
        pool = json.loads(sql("select relay_pool_snapshot();", seat=seat))
        service.managed.apply_pool_snapshot(identity["tenant_id"], pool)
        phone, name = contacts[segment]
        send(
            "PUT",
            "/v1/admin/settings",
            {
                "hubspot_service_key": "fixture",
                "test_phone": phone,
                "telnyx_connection_id": connection,
                "sip_username": f"rep-{segment}",
                "realvalidito_api_key": "fixture",
                "realvalidito_api_secret": "fixture",
            },
        )
        assert send("GET", "/v1/hubspot/lists")["lists"] == [{"id": segment, "name": f"Rep {segment}"}]
        send("POST", "/v1/hubspot/sync", {"list_id": segment, "list_name": f"Rep {segment}"})
        send("POST", "/v1/admin/jobs/run")
        send("POST", "/v1/settings/test-call")
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
        reservation = send("POST", "/v1/queue/next", {"campaign_id": seeded["campaign_id"]})
        assert reservation["lead"]["name"] == name and reservation["number"]["phone"] == NUMBER
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
        assert receipt(sql, f"out-{segment}", "call.initiated", payload, seat=seat) == "received"
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
        b = desktop(stack, B, "15", "app-b")
        for seat, rep in [(A, a), (B, b)]:
            rep.send("POST", f"/v1/calls/{rep.call['id']}/hangup")
            rep.send("POST", "/v1/rep/presence", {"status": "paused"})
            rep.send("POST", "/v1/rep/reachable", {"registered": True})
            assert receipt(sql, f"ended-{seat}", "call.hangup", rep.payload, seat=seat) == "received"
        assert inbound(sql, "first-app", caller=PROSPECT) == [A, a.call["id"]]
        assert inbound(sql, "second-app", caller=second) == [B, b.call["id"]]
        sql(f"update relay_private.number_pool_numbers set daily_limit=2 where phone='{NUMBER}';")

        for seat, rep, tag, name in [(A, a, "first-app", "Jordan"), (B, b, "second-app", "Casey")]:
            batch = json.loads(
                sql(
                    f"select relay_exchange('{uuid4()}','{uuid4()}');",
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
