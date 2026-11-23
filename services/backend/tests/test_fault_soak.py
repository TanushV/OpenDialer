"""R9E.4: seeded random runs of the calling loop with bad conditions mixed in; the calling laws hold after every step.

Each run starts from the simulated workspace and takes random steps: go ready or pause, dial, answer, either side
hangs up, save an outcome, opt a number out, deliver an event twice or late, and jump the clock by minutes to a day.
After every step:
- no request fails with a server error;
- only a fresh dial action may submit one call to the carrier;
- the rep never has two calls open at once (G2);
- a number opted out is never dialed again;
- a paused rep is never offered a lead (Pause is always honored);
- an outreach dial happens only Monday to Friday, 9:00 to 17:00 in the lead's zone, outside US federal holidays,
  checked here from the area code and a fixed holiday list, independently of the product's own rule;
- an End as voicemail within 7 seconds of answer never reaches the carrier before 7 seconds after answer;
- a late detection receipt never causes a hang-up, and practice speech on a test call never adds a hold;
- no transcript text remains for a call that has ended;
and at the end, a plain recovery (end the call, save its outcome, go ready) always leaves calling either able to dial
or saying in plain words why not (G3, G10). A failing seed is a regression test: keep it in REGRESSION_SEEDS.

SOAK_RUNS and SOAK_STEPS size the run. SOAK_SEED replays a single sequence.
"""

import os
import random
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import ready
from helpers import disposition_body

from services.backend.tests.stress_support import StressTrace, soak_count, soak_seeds

pytestmark = pytest.mark.slow

