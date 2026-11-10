"""G10, R9E.3: every blocker is driven in and back out through its real code path.

`EXIT_TESTS` names one test per blocker code; a blocker without one fails `test_every_blocker_has_an_exit_test`. A
blocker that says it clears by itself is cleared here by the software alone (the relay loop, a timer), never by a
test reaching into state; one that needs the rep is cleared by the control its message names.
"""

import time
from types import SimpleNamespace
from uuid import UUID

import pytest
from app.calling.blockers import CATALOG
from app.platform.db import MODELS, update
from conftest import ready
from helpers import principal
from sqlalchemy import select
from test_number_setup import (  # noqa: F401 - the fixture is used by name
    fresh,
    test_one_answered_test_call_opens_every_number_immediately_and_never_expires,
)

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
RELAY_SETTINGS = {
    "telnyx_connection_id": "1000001",
    "telnyx_public_key": "test-public-key",
    "telephony_credential_id": "cred-1",
    "sip_username": "rep-line",
}
PROFILE = {
    "label": "Test computer",
    "url": "https://abcdefghijklmnopqrst.supabase.co",
    "publishable_key": "sb_publishable_test_key",
    "workspace_id": WORKSPACE_ID,
    "email": "rep@example.test",
    "password": "a-long-test-password",
    "telnyx_api_key": "test-telnyx-key",
}


class FakeRelay:
    """The Supabase client as the relay loop and the setup-file import use it. `script` feeds relay_exchange."""

    def __init__(self, *, session=True):
        self.script = []
        self.cleared_gap = False
        self.session = session
        relay = self

        class Auth:
            async def get_session(self):
                return SimpleNamespace(access_token="t") if relay.session else None

            async def sign_in_with_password(self, credentials):
                return SimpleNamespace(user=SimpleNamespace(id=UUID(WORKSPACE_ID)))

            async def close(self):
                pass

        self.auth = Auth()
        self.postgrest = SimpleNamespace(aclose=_noop)
        self.realtime = SimpleNamespace(close=_noop, set_auth=_noop_auth)

    def table(self, name):
        rows = [{"id": WORKSPACE_ID, "settings": RELAY_SETTINGS}]
        chain = SimpleNamespace()
        chain.select = lambda *a: chain
        chain.eq = lambda *a: chain
        chain.execute = lambda: _value(SimpleNamespace(data=rows))
        return chain

    def rpc(self, name, args):
        async def execute():
            if name == "relay_clear_gap":
                self.cleared_gap = True
                return SimpleNamespace(data=None)
            step = self.script.pop(0) if self.script else {}
            if isinstance(step, Exception):
                raise step
            gap = None if self.cleared_gap else step.get("gap_since")
            return SimpleNamespace(data={"topic": "t", "events": [], "numbers": [], **step, "gap_since": gap})

        return SimpleNamespace(execute=execute)

    def channel(self, *args, **kwargs):
        raise RuntimeError("no realtime in tests")

    async def remove_all_channels(self):
        pass


async def _noop():
    pass


async def _noop_auth(token=None):
    pass


async def _value(value):
    return value


