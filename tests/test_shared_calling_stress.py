"""Generate overlapping calling faults across two authorities and the real pool RPCs."""

import asyncio
import json
import random
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic
from types import SimpleNamespace

import httpx
import pytest
from app.main import create_app
from app.managed_webhooks import deliver
from app.platform.config import Settings
from app.providers.telnyx import TelnyxCarrier
from fastapi.testclient import TestClient
from relay_test_support import (
    INSTALL,
    NUMBER,
    POOL,
    SPARE,
    A,
    B,
    RelayRPC,
    event_owner,
    inbound,
    quote_json,
    receipt,
    shared_hold,
)
from relay_test_support import pool_db as pool_db
from relay_test_support import relay_db as relay_db
from relay_test_support import shared_db as shared_db

from services.backend.tests.carrier_http import CarrierHTTP
from services.backend.tests.stress_support import StressTrace, soak_count, soak_seeds

pytestmark = pytest.mark.slow
EVENTS = soak_count("SOAK_SHARED_EVENTS", 12)
SEATS = {A: ("rep-a", "+14155550141"), B: ("rep-b", "+14155550142")}


class Desktop:
    def __init__(self, sql, seat, directory, adapter, clock, trace):
        self.sql, self.seat, self.clock, self.trace = sql, seat, clock, trace
        self.sip, self.phone = SEATS[seat]
        self.settings = Settings(
            data_dir=directory,
            database_url=f"sqlite:///{directory}/opendialer.db",
            worker_enabled=False,
            simulation=True,
            local_token="shared-stress-fixture",
        )
        self.adapter = adapter
        self.stack = ExitStack()
        self.scope = None
        self.acks = []

    def reopen(self):
        self.stack.close()
        self.trace.record("open desktop", rep=self.sip, restart=self.scope is not None)
        app = create_app(self.settings, clock=lambda: self.clock.now, carrier=self.adapter)
        self.stack.callback(app.state.db.engine.dispose)
        self.client = self.stack.enter_context(TestClient(app))
        self.service = app.state.service
        self.service.require_prepared_scripts = False
        self.client.headers["X-Local-Token"] = "shared-stress-fixture"
        if self.scope is None:
            auth = self.api("POST", "/v1/setup", {"seller_name": self.sip, "campaign_name": "Stress"})
        else:
            auth = self.api("POST", "/v1/local/session")
        self.client.headers["Authorization"] = "Bearer " + auth["access_token"]
        if self.scope is None:
            self.scope = auth["tenant_id"]
            seeded = self.api("POST", "/v1/simulation/seed")
            self.queue = {"campaign_id": seeded["campaign_id"], "list_names": ["Stress"]}
            self.api(
                "POST",
                "/v1/leads/import",
                {
                    "campaign_id": self.queue["campaign_id"],
                    "list_name": "Stress",
                    "prepare": False,
                    "rows": [{"name": f"Prospect of {self.sip}", "phone": self.phone, "company": self.sip}],
                },
            )
            self.service.configs.write(
                self.scope,
                {
                    "telnyx_connection_id": "app-a",
                    "sip_username": self.sip,
                    "telephony_credential_id": self.sip,
                },
            )
        managed = self.service.managed
        managed.client = RelayRPC(self.sql, self.seat)
        self.stack.callback(setattr, managed, "client", None)
        managed.config = {"tenant_id": self.scope, "store_id": self.seat, "workspace_id": self.seat}
        managed.installation = INSTALL
        self.exchange()

    def api(self, method, path, body=None):
        if method != "GET":
            self.trace.record("desktop request", rep=self.sip, method=method, path=path, body=body)
        response = self.client.request(method, path, json=body)
        assert response.is_success, (self.sip, method, path, response.status_code, response.text[:500])
        return response.json()

    def exchange(self, *, reachable=False, repeat=False):
        batch = json.loads(
            self.sql(
                f"select relay_exchange('{self.seat}','{INSTALL}',{quote_json(self.acks)}::jsonb,'',{str(reachable).lower()});",
                seat=self.seat,
            )
        )
        self.trace.record(
            "relay exchange", rep=self.sip, events=[row["event_id"] for row in batch["events"]], repeat=repeat
        )
        self.service.managed.apply_pool_snapshot(self.scope, batch["pool"])
        for row in batch["events"]:
            # The relay lease uses wall time. The authorities use the same logical time for receipt handling.
            row["received_at"] = self.clock.now.isoformat()
        for _ in range(2 if repeat else 1):
            self.acks = self.client.portal.call(deliver, self.service, self.scope, batch["events"])
        self.service.managed.last_exchange = monotonic()
        self.service.managed.caught_up = True
        self.api("POST", "/v1/admin/jobs/run")

    def reserve(self, emit, carrier):
        agent = self.api("POST", "/v1/telephony/agent")["agent_call_control_id"]
        emit(self, agent, "call.answered", carrier.dials[agent])
        self.exchange()
        assert self.api("GET", f"/v1/telephony/agent/{agent}")["state"] == "answered"
        self.api(
            "POST",
            "/v1/rep/presence",
            {
                "status": "ready",
                "audio_ready": True,
                "telephony_ready": True,
                "agent_call_control_id": agent,
            },
        )
        hold = self.api("POST", "/v1/queue/next", self.queue)
        assert hold["lead"]["phone"] == self.phone and hold["number"]["phone"] == NUMBER, hold
        return agent, {"reservation_id": hold["reservation_id"], "idempotency_key": self.sip + "-only-intent"}