REGRESSION_SEEDS = []
STEPS = soak_count("SOAK_STEPS", 50)
TEST_PHONES = ["+12025550185", "+12025550186", "+12025550187"]
OUTCOMES = ["no_answer", "not_interested", "voicemail", "callback_requested", "gatekeeper"]
TERMINAL = {"ENDED", "FINALIZED"}  # ENDED: hung up, waiting for its outcome; FINALIZED: saved
# Independent of the product: the fixture's area codes and the 2026-2027 US federal holidays as observed.
AREA_ZONES = {
    "415": "America/Los_Angeles",
    "212": "America/New_York",
    "312": "America/Chicago",
    "512": "America/Chicago",
    "617": "America/New_York",
}
HOLIDAYS = {
    date.fromisoformat(day)
    for day in (
        "2026-01-01 2026-01-19 2026-02-16 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-10-12 2026-11-11 "
        "2026-11-26 2026-12-25 2027-01-01 2027-01-18 2027-02-15 2027-05-31 2027-06-18 2027-07-05 2027-09-06 "
        "2027-10-11 2027-11-11 2027-11-25 2027-12-24"
    ).split()
}
HOLD = timedelta(seconds=7)
SPEECH = ["Stop calling me, take me off your list.", "We have no budget this year.", "Who is this?"]


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
        self.hangups = []  # (provider_id, time) of every carrier stop, before any deduplication.
        self.held = {}  # provider_id -> answered_at for an End as voicemail inside the short-call window.
        self.coverage = {}  # Effects this run actually reached, so a vacuous run cannot pass.
        self.violations = []  # Laws broken inside carrier calls, asserted by `check`.
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

    def offer(self):
        return self.request("POST", "/v1/queue/next", json=self.queue)

    def pause(self):
        self.request(
            "POST",
            "/v1/rep/presence",
            json={"status": "paused", "audio_ready": True, "telephony_ready": False},
        )
        if not self.open_call():
            self.request("POST", "/v1/queue/release", json={})
        # Pause is always honored: no lead is offered to a paused rep.
        offered = self.offer()
        assert offered.status_code == 409, f"a paused rep was offered a lead: {offered.text[:200]}"

    def dial(self):
        if self.open_call():
            return
        self.go_ready()
        offered = self.offer()
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
            if self.rng.random() < 0.5:
                self.speak()  # Speech usually follows an answer; keeps long runs from being vacuous.

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
            if self.request("POST", f"/v1/calls/{call['id']}/disposition", json=body).is_success:
                self.coverage["saved"] = self.coverage.get("saved", 0) + 1

    def opt_out(self):
        leads = self.request("GET", "/v1/workspace/leads?filter=active&q=").json()["items"]
        phones = sorted({lead["phone"] for lead in leads if lead.get("phone")} - self.opted_out.keys())
        if phones:
            phone = self.rng.choice(phones)
            self.request("POST", "/v1/suppressions", json={"phone": phone, "reason": "opt_out"})
            self.opted_out.setdefault(phone, len(self.carrier.dials))
            self.coverage["opt_out"] = len(self.opted_out)

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

    def business_hours(self):
        """Move to the next time every fixture zone is open (12:00 to 16:00 New York, a working weekday)."""
        eastern = self.clock.now.astimezone(ZoneInfo("America/New_York"))
        day = eastern.date()
        if eastern.hour >= 16:
            day += timedelta(days=1)
        while day.weekday() >= 5 or day in HOLIDAYS:
            day += timedelta(days=1)
        start = datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo("America/New_York"))
        target = max(start, eastern) + timedelta(minutes=self.rng.randrange(0, 180))
        if target > self.clock.now:
            self.clock.now = target.astimezone(UTC)  # The product's clock is always UTC.
            self.trace.record("clock", now=self.clock.now.isoformat())
            self.time_jump_session()

    def time_jump_session(self):
        token = self.client.post("/v1/local/session", headers={"X-Local-Token": "test-local-secret"}, json={})
        assert token.status_code == 200, token.text
        self.h = {"Authorization": f"Bearer {token.json()['access_token']}"}
        self.ws["headers"] = self.h

    def answered_call(self):
        call = self.open_call()
        return call if call and call.get("answered_at") and call.get("state") != "ENDING" else None

    def end_as_voicemail(self):
        """Work package 6.10: the rep ends a mailbox; inside 7 seconds the carrier stop must wait."""
        call = self.answered_call()
        if not call:
            return
        answered = datetime.fromisoformat(call["answered_at"])
        ended = self.request("POST", f"/v1/calls/{call['id']}/hangup", json={"mark_voicemail": True})
        # Independent of the server's answer: a press inside 7 seconds of answer must hold the carrier stop.
        if ended.is_success and self.clock.now < answered + HOLD:
            self.held[call["provider_id"]] = answered
            self.coverage["held"] = self.coverage.get("held", 0) + 1

    def detection_receipt(self):
        """A late mailbox, beep or screening result from the carrier changes nothing (work package 6.1)."""
        if self.answered_call():
            kind = self.rng.choice(
                [
                    ("call.machine.premium.detection.ended", "machine"),
                    ("call.machine.premium.greeting.ended", "beep_detected"),
                    ("call.machine.premium.call_screening.detected", "screening"),
                ]
            )
            self.event(kind[0], {"result": kind[1]})
            self.coverage["detection"] = self.coverage.get("detection", 0) + 1

    def speak(self, text=None):
        """Prospect speech on a live call: the helper may use it; it is gone once the call ends."""
        call = self.answered_call()
        if call:
            self.request(
                "POST",
                f"/v1/calls/{call['id']}/transcript",
                json={
                    "segment_id": f"soak-{len(self.events)}-{self.rng.random()}",
                    "revision": 1,
                    "side": "remote",
                    "is_final": True,
                    "text": text or self.rng.choice(SPEECH),
                },
            )
            self.coverage["speech"] = self.coverage.get("speech", 0) + 1

    def suppressed_phones(self):
        """Every active hold or opt-out, read from the store itself rather than through any lead listing."""
        from app.platform.db import MODELS
        from sqlalchemy import select

        model = MODELS["suppressions"]
        with self.client.app.state.db.transaction(False) as s:
            return {row.data["phone"] for row in s.scalars(select(model)) if row.data.get("active", True)}

    def transcript_text_after_end(self):
        from app.platform.db import MODELS
        from sqlalchemy import select

        calls = self.request("GET", "/v1/workspace/calls?filter=all&q=").json()["items"]
        ended = {c["id"] for c in calls if c.get("state") in TERMINAL}
        model = MODELS["transcript_segments"]
        with self.client.app.state.db.transaction(False) as s:
            rows = s.scalars(select(model).where(model.ctx_key.in_(ended))) if ended else []
            return [row.data.get("text") for row in rows if row.data.get("text")]

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
        business_hours,
        business_hours,
        end_as_voicemail,
        detection_receipt,
        speak,
        speak,
    ]

    # --- laws ---------------------------------------------------------------------------------------------------------

    def check(self):
        assert not self.violations, self.violations[0]
        assert len(self.submissions) <= self.keys, "Carrier received more dials than the rep requested"
        assert len(set(self.submissions)) == len(self.submissions), "An original dial was submitted again"
        calls = self.request("GET", "/v1/workspace/calls?filter=all&q=").json()["items"]
        open_calls = [c for c in calls if c.get("state") not in TERMINAL]
        assert len(open_calls) <= 1, f"two calls open at once: {[c['state'] for c in open_calls]}"
        for phone, before in self.opted_out.items():
            after = [d for d in self.carrier.dials[before:] if d.get("to") == phone]
            assert not after, f"{phone} was dialed after it opted out"
        for provider, at in self.hangups:
            answered = self.held.get(provider)
            assert answered is None or at >= answered + HOLD, (
                f"short-call hold broken: carrier stop for {provider} at {at.isoformat()}, answer {answered.isoformat()}"
            )
        left = self.transcript_text_after_end()
        assert not left, f"transcript text kept after a call ended: {len(left)} segments"

    def recover(self):
        """The plainest way back: end the call, save its outcome, go ready. Calling must then dial or say why."""
        if self.open_call():
            self.rep_hangs_up()
        self.save()
        self.go_ready()
        readiness = self.request("GET", "/v1/calling/readiness").json()
        assert readiness["blockers"] or readiness["callable_now"] or readiness["why_none"], readiness
        offered = self.offer()
        if offered.status_code != 200:
            detail = offered.json()["detail"]
            message = detail.get("message") if isinstance(detail, dict) else detail
            assert message, f"stuck with no reason after recovery: {offered.text[:200]}"


