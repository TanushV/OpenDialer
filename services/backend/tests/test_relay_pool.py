"""R9C.3 and R9C.7: the shared number pool in the relay, run on a local PostgreSQL with the real migrations.

The Supabase-only pieces (auth.uid(), realtime.send, pg_cron) are stubbed; every relay migration runs as written. Two
seats leasing at the same moment never get one number, the daily cap counts every seat, the prospect's area code wins,
and a callback reaches the seat that last called, else the owner's seat.
"""

import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

if os.name == "nt":
    pytest.skip("the relay runs on Supabase; its SQL is tested on Mac and Linux", allow_module_level=True)
pgserver = pytest.importorskip("pgserver")
pytestmark = pytest.mark.slow

MIGRATIONS = sorted((Path(__file__).resolve().parents[3] / "supabase/migrations").glob("*.sql"))
PSQL = Path(pgserver._commands.POSTGRES_BIN_PATH) / "psql"
SEAT_A, SEAT_B, OWNER = (str(uuid.UUID(int=i)) for i in (1, 2, 3))
STUBS = """
create role anon nologin; create role authenticated nologin; create role service_role nologin bypassrls;
create schema auth; create table auth.users(id uuid primary key);
create function auth.uid() returns uuid language sql stable as
 $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
grant usage on schema auth to authenticated, service_role; grant execute on function auth.uid() to authenticated, service_role;
create schema realtime; create table realtime.messages(id serial primary key, extension text, topic text);
alter table realtime.messages enable row level security;
create function realtime.topic() returns text language sql stable as $$ select '' $$;
create table realtime.sent(payload jsonb, event text, topic text);
create function realtime.send(payload jsonb, event text, topic text, private boolean) returns void language sql as
 $$ insert into realtime.sent values (payload, event, topic) $$;
grant usage on schema realtime to authenticated, service_role;
grant insert on realtime.sent to service_role; grant execute on function realtime.send(jsonb,text,text,boolean) to service_role;
create schema cron; create function cron.schedule(a text, b text, c text) returns bigint language sql as $$ select 1::bigint $$;
"""


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    server = pgserver.get_server(tmp_path_factory.mktemp("relay"), cleanup_mode="stop")
    uri = server.get_uri()

    def sql(text, *, role=None, seat=None):
        prefix = ""
        if role:
            prefix += f"set role {role};"
        if seat:
            prefix += f"set request.jwt.claim.sub to '{seat}';"
        done = subprocess.run(  # noqa: S603 - the bundled psql against a throwaway local server
            [str(PSQL), uri, "-v", "ON_ERROR_STOP=1", "-At", "-q", "-c", prefix + text],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if done.returncode:
            raise RuntimeError(done.stderr.strip())
        return done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""

    sql(STUBS)
    for path in MIGRATIONS:
        sql(path.read_text().replace("create extension if not exists pg_cron;", ""))
    seats = ", ".join(f"('{s}')" for s in (SEAT_A, SEAT_B, OWNER))
    sql(f"insert into auth.users values {seats}")
    sql(
        f"insert into public.relay_workspaces(id, label) values ('{SEAT_A}', 'A'), ('{SEAT_B}', 'B'), ('{OWNER}', 'Owner');"
        f"insert into public.relay_routes values ('1000001', 'acct', '{SEAT_A}', 'key'), ('1000002', 'acct', '{SEAT_B}', 'key'),"
        f" ('1000003', 'acct', '{OWNER}', 'key');"
        f"insert into public.relay_pool_settings(owner_workspace) values ('{OWNER}');"
    )
    yield sql
    server.cleanup()


def reset(db, numbers):
    db(
        "delete from public.relay_pool_leases; delete from public.relay_pool_use; delete from public.relay_last_caller;"
        "delete from public.relay_pool_calls; delete from public.relay_pool; delete from public.relay_events;"
    )
    values = ", ".join(f"('{phone}', '{phone[2:5]}', {cap})" for phone, cap in numbers)
    db(f"insert into public.relay_pool(phone, area_code, daily_cap) values {values}")


def lease(db, seat, prospect, areas, call_ref):
    areas_sql = "array[" + ",".join(f"'{a}'" for a in areas) + "]::text[]"
    return db(
        f"select relay_lease_number('{prospect}', {areas_sql}, '{call_ref}', 600)->>'phone'",
        role="authenticated",
        seat=seat,
    )


def test_two_seats_leasing_at_once_never_get_the_same_number(db):
    reset(db, [(f"+1415555{i:04d}", 50) for i in range(10)])
    with ThreadPoolExecutor(max_workers=10) as pool:
        got = list(
            pool.map(
                lambda i: lease(db, SEAT_A if i % 2 else SEAT_B, f"+1212555{i:04d}", ["415"], f"call-{i}"),
                range(10),
            )
        )
    assert len(set(got)) == 10, got
    assert lease(db, SEAT_A, "+12125559999", ["415"], "call-x") == "", (
        "every number is leased; the pool says so"
    )


def test_the_daily_cap_counts_every_seat_and_a_repeat_ask_counts_once(db):
    reset(db, [("+14155550001", 2)])
    first = lease(db, SEAT_A, "+12125550001", ["415"], "a-1")
    assert lease(db, SEAT_A, "+12125550001", ["415"], "a-1") == first, "the same call gets the same number"
    db("select relay_release_number('a-1')", role="authenticated", seat=SEAT_A)
    assert lease(db, SEAT_B, "+12125550002", ["415"], "b-1") == first
    db("select relay_release_number('b-1')", role="authenticated", seat=SEAT_B)
    assert lease(db, SEAT_A, "+12125550003", ["415"], "a-2") == "", (
        "two attempts across both seats reach the cap of 2"
    )
    assert db("select attempts from public.relay_pool_use") == "2"


def test_the_prospects_area_code_wins_then_the_least_used_number(db):
    reset(db, [("+14155550001", 50), ("+12125550001", 50), ("+12125550002", 50)])
    assert lease(db, SEAT_A, "+12125551111", ["212"], "c-1") == "+12125550001"
    db("select relay_release_number('c-1')", role="authenticated", seat=SEAT_A)
    assert lease(db, SEAT_A, "+12125552222", ["212"], "c-2") == "+12125550002", "the less used 212 number"
    db("select relay_release_number('c-2')", role="authenticated", seat=SEAT_A)
    assert lease(db, SEAT_A, "+13105553333", ["310"], "c-3") == "+14155550001", (
        "no local number: the least used"
    )


def test_a_seat_cannot_release_another_seats_lease(db):
    reset(db, [("+14155550001", 50)])
    phone = lease(db, SEAT_A, "+12125550001", ["415"], "mine")
    db("select relay_release_number('mine')", role="authenticated", seat=SEAT_B)
    assert lease(db, SEAT_B, "+12125550002", ["415"], "theirs") == "", f"{phone} is still leased by seat A"


def receive(db, event_id, call_id, frm, to, connection="1000003"):
    payload = f'{{"direction": "incoming", "from": "{frm}", "to": "{to}", "call_control_id": "{call_id}", "connection_id": "{connection}"}}'
    receipt = (
        f'{{"workspace_id": "{OWNER}", "account_id": "acct", "hash": "{event_id}", "received_at": "2026-11-09T10:00:00Z",'
        f' "event": {{"id": "{event_id}", "event_type": "call.initiated", "payload": {payload}}}}}'
    )
    db(f"select relay_receive('{receipt}'::jsonb)", role="service_role")
    return db(f"select workspace_id from public.relay_events where event_id = '{event_id}'")


def test_a_callback_to_a_pool_number_rings_the_seat_that_called_and_unknown_callers_reach_the_owner(db):
    reset(db, [("+14155550001", 50)])
    phone = lease(db, SEAT_B, "+12125557777", ["415"], "cb-1")
    assert receive(db, "ev-1", "inbound-1", "+12125557777", phone) == SEAT_B
    assert receive(db, "ev-2", "inbound-1", "+12125557777", phone) == SEAT_B, "later events follow the pin"
    assert receive(db, "ev-3", "inbound-2", "+19995550000", phone) == OWNER
    assert db("select count(*) from public.relay_numbers where phone = '" + phone + "'") == "0", (
        "a pool number is never claimed by one seat"
    )


def test_clients_cannot_read_the_pool_tables_or_forge_a_seat(db):
    with pytest.raises(RuntimeError):
        db("select * from public.relay_pool_leases", role="authenticated", seat=SEAT_A)
    with pytest.raises(RuntimeError):
        db(f"select relay_pool_seat('x', '+12125550000', '{SEAT_A}')", role="authenticated", seat=SEAT_A)


def test_only_the_owner_sets_up_a_seat_on_its_own_account_and_a_route_is_never_shared(db):
    hash_ = "a" * 64
    settings = '\'{"telephony_credential_id": "cred-9", "sip_username": "rep9"}\'::jsonb'
    with pytest.raises(RuntimeError, match="Only the team owner"):
        db(
            f"select relay_invite_seat('Rep', '{hash_}', '123456789', {settings})",
            role="authenticated",
            seat=SEAT_A,
        )
    with pytest.raises(RuntimeError, match="Route already assigned"):
        db(
            f"select relay_invite_seat('Rep', '{hash_}', '1000001', {settings})",
            role="authenticated",
            seat=OWNER,
        )
    seat = db(
        f"select relay_invite_seat('Rep 9', '{hash_}', '123456789', {settings})",
        role="authenticated",
        seat=OWNER,
    )
    route = db(
        f"select routes->0->>'account_id' || '|' || (settings->>'telnyx_connection_id') from public.relay_invitations where workspace_id = '{seat}'"
    )
    assert route == "acct|123456789", "the new seat's route is on the owner's account"
    db(f"insert into auth.users values ('{seat}')")  # managed-setup creates the sign-in before activation
    db(f"select relay_activate('{hash_}')", role="service_role")
    assert db("select workspace_id from public.relay_routes where connection_id = '123456789'") == seat


def test_only_the_owner_changes_the_pool(db):
    with pytest.raises(RuntimeError, match="Only the team owner"):
        db("select relay_set_pool_number('+13125550000')", role="authenticated", seat=SEAT_A)
    db("select relay_set_pool_number('+13125550000', 40)", role="authenticated", seat=OWNER)
    assert (
        db("select area_code || '|' || daily_cap from public.relay_pool where phone = '+13125550000'")
        == "312|40"
    )