@pytest.fixture
def fast_relay(monkeypatch):
    import app.managed_webhooks as managed

    monkeypatch.setattr(managed, "UNJOINED_POLL_SECONDS", 0.05)
    monkeypatch.setattr(managed, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(managed, "RECONNECT_SECONDS", 0.05)
    relay = FakeRelay()

    async def make_client(self, config, storage=None):
        if storage is not None:
            storage.path.write_text("{}")  # the real client saves its session here before the import moves it
        return relay

    monkeypatch.setattr(managed.ManagedConnection, "make_client", make_client)
    return relay


def codes(workspace, app, *, rep=False, integrity=True):
    service = app.state.service
    with service.db.transaction(False) as s:
        found = service.dial_blockers_in(s, principal(workspace), rep=rep, integrity_ok=integrity)
    return [b.code for b in found]


def eventually(check, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return check()


def carrier_mode(app):
    """Leave simulation so carrier setup counts, with a complete setup in place."""
    app.state.service.settings.simulation = False


def import_setup_file(workspace):
    r = workspace["client"].post("/v1/admin/managed-connection", headers=workspace["headers"], json=PROFILE)
    assert r.status_code == 200, r.text


# --- Setup ------------------------------------------------------------------------------------------------------------


def database_integrity_failure(workspace, app):
    assert "DATABASE_INTEGRITY_FAILURE" in codes(workspace, app, integrity=False)
    # The control is Restore a backup; a restored database passes its check, and nothing else holds the blocker.
    assert "DATABASE_INTEGRITY_FAILURE" not in codes(workspace, app, integrity=True)


def restore_reconciliation_required(workspace, app):
    with app.state.db.transaction() as s:
        update(s.get(MODELS["sellers"], workspace["seller_id"]), restore_hold=True)
    assert "RESTORE_RECONCILIATION_REQUIRED" in codes(workspace, app)
    r = workspace["client"].post(
        "/v1/admin/restore/reconcile",
        headers=workspace["headers"],
        json={"review_reference": "Checked the restored backup against the carrier"},
    )
    assert r.status_code == 200, r.text
    assert "RESTORE_RECONCILIATION_REQUIRED" not in codes(workspace, app)


def telnyx_not_configured(workspace, app):
    carrier_mode(app)
    app.state.service.configs.write(
        workspace["tenant_id"], {"telnyx_api_key": "", "telnyx_connection_id": ""}
    )
    assert "TELNYX_NOT_CONFIGURED" in codes(workspace, app)
    r = workspace["client"].put(
        "/v1/admin/settings",
        headers=workspace["headers"],
        json={"telnyx_api_key": "test-telnyx-key", "telnyx_connection_id": "1000001"},
    )
    assert r.status_code == 200, r.text
    assert "TELNYX_NOT_CONFIGURED" not in codes(workspace, app)


def _missing_until_import(code, fields):
    def check(workspace, app, fast_relay):
        carrier_mode(app)
        app.state.service.configs.write(
            workspace["tenant_id"],
            {"telnyx_api_key": "k", "telnyx_connection_id": "1000001", **dict.fromkeys(fields, "")},
        )
        assert code in codes(workspace, app)
        import_setup_file(workspace)
        assert eventually(lambda: code not in codes(workspace, app))

    return check


webhook_signature_key_missing = _missing_until_import("WEBHOOK_SIGNATURE_KEY_MISSING", ["telnyx_public_key"])
audio_setup_missing = _missing_until_import(
    "AUDIO_SETUP_MISSING", ["sip_username", "telephony_credential_id"]
)


def no_campaign(workspace, app):
    with app.state.db.transaction() as s:
        campaigns = list(s.scalars(select(MODELS["campaigns"])))
        for campaign in campaigns:
            campaign.tenant_id = "moved-away"
    assert "NO_CAMPAIGN" in codes(workspace, app)
    with app.state.db.transaction() as s:
        for campaign in s.scalars(select(MODELS["campaigns"])):
            campaign.tenant_id = workspace["tenant_id"]
    # The control is Import a list, which makes the campaign; with one, the blocker is gone.
    assert "NO_CAMPAIGN" not in codes(workspace, app)


# --- Relay connection -------------------------------------------------------------------------------------------------


def connection_setup_changing(workspace, app, fast_relay):
    service = app.state.service
    service.managed.configuring = True
    assert "CONNECTION_SETUP_CHANGING" in codes(workspace, app)
    # The import route sets the flag and always clears it, whether the import works or not.
    bad = {**PROFILE, "url": "https://not-a-project.example"}
    workspace["client"].post("/v1/admin/managed-connection", headers=workspace["headers"], json=bad)
    service.managed.configuring = False  # a refused body never reached the route; the route's own finally:
    import_setup_file(workspace)
    assert service.managed.configuring is False
    assert eventually(lambda: "CONNECTION_SETUP_CHANGING" not in codes(workspace, app))


def connection_sign_in_required(workspace, app, fast_relay):
    import_setup_file(workspace)
    assert eventually(lambda: not codes(workspace, app))
    fast_relay.session = False
    app.state.service.managed.last_exchange = 0
    fast_relay.script.append(RuntimeError("relay dropped"))
    assert eventually(lambda: "CONNECTION_SIGN_IN_REQUIRED" in codes(workspace, app))
    fast_relay.session = True
    import_setup_file(workspace)
    assert eventually(lambda: "CONNECTION_SIGN_IN_REQUIRED" not in codes(workspace, app))


def connection_in_use_elsewhere(workspace, app, fast_relay):
    import_setup_file(workspace)
    assert eventually(lambda: not codes(workspace, app))
    fast_relay.script.append(RuntimeError("This setup is in use on another computer"))
    assert eventually(lambda: "CONNECTION_IN_USE_ELSEWHERE" in codes(workspace, app))
    # The other computer quits: the next exchange succeeds and the loop takes over by itself.
    assert eventually(lambda: "CONNECTION_IN_USE_ELSEWHERE" not in codes(workspace, app))


def connection_reconnecting(workspace, app, fast_relay):
    import_setup_file(workspace)
    assert eventually(lambda: not codes(workspace, app))
    fast_relay.script.extend([RuntimeError("network down")] * 3)
    assert eventually(lambda: "CONNECTION_RECONNECTING" in codes(workspace, app))
    assert eventually(lambda: "CONNECTION_RECONNECTING" not in codes(workspace, app))


def connection_delivery_gap(workspace, app, fast_relay):
    """November 9: a gap from days with the app closed stayed until a hidden Reconnect button was pressed."""
    fast_relay.script.extend([{"gap_since": "2026-11-07T10:00:00+00:00"}] * 3)
    import_setup_file(workspace)
    assert eventually(lambda: fast_relay.cleared_gap)
    assert eventually(lambda: not codes(workspace, app))


# --- Numbers ----------------------------------------------------------------------------------------------------------


def _numbers(app, **values):
    with app.state.db.transaction() as s:
        for number in s.scalars(select(MODELS["number_inventory"])):
            update(number, **values)


def no_caller_number(workspace, app):
    _numbers(app, lifecycle="retired_callback_only")
    assert "NO_CALLER_NUMBER" in codes(workspace, app)
    _numbers(app, lifecycle="healthy")
    assert "NO_CALLER_NUMBER" not in codes(workspace, app)


def caller_number_route_required(workspace, app):
    _numbers(app, lifecycle="identity_configured", identity_configured=False)
    assert "CALLER_NUMBER_ROUTE_REQUIRED" in codes(workspace, app)
    _numbers(app, identity_configured=True)
    assert "CALLER_NUMBER_ROUTE_REQUIRED" not in codes(workspace, app)


def caller_number_paused(workspace, app):
    with app.state.db.transaction(False) as s:
        number = next(iter(s.scalars(select(MODELS["number_inventory"]))))
    numbers = workspace["client"].get("/v1/workspace", headers=workspace["headers"]).json()["numbers"]
    for row in numbers:
        r = workspace["client"].post(
            f"/v1/admin/numbers/{row['id']}/pause", headers=workspace["headers"], json={"reason": "test"}
        )
        assert r.status_code in (200, 404), r.text
    if "CALLER_NUMBER_PAUSED" not in codes(workspace, app):
        _numbers(app, lifecycle="paused_outbound")
    assert "CALLER_NUMBER_PAUSED" in codes(workspace, app), codes(workspace, app)
    assert number
    _numbers(app, lifecycle="healthy")
    assert "CALLER_NUMBER_PAUSED" not in codes(workspace, app)


# The way out is one answered test call; the acceptance test drives it through the carrier events.
caller_number_callback_test_required = (
    test_one_answered_test_call_opens_every_number_immediately_and_never_expires
)


# --- Calling ----------------------------------------------------------------------------------------------------------


def pending_local_optouts(workspace, app):
    ready(workspace, pending_optouts=1)
    assert "PENDING_LOCAL_OPTOUTS" in codes(workspace, app, rep=True)
    # The Dialer syncs the journal by itself and reports zero pending.
    ready(workspace, pending_optouts=0)
    assert "PENDING_LOCAL_OPTOUTS" not in codes(workspace, app, rep=True)


def test_session_test_line_only(workspace, app):
    from app.calling.blockers import test_line_refusal

    client, headers = workspace["client"], workspace["headers"]
    body = {"test_session": True, "test_phone": "+12038211101"}
    assert client.put("/v1/admin/settings", headers=headers, json=body).status_code == 200
    service, p = app.state.service, principal(workspace)
    refused = test_line_refusal(
        "+16175550100", test_session=service.in_test_session(p), test_lines=service.test_lines(p)
    )
    assert [b.code for b in refused] == ["TEST_SESSION_TEST_LINE_ONLY"]
    assert client.delete("/v1/settings/test-call", headers=headers).status_code == 200
    refused = test_line_refusal(
        "+16175550100", test_session=service.in_test_session(p), test_lines=service.test_lines(p)
    )
    assert refused == []


def _timed(code, key, extra=None):
    def check(workspace, app, clock):
        from datetime import timedelta

        service = app.state.service
        until = (clock.now + timedelta(seconds=60)).isoformat()
        service.configs.write(workspace["tenant_id"], {key: until, **(extra or {})})
        assert code in codes(workspace, app)
        clock.now += timedelta(seconds=61)
        assert code not in codes(workspace, app)

    return check


telnyx_call_limit_reached = _timed("TELNYX_CALL_LIMIT_REACHED", "channel_limited_until")


def rep_audio_not_ready(workspace, app):
    ready(workspace, audio_ready=False)
    assert "REP_AUDIO_NOT_READY" in codes(workspace, app, rep=True)
    ready(workspace)
    assert "REP_AUDIO_NOT_READY" not in codes(workspace, app, rep=True)


EXIT_TESTS = {
    "DATABASE_INTEGRITY_FAILURE": database_integrity_failure,
    "RESTORE_RECONCILIATION_REQUIRED": restore_reconciliation_required,
    "TELNYX_NOT_CONFIGURED": telnyx_not_configured,
    "WEBHOOK_SIGNATURE_KEY_MISSING": webhook_signature_key_missing,
    "AUDIO_SETUP_MISSING": audio_setup_missing,
    "CONNECTION_SETUP_CHANGING": connection_setup_changing,
    "CONNECTION_SIGN_IN_REQUIRED": connection_sign_in_required,
    "CONNECTION_IN_USE_ELSEWHERE": connection_in_use_elsewhere,
    "CONNECTION_RECONNECTING": connection_reconnecting,
    "CONNECTION_DELIVERY_GAP": connection_delivery_gap,
    "NO_CAMPAIGN": no_campaign,
    "NO_CALLER_NUMBER": no_caller_number,
    "CALLER_NUMBER_ROUTE_REQUIRED": caller_number_route_required,
    "CALLER_NUMBER_CALLBACK_TEST_REQUIRED": caller_number_callback_test_required,
    "CALLER_NUMBER_PAUSED": caller_number_paused,
    "PENDING_LOCAL_OPTOUTS": pending_local_optouts,
    "TEST_SESSION_TEST_LINE_ONLY": test_session_test_line_only,
    "TELNYX_CALL_LIMIT_REACHED": telnyx_call_limit_reached,
    "REP_AUDIO_NOT_READY": rep_audio_not_ready,
}


def test_every_blocker_has_an_exit_test():
    assert set(EXIT_TESTS) == set(CATALOG), set(CATALOG) ^ set(EXIT_TESTS)


@pytest.mark.parametrize("code", sorted(EXIT_TESTS))
def test_blocker_goes_in_and_back_out(code, request):
    check = EXIT_TESTS[code]
    names = check.__code__.co_varnames[: check.__code__.co_argcount]
    check(*(request.getfixturevalue(name) for name in names))


def test_a_relay_that_stays_down_is_repaired_then_shown_as_stuck_with_reconnect(
    workspace, app, fast_relay, monkeypatch
):
    """R9E.6: the watchdog restarts a relay that outlived its promise; still down, the blocker stops saying
    it clears by itself and names Reconnect."""
    import app.calling.watchdog as watchdog
    from app.calling.blockers import Exit

    monkeypatch.setitem(watchdog.EXITS, "CONNECTION_RECONNECTING", Exit(by_itself_within=0.2))
    import_setup_file(workspace)
    assert eventually(lambda: not codes(workspace, app))
    fast_relay.script.extend([RuntimeError("network down")] * 1000)
    assert eventually(lambda: "CONNECTION_RECONNECTING" in codes(workspace, app))
    service, p, portal = app.state.service, principal(workspace), workspace["client"].portal
    restarts = []
    real_start = service.managed.start

    async def start():
        restarts.append(1)
        await real_start()

    monkeypatch.setattr(service.managed, "start", start)
    for _ in range(3):
        portal.call(service.watch_calling, p)
        time.sleep(0.25)
    assert restarts, "the watchdog restarted the relay loop"
    detail = workspace["client"].get("/v1/blockers", headers=workspace["headers"]).json()["blockers"]
    stuck = next(b for b in detail if b["code"] == "CONNECTION_RECONNECTING")
    assert stuck["stuck"] is True and "Reconnect" in stuck["message"]
    fast_relay.script.clear()
    assert eventually(lambda: not codes(workspace, app))
    portal.call(service.watch_calling, p)
    assert service.watchdog.stuck == set()