class TestPhoneRun(Run):
    __test__ = False

    def __init__(self, workspace, carrier, clock, seed, trace):
        super().__init__(workspace, carrier, clock, seed, trace)
        self.from_list = bool(seed % 2)
        self.run_id = None
        self.phone = TEST_PHONES[0]

    def offer(self):
        return self.request("POST", "/v1/calls/test/reserve", json={"test_run_id": self.run_id})

    def dial(self):
        if self.open_call():
            return
        view = self.request("GET", "/v1/workspace").json()
        if self.from_list and not view["test_run"]:
            self.pause()
            if not self.request(
                "PUT", "/v1/settings/test-list", json={"rows": [{"phone": phone} for phone in TEST_PHONES]}
            ).is_success:
                return
            armed = self.request("POST", "/v1/settings/test-list/start", json={})
            if not armed.is_success:
                return
            self.run_id = armed.json()["id"]
        elif not self.from_list and not view["test_phone"]:
            self.phone = self.rng.choice(TEST_PHONES)
            assert self.request("PUT", "/v1/admin/settings", json={"test_phone": self.phone}).is_success
            if not self.request("POST", "/v1/settings/test-call", json={}).is_success:
                return
        super().dial()

    def end_test(self):
        self.request("DELETE", "/v1/settings/test-call")

    def check(self):
        super().check()
        assert all(d["to"] in TEST_PHONES and "time_limit_secs" not in d for d in self.carrier.dials)
        leads = self.request("GET", "/v1/workspace/leads?filter=active&q=").json()["items"]
        assert all(not lead.get("attempt_count") and not lead.get("last_outcome") for lead in leads)

    STEPS = [*Run.STEPS, end_test]

    def window(self, phone, at):
        return True  # Approved test destinations are not held to the outreach window (work package 3.8).


def checked(run, step, *args):
    """Run one step with the step-local laws: dials only from a dial action, no hang-up from a detection receipt,
    no hold from practice speech."""
    submitted, stopped = len(run.submissions), len(run.hangups)
    practice = run.suppressed_phones() if step is Run.speak and isinstance(run, TestPhoneRun) else None
    run.dial() if step is Run.dial else step(run, *args)
    assert len(run.submissions) - submitted <= int(step is Run.dial), (
        f"{step.__name__} sent a carrier dial without a fresh dial action for it"
    )
    if step is Run.detection_receipt:
        assert len(run.hangups) == stopped, "a late detection receipt caused a hang-up"
    if practice is not None:
        assert run.suppressed_phones() <= practice, "practice speech on a test call added a hold"


def warm_up(run, clock, trace):
    """One complete path first, so every run reaches the new behavior before its random faults: a dial in business
    hours, an answer, prospect speech, an early End as voicemail held to 7 seconds, recovery sends the stop, save."""
    trace.record("warm up")
    run.business_hours()
    run.dial()
    if run.open_call():
        run.event("call.answered")
        checked(run, Run.detection_receipt)
        checked(
            run, Run.speak, SPEECH[0] if isinstance(run, TestPhoneRun) else "We have no budget this year."
        )
        run.end_as_voicemail()
        run.check()
        clock.now += HOLD
        run.maintenance()
        run.save()
    run.coverage["answered"] = run.coverage.get("answered", 0) + int(bool(run.held))
    run.check()
    if not isinstance(run, TestPhoneRun):
        # A weekend probe: an outreach dial must be refused, never placed (independent window check on dial).
        eastern = clock.now.astimezone(ZoneInfo("America/New_York"))
        saturday = eastern.date() + timedelta(days=(5 - eastern.weekday()) % 7 or 7)
        clock.now = datetime(
            saturday.year, saturday.month, saturday.day, 13, tzinfo=ZoneInfo("America/New_York")
        )
        clock.now = clock.now.astimezone(UTC)
        run.time_jump_session()
        checked(run, Run.dial)
        if run.open_call():
            run.rep_hangs_up()
            run.save()
        run.business_hours()
        run.check()


