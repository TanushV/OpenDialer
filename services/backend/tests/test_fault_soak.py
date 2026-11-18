"""R9E.4: seeded random runs of the calling loop with bad conditions mixed in; the calling laws hold after every step.

Each run starts from the simulated workspace and takes random steps: go ready or pause, dial, answer, either side
hangs up, save an outcome, opt a number out, deliver an event twice or late, and jump the clock by minutes to a day.
After every step:
- no request fails with a server error;
- only a fresh dial action may submit one call to the carrier;
- the rep never has two calls open at once (G2);
- a number opted out is never dialed again;
- a paused rep is never offered a lead (Pause is always honored);
and at the end, a plain recovery (end the call, save its outcome, go ready) always leaves calling either able to dial
or saying in plain words why not (G3, G10). A failing seed is a regression test: keep it in REGRESSION_SEEDS.

SOAK_RUNS and SOAK_STEPS size the run. SOAK_SEED replays a single sequence.
"""

import os
import random
from datetime import timedelta

import pytest
from conftest import ready
from helpers import disposition_body

from services.backend.tests.stress_support import StressTrace, soak_count, soak_seeds

pytestmark = pytest.mark.slow

REGRESSION_SEEDS = []
STEPS = soak_count("SOAK_STEPS", 50)
OUTCOMES = ["no_answer", "not_interested", "voicemail", "callback_requested", "gatekeeper"]
TERMINAL = {"ENDED", "FINALIZED"}  # ENDED: hung up, waiting for its outcome; FINALIZED: saved


class Run:
    def __init__(self, workspace, carrier, clock, seed, trace):
        self.ws, self.carrier, self.clock = workspace, carrier, clock
        self.rng = random.Random(seed)
        self.trace = trace
        self.client, self.h = workspace["client"], workspace["headers"]
        self.call = None  # the rep's current call, as last read
        self.events = []  # (event_id, event_type, call_id, payload) delivered so far
        self.opted_out = {}  # phone -> number of dials before the opt-out
        self.keys = 0
        self.submissions = []  # Count sends before the replay carrier deduplicates them.
        self.last_start = None
        self.queue = {"campaign_id": workspace["campaign_id"], "list_names": ["Soak"]}

    def request(self, method, path, **kwargs):
        if method != "GET":
            self.trace.record("request", method=method, path=path, body=kwargs.get("json"))
        r = self.client.request(method, path, headers=self.h, **kwargs)
        if method != "GET" or not r.is_success:
            self.trace.record(
                "response", path=path, status=r.status_code, detail=r.text[:400] if not r.is_success else None
            )
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

    def pause(self):
        self.request(
            "POST",
            "/v1/rep/presence",
            json={"status": "paused", "audio_ready": True, "telephony_ready": False},
        )
        if not self.open_call():
            self.request("POST", "/v1/queue/release", json={})
        # Pause is always honored: no lead is offered to a paused rep.
        offered = self.request("POST", "/v1/queue/next", json=self.queue)
        assert offered.status_code == 409, f"a paused rep was offered a lead: {offered.text[:200]}"

    def dial(self):
        if self.open_call():
            return
        self.go_ready()
        offered = self.request("POST", "/v1/queue/next", json=self.queue)
        if offered.status_code != 200:
            detail = offered.json()["detail"]
            message = detail.get("message") if isinstance(detail, dict) else detail
            assert message, f"a refusal with no reason: {offered.text[:200]}"
            return
        self.keys += 1
        data = {"reservation_id": offered.json()["reservation_id"], "idempotency_key": f"soak-{self.keys}"}
        started = self.request("POST", "/v1/calls/start", json=data)
        if started.status_code == 200:
            self.call = started.json()
            self.last_start = data, self.call["id"]

    def retry_start(self):
        if self.last_start:
            data, original = self.last_start
            retried = self.request("POST", "/v1/calls/start", json=data)
            assert retried.is_success and retried.json()["id"] == original, (
                "Retry lost the original dial intent"
            )

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
        phones = sorted({lead["phone"] for lead in leads if lead.get("phone")} - self.opted_out.keys())
        if phones:
            phone = self.rng.choice(phones)
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
        self.trace.record("clock", now=self.clock.now.isoformat())
        # The session would lapse over a long jump; sign in again as the desktop does.
        token = self.client.post("/v1/local/session", headers={"X-Local-Token": "test-local-secret"}, json={})
        assert token.status_code == 200, token.text
        self.h = {"Authorization": f"Bearer {token.json()['access_token']}"}
        self.ws["headers"] = self.h

    def maintenance(self):
        self.request("POST", "/v1/admin/recovery")

    STEPS = [
        go_ready,
        pause,
        dial,
        dial,
        retry_start,
        answer,
        prospect_hangs_up,
        rep_hangs_up,
        save,
        save,
        opt_out,
        duplicate_event,
        late_event,
        time_jump,
        maintenance,
    ]

    # --- laws ---------------------------------------------------------------------------------------------------------

    def check(self):
        assert len(self.submissions) <= self.keys, "Carrier received more dials than the rep requested"
        assert len(set(self.submissions)) == len(self.submissions), "An original dial was submitted again"
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
        offered = self.request("POST", "/v1/queue/next", json=self.queue)
        if offered.status_code != 200:
            detail = offered.json()["detail"]
            message = detail.get("message") if isinstance(detail, dict) else detail
            assert message, f"stuck with no reason after recovery: {offered.text[:200]}"


