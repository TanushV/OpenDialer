"""R9E.4: seeded random runs of the calling loop with bad conditions mixed in; the calling laws hold after every step.

Each run starts from the simulated workspace and takes random steps: go ready or pause, dial, answer, either side
hangs up, save an outcome, opt a number out, deliver an event twice or late, and jump the clock by minutes to a day.
After every step:
- no request fails with a server error;
- the rep never has two calls open at once (G2);
- a number opted out is never dialed again;
- a paused rep is never offered a lead (Pause is always honored);
and at the end, a plain recovery (end the call, save its outcome, go ready) always leaves calling either able to dial
or saying in plain words why not (G3, G10). A failing seed is a regression test: keep it in REGRESSION_SEEDS.

SOAK_RUNS sets how many seeds run (CI runs the default; run thousands locally before a release).
"""

import os
import random
from datetime import timedelta

import pytest
from conftest import ready
from helpers import disposition_body

pytestmark = pytest.mark.slow

RUNS = int(os.environ.get("SOAK_RUNS", "25"))
REGRESSION_SEEDS = []
STEPS = 50
OUTCOMES = ["no_answer", "not_interested", "voicemail", "callback_requested", "gatekeeper"]
TERMINAL = {"ENDED", "FINALIZED"}  # ENDED: hung up, waiting for its outcome; FINALIZED: saved