def outreach_window(phone, at):
    """The work package 9.3 C2 window, recomputed here from the fixture's area code and a fixed holiday list."""
    local = at.astimezone(ZoneInfo(AREA_ZONES[phone[2:5]]))
    return local.weekday() < 5 and local.date() not in HOLIDAYS and 9 <= local.hour < 17


Run.window = staticmethod(outreach_window)


def exercise(run, seed, clock, trace, monkeypatch):
    dial, hangup = run.carrier.dial, run.carrier.hangup

    async def observe_dial(payload):
        run.submissions.append(payload["command_id"])
        trace.record("carrier dial", command=payload["command_id"], phone=payload["to"])
        # Recorded, not raised: the product treats any carrier exception as an uncertain dial and would swallow it.
        if not run.window(payload["to"], clock.now):
            run.violations.append(
                f"outreach dial outside the calling window: {payload['to']} at {clock.now.isoformat()}"
            )
        return await dial(payload)

    async def observe_hangup(provider_id, command_id):
        run.hangups.append((provider_id, clock.now))
        trace.record("carrier hangup", provider=provider_id, now=clock.now.isoformat())
        return await hangup(provider_id, command_id)

    action = run.carrier.action

    async def observe_action(provider_id, name, payload):
        if name == "hangup":  # A stop sent as a call-control action counts as a hang-up too.
            run.hangups.append((provider_id, clock.now))
            trace.record("carrier hangup action", provider=provider_id, now=clock.now.isoformat())
        return await action(provider_id, name, payload)

    monkeypatch.setattr(run.carrier, "dial", observe_dial)
    monkeypatch.setattr(run.carrier, "hangup", observe_hangup)
    monkeypatch.setattr(run.carrier, "action", observe_action)
    schedule = random.Random(seed)
    warm_up(run, clock, trace)
    for index in range(STEPS):
        step = schedule.choice(run.STEPS)
        run.rng.seed(schedule.getrandbits(64))
        trace.record("choose", number=index + 1, operation=step.__name__, now=clock.now.isoformat())
        checked(run, step)
        for name, count in (("dial", len(run.submissions)), ("answered", int(bool(run.answered_call())))):
            run.coverage[name] = max(run.coverage.get(name, 0), count)
        run.check()
    trace.record("recover")
    submitted = len(run.submissions)
    run.recover()
    assert len(run.submissions) == submitted, "Recovering the workspace submitted another dial"
    run.check()
    trace.record("coverage", **run.coverage)
    if STEPS >= 200:
        # A long run that never dialed, answered or spoke proves nothing (work package section 7).
        missing = [name for name in ("dial", "answered", "speech", "held") if not run.coverage.get(name)]
        assert not missing, f"vacuous run: never reached {missing}"
    return run.coverage


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
        exercise(run, seed, clock, trace, monkeypatch)
    if os.environ.get("SOAK_STATS"):
        calls = run.request("GET", "/v1/workspace/calls?filter=all&q=").json()["items"]
        print(
            f"SOAKSTAT dials={len(carrier.dials)} calls={len(calls)} saved={sum(bool(c.get('outcome')) for c in calls)} "
            f"answered={sum(c.get('answered_at') is not None for c in calls)} optouts={len(run.opted_out)}"
        )


@pytest.mark.parametrize("seed", soak_seeds("SOAK_TEST_RUNS", 8))
def test_test_phones_stay_separate_under_random_faults(
    seed, workspace, carrier, clock, request, tmp_path, monkeypatch
):
    with StressTrace(tmp_path, request.node.nodeid, seed, SOAK_STEPS=STEPS) as trace:
        run = TestPhoneRun(workspace, carrier, clock, seed, trace)
        imported = run.request(
            "POST",
            "/v1/leads/import",
            json={
                "campaign_id": workspace["campaign_id"],
                "list_name": "Soak",
                "prepare": False,
                "rows": [{"name": f"Outreach {i}", "phone": phone} for i, phone in enumerate(TEST_PHONES)],
            },
        )
        assert imported.is_success, imported.text
        exercise(run, seed, clock, trace, monkeypatch)