@pytest.mark.parametrize("seed", soak_seeds("SOAK_SHARED_RUNS", 6))
def test_shared_calling_randomized_faults(seed, shared_db, tmp_path, request):
    sql = shared_db
    rng = random.Random(seed)
    seats = list(SEATS)
    rng.shuffle(seats)
    clock = SimpleNamespace(now=datetime(2026, 11, 12, 18, tzinfo=UTC))  # Not a federal holiday.
    carrier = CarrierHTTP({sip: sip for sip, _ in SEATS.values()})
    accepted, release = Event(), Event()
    with (
        StressTrace(
            tmp_path, request.node.nodeid, seed, SOAK_SHARED_EVENTS=EVENTS, OPENDIALER_REQUIRE_RELAY_TESTS=1
        ) as trace,
        ExitStack() as stack,
    ):
        sql(f"update relay_private.number_pools set fallback_enabled=true where id='{POOL}';")
        sql(
            f"update relay_private.number_pool_numbers set daily_limit=2,outbound_enabled=(phone='{NUMBER}');"
        )
        # Leave just the two permitted outbound slots on one caller ID.
        assert (
            sql(f"select outbound_enabled from relay_private.number_pool_numbers where phone='{SPARE}';")
            == "f"
        )

        async def transport(incoming):
            path = incoming.url.path
            payload = json.loads(incoming.content) if incoming.content else {}
            trace.record(
                "carrier request",
                method=incoming.method,
                path=path,
                command=payload.get("command_id"),
                destination=payload.get("to"),
            )
            response = carrier(incoming)
            if incoming.method == "POST" and path == "/v2/calls" and payload["to"] == SEATS[seats[0]][1]:
                assert not accepted.is_set(), "A second request reached the carrier for the held dial"
                trace.record(
                    "carrier accepted with reply held", leg=response.json()["data"]["call_control_id"]
                )
                accepted.set()
                assert await asyncio.to_thread(release.wait, 45), "The held dial reply was never released"
                trace.record("carrier dial reply lost")
                raise httpx.ReadTimeout("Accepted but the reply was lost", request=incoming)
            return response

        http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        stack.callback(lambda: asyncio.run(http.aclose()))
        adapter = TelnyxCarrier("stress-fixture", "app-a", http=http)
        a, b = [Desktop(sql, seat, tmp_path / SEATS[seat][0], adapter, clock, trace) for seat in seats]
        for rep in (a, b):
            rep.settings.data_dir.mkdir()
            stack.callback(rep.stack.close)
            rep.reopen()

        def emit(rep, leg, kind, payload, *, suffix=""):
            tag = f"{leg}-{kind}{suffix}"
            result = receipt(
                sql,
                tag,
                kind,
                {
                    **payload,
                    "call_control_id": leg,
                    "direction": "outgoing",
                    "connection_id": "app-a",
                },
            )
            owner = event_owner(sql, tag)
            trace.record("carrier event", event=tag, result=result, expected_rep=rep.sip, owner=owner)
            assert result in {"received", "duplicate"}, (tag, result)
            assert owner == rep.seat, f"{tag} routed to {owner}, expected {rep.seat}"

        agent_a, intent_a = a.reserve(emit, carrier)
        agent_b, intent_b = b.reserve(emit, carrier)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(a.api, "POST", "/v1/calls/start", intent_a)
            try:
                if not accepted.wait(15):
                    pending.result(timeout=1)  # Report a refused dial before the gate when available.
                    pytest.fail("The original dial never reached the carrier gate")
                (old_leg,) = [leg for leg, payload in carrier.dials.items() if payload["to"] == a.phone]
                original = carrier.dials[old_leg]["command_id"]
                old_path = f"/v1/calls/{original}"
                next_call = {}

                def contend():
                    result = shared_hold(sql, b.seat, "overlapping-prospect", a.phone)
                    trace.record("other rep tried same prospect", rep=b.sip, result=result)
                    assert result == {"reason": "busy"}, result

                def retry():
                    duplicate = a.api("POST", "/v1/calls/start", intent_a)
                    assert duplicate["id"] == original, duplicate["id"]

                actions = [
                    ("stop original", lambda: a.api("POST", old_path + "/hangup")),
                    ("pause original rep", lambda: a.api("POST", "/v1/rep/presence", {"status": "paused"})),
                    ("retry original start", retry),
                    ("contend for original prospect", contend),
                    (
                        "other rep starts a different call",
                        lambda: next_call.update(b.api("POST", "/v1/calls/start", intent_b)),
                    ),
                ]
                rng.shuffle(actions)
                for name, action in actions:
                    trace.record("scheduled while reply held", choice=name)
                    action()
            finally:
                release.set()
            assert pending.result(timeout=15)["state"] == "ENDING"

        next_leg = next_call["provider_id"]
        next_path = f"/v1/calls/{next_call['id']}"
        emit(b, next_leg, "call.answered", carrier.dials[next_leg])
        b.exchange()
        assert b.api("GET", next_path)["state"] == "ANSWERED_UNCLASSIFIED"
        mailbox = rng.choice([False, True])
        if mailbox:
            emit(
                b,
                next_leg,
                "call.machine.premium.greeting.ended",
                {
                    **carrier.dials[next_leg],
                    "result": "beep_detected",
                },
            )
            b.exchange()
        next_state = "ANSWERED_UNCLASSIFIED"  # No detection: a beep receipt changes nothing (work package 6).
        carrier.live.remove(agent_a)
        a.reopen()  # Backend goes away with the stop recorded and no carrier reference.
        for seconds in rng.choice([(), (150,), (150, 301)]):
            clock.now += timedelta(seconds=seconds)
            trace.record("advance missing-receipt deadline", seconds=seconds, now=clock.now.isoformat())
            a.api("POST", "/v1/admin/recovery")

        ended_first = rng.choice([False, True])
        if ended_first:
            carrier.live.remove(old_leg)
            emit(a, old_leg, "call.hangup", carrier.dials[old_leg])
            a.exchange(repeat=True)
        else:
            carrier.lose_hangup_response_for = old_leg

        backlog = [
            (a, old_leg, "call.answered", {}),
            (a, old_leg, "call.ringing", {}),
            (a, old_leg, "call.machine.premium.greeting.ended", {"result": "beep_detected"}),
            (a, agent_a, "call.hangup", {}),
            (b, next_leg, "call.answered", {}),
            (b, next_leg, "call.ringing", {}),
        ]
        tape = backlog + rng.choices(backlog, k=EVENTS)
        rng.shuffle(tape)
        for index, (rep, leg, kind, facts) in enumerate(tape, 1):
            emit(rep, leg, kind, {**carrier.dials[leg], **facts})
            if index % 5 == 0:
                rep.exchange(repeat=rng.choice([True, False]))
            assert carrier.live.issuperset({agent_b, next_leg}), "A late event ended the other rep's call"
        a.exchange(repeat=True)
        b.exchange(repeat=True)
        for _ in range(4):
            clock.now += timedelta(seconds=31)
            a.api("POST", "/v1/admin/recovery")
        assert carrier.live == {agent_b, next_leg}, f"Orphan leg after recovery: {sorted(carrier.live)}"
        assert b.api("GET", next_path)["state"] == next_state
        assert a.api("GET", old_path)["state"] == "ENDED"
        stops = [
            (path, body["command_id"])
            for method, path, body in carrier.requests
            if method == "POST" and path.endswith("/actions/hangup")
        ]
        assert stops == (
            [] if ended_first else [(f"/calls/{old_leg}/actions/hangup", original + ":hangup")]
        ), stops
        carrier.dial_budget(2)

        b.api("POST", next_path + "/hangup", {"mark_voicemail": True} if mailbox else None)
        emit(b, next_leg, "call.hangup", carrier.dials[next_leg])
        b.exchange()
        saved = b.api("GET", next_path)
        # End as voicemail leaves the outcome to the rep: a message left or not (work package 6.12).
        assert (saved["state"], saved["outcome"]) == ("ENDED", None)
        b.api("POST", "/v1/rep/presence", {"status": "paused"})
        b.api("POST", "/v1/rep/reachable", {"registered": True})
        b.exchange(reachable=True)
        at_cap = shared_hold(sql, a.seat, "at-cap", "+14155550143")
        trace.record("outbound at cap", result=at_cap)
        assert at_cap["reason"] == "capacity" and not at_cap.get("submitted"), at_cap
        carrier.incoming.add("inbound-stress-return")
        carrier.live.add("inbound-stress-return")
        owner = inbound(sql, "stress-return", caller=b.phone)
        trace.record("callback at outbound cap", owner=owner, expected=b.seat, prospect=b.phone)
        assert owner == [b.seat, next_call["id"]]
        for rep in (a, b):
            rep.exchange(reachable=rep is b, repeat=True)
            tasks = [item for item in rep.api("GET", "/v1/workspace/tasks")["items"] if item.get("inbound")]
            assert len(tasks) == int(rep is b), (rep.sip, tasks)
            if tasks:
                assert (tasks[0]["previous_call_id"], tasks[0]["lead_name"], tasks[0]["route"]) == (
                    next_call["id"],
                    f"Prospect of {b.sip}",
                    "rep",
                )
        b.api("POST", "/v1/admin/control/run")
        targets = [
            body["to"]
            for method, path, body in carrier.requests
            if method == "POST" and path.endswith("/actions/transfer")
        ]
        assert targets == [f"sip:{b.sip}@sip.telnyx.com"], targets
        carrier.dial_budget(2)
