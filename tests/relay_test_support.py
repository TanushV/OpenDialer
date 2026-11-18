"""Disposable PostgreSQL fixtures for the shared calling relay tests."""

import base64
import json
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

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
        "received_at": datetime.now(UTC).isoformat(),
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


class RelayRPC:
    """Send the authority's pool RPCs to the isolated database as this rep."""

    def __init__(self, sql, seat):
        self.sql, self.seat = sql, seat

    def rpc(self, name, args):
        assert name in {
            "relay_pool_agent",
            "relay_pool_reserve",
            "relay_pool_submit",
            "relay_pool_cancel",
            "relay_callback_claim",
        }
        values = ",".join(sql_string(v) if isinstance(v, str) else str(v).lower() for v in args.values())

        async def execute():
            return SimpleNamespace(
                data=json.loads(self.sql(f"select {name}({values});", seat=self.seat) or "null")
            )

        return SimpleNamespace(execute=execute)