class Run:
    def __init__(self, workspace, carrier, clock, seed):
        self.ws, self.carrier, self.clock = workspace, carrier, clock
        self.rng = random.Random(seed)
        self.client, self.h = workspace["client"], workspace["headers"]
        self.call = None  # the rep's current call, as last read
        self.events = []  # (event_id, event_type, call_id, payload) delivered so far
        self.opted_out = {}  # phone -> number of dials before the opt-out
        self.paused = True
        self.keys = 0

    def request(self, method, path, **kwargs):
        r = self.client.request(method, path, headers=self.h, **kwargs)
        assert r.status_code < 500, f"{method} {path} -> {r.status_code}: {r.text[:300]}"
        return r

    def read_call(self):
        if self.call:
            self.call = self.request("GET", f"/v1/calls/{self.call['id']}").json()
        return self.call

    def open_call(self):
        call = self.read_call()
        return call if call and call.get("state") not in TERMINAL else None

    def event(self, event_type, payload=None, *, event_id=None, call=None):
        call = call or self.call
        if not call:
            return
        event_id = event_id or f"{call['id']}-{event_type}-{len(self.events)}"
        body = {
            "event_id": event_id,
            "event_type": event_type,
            "call_id": call["id"],
            "payload": payload or {},
        }
        self.request("POST", "/v1/simulation/event", json=body)
        self.events.append(body)

    # --- steps --------------------------------------------------------------------------------------------------------

    def go_ready(self):
        ready(self.ws)
        self.paused = False

    def pause(self):
        self.request(
            "POST",
            "/v1/rep/presence",
            json={"status": "paused", "audio_ready": True, "telephony_ready": False},
        )
        if not self.open_call():
            self.request("POST", "/v1/queue/release", json={})
        self.paused = True
        # Pause is always honored: no lead is offered to a paused rep.
        offered = self.request("POST", "/v1/queue/next", json={"campaign_id": self.ws["campaign_id"]})
        assert offered.status_code == 409, f"a paused rep was offered a lead: {offered.text[:200]}"

    def dial(self):
        if self.open_call():
            return
        self.go_ready()
        offered = self.request("POST", "/v1/queue/next", json={"campaign_id": self.ws["campaign_id"]})
        if offered.status_code != 200:
            detail = offered.json()["detail"]
            message = detail.get("message") if isinstance(detail, dict) else detail
            assert message, f"a refusal with no reason: {offered.text[:200]}"
            return
        self.keys += 1
        started = self.request(
            "POST",
            "/v1/calls/start",
            json={"reservation_id": offered.json()["reservation_id"], "idempotency_key": f"soak-{self.keys}"},
        )
        if started.status_code == 200:
            self.call = started.json()

    def answer(self):
        if self.open_call():
            self.event("call.answered")

    def prospect_hangs_up(self):
        if self.open_call():
            self.event("call.hangup", {"hangup_cause": "normal_clearing", "hangup_source": "callee"})

    def rep_hangs_up(self):
        if self.open_call():
            self.request("POST", f"/v1/calls/{self.call['id']}/hangup")
            self.event("call.hangup", {"hangup_cause": "normal_clearing", "hangup_source": "caller"})

    def save(self):
        call = self.read_call()
        if call and call.get("state") in TERMINAL and not call.get("outcome"):
            body = disposition_body(self.client, self.h, call["id"], outcome=self.rng.choice(OUTCOMES))
            self.request("POST", f"/v1/calls/{call['id']}/disposition", json=body)

    def opt_out(self):
        leads = self.request("GET", "/v1/workspace/leads?filter=active&q=").json()["items"]
        if leads:
            phone = self.rng.choice(leads)["phone"]
            self.request("POST", "/v1/suppressions", json={"phone": phone, "reason": "opt_out"})
            self.opted_out.setdefault(phone, len(self.carrier.dials))

    def duplicate_event(self):
        if self.events:
            again = dict(self.rng.choice(self.events))
            self.request("POST", "/v1/simulation/event", json=again)

    def late_event(self):
        if self.events and self.rng.random() < 0.5:
            old = self.rng.choice(self.events)
            body = {**old, "event_id": old["event_id"] + "-late", "event_type": "call.answered"}
            self.request("POST", "/v1/simulation/event", json=body)

    def time_jump(self):
        self.clock.now += timedelta(minutes=self.rng.choice([1, 5, 30, 120, 60 * 13, 60 * 26]))
        # The session would lapse over a long jump; sign in again as the desktop does.
        token = self.client.post("/v1/local/session", headers={"X-Local-Token": "test-local-secret"}, json={})
        assert token.status_code == 200, token.text
        self.h = {"Authorization": f"Bearer {token.json()['access_token']}"}
        self.ws["headers"] = self.h

    STEPS = [
        go_ready,
        pause,
        dial,
        dial,
        answer,
        prospect_hangs_up,
        rep_hangs_up,
        save,
        save,
        opt_out,
        duplicate_event,
        late_event,
        time_jump,
    ]

    # --- laws ---------------------------------------------------------------------------------------------------------

    def check(self):
        calls = self.request("GET", "/v1/workspace/calls?filter=all&q=").json()["items"]
        open_calls = [c for c in calls if c.get("state") not in TERMINAL]
        assert len(open_calls) <= 1, f"two calls open at once: {[c['state'] for c in open_calls]}"
        for phone, before in self.opted_out.items():
            after = [d for d in self.carrier.dials[before:] if d.get("to") == phone]
            assert not after, f"{phone} was dialed after it opted out"

    def recover(self):
        """The plainest way back: end the call, save its outcome, go ready. Calling must then dial or say why."""
        if self.open_call():
            self.rep_hangs_up()
        self.save()
        self.go_ready()
        readiness = self.request("GET", "/v1/calling/readiness").json()
        assert readiness["blockers"] or readiness["callable_now"] or readiness["why_none"], readiness
        offered = self.request("POST", "/v1/queue/next", json={"campaign_id": self.ws["campaign_id"]})
        if offered.status_code != 200:
            detail = offered.json()["detail"]
            message = detail.get("message") if isinstance(detail, dict) else detail
            assert message, f"stuck with no reason after recovery: {offered.text[:200]}"


@pytest.mark.parametrize("seed", [*REGRESSION_SEEDS, *range(RUNS)])
def test_calling_laws_hold_under_random_faults(seed, workspace, carrier, clock):
    rows = [
        {"name": f"Soak lead {i}", "phone": f"+1{area}55501{i:02d}", "company": f"Soak Co {i}"}
        for i, area in enumerate(["415", "212", "312", "512", "617"] * 3)
    ]
    imported = workspace["client"].post(
        "/v1/leads/import",
        headers=workspace["headers"],
        json={"campaign_id": workspace["campaign_id"], "list_name": "Soak", "rows": rows, "prepare": False},
    )
    assert imported.status_code == 200, imported.text
    run = Run(workspace, carrier, clock, seed)
    for _ in range(STEPS):
        step = run.rng.choice(Run.STEPS)
        step(run)
        run.check()
    run.recover()
    run.check()
    if os.environ.get("SOAK_STATS"):
        calls = run.request("GET", "/v1/workspace/calls?filter=all&q=").json()["items"]
        print(
            f"SOAKSTAT dials={len(carrier.dials)} calls={len(calls)} saved={sum(bool(c.get('outcome')) for c in calls)} "
            f"answered={sum(c.get('answered_at') is not None for c in calls)} optouts={len(run.opted_out)}"
        )