@pytest.mark.parametrize("seed", soak_seeds("SOAK_RUNS", 25, REGRESSION_SEEDS))
def test_calling_laws_hold_under_random_faults(
    seed, workspace, carrier, clock, request, tmp_path, monkeypatch
):
    count = min(90, max(15, STEPS // 4))
    rows = [
        {"name": f"Soak lead {i}", "phone": f"+1{area}55501{i:02d}", "company": f"Soak Co {i}"}
        for i, area in enumerate(["415", "212", "312", "512", "617"] * 18)
        if i < count
    ]
    with StressTrace(tmp_path, request.node.nodeid, seed, SOAK_STEPS=STEPS) as trace:
        trace.record("import fixture list", size=count)
        imported = workspace["client"].post(
            "/v1/leads/import",
            headers=workspace["headers"],
            json={
                "campaign_id": workspace["campaign_id"],
                "list_name": "Soak",
                "rows": rows,
                "prepare": False,
            },
        )
        assert imported.status_code == 200, imported.text
        run = Run(workspace, carrier, clock, seed, trace)
        dial = carrier.dial

        async def observe_dial(payload):
            run.submissions.append(payload["command_id"])
            trace.record("carrier dial", command=payload["command_id"], phone=payload["to"])
            return await dial(payload)

        monkeypatch.setattr(carrier, "dial", observe_dial)
        schedule = random.Random(seed)
        for index in range(STEPS):
            step = schedule.choice(Run.STEPS)
            run.rng.seed(schedule.getrandbits(64))
            trace.record("choose", number=index + 1, operation=step.__name__, now=clock.now.isoformat())
            submitted = len(run.submissions)
            step(run)
            assert len(run.submissions) - submitted <= int(step is Run.dial), (
                f"{step.__name__} sent a carrier dial without a fresh dial action for it"
            )
            run.check()
        trace.record("recover")
        submitted = len(run.submissions)
        run.recover()
        assert len(run.submissions) == submitted, "Recovering the workspace submitted another dial"
        run.check()
    if os.environ.get("SOAK_STATS"):
        calls = run.request("GET", "/v1/workspace/calls?filter=all&q=").json()["items"]
        print(
            f"SOAKSTAT dials={len(carrier.dials)} calls={len(calls)} saved={sum(bool(c.get('outcome')) for c in calls)} "
            f"answered={sum(c.get('answered_at') is not None for c in calls)} optouts={len(run.opted_out)}"
        )
