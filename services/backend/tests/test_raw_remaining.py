"""The remaining raw lifetime a client is told shrinks with real time, even on a frozen clock.

The Windows native journey showed raw speech on screen more than six minutes after receipt:
simulation freezes the business clock, the server reported raw_expires_at minus that clock
(always about 300 s), and the desktop restarted a full lifetime on every refresh."""

from datetime import UTC, datetime, timedelta

from app import privacy
from app.privacy import EphemeralBuffer

FROZEN = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)


def test_remaining_lifetime_follows_the_monotonic_deadline_under_a_frozen_clock(monkeypatch):
    elapsed = [1000.0]
    monkeypatch.setattr(privacy.time, "monotonic", lambda: elapsed[0])
    buffer = EphemeralBuffer(clock=lambda: FROZEN)
    buffer.put("segment", {"text": "synthetic"}, expires_at=FROZEN + timedelta(seconds=300))
    assert buffer.remaining_ms("segment") == 300_000
    elapsed[0] += 200
    assert buffer.remaining_ms("segment") == 100_000, "The frozen clock must not restart the lifetime"
    elapsed[0] += 101
    assert buffer.remaining_ms("segment") is None and buffer.get("segment") is None


def test_remaining_lifetime_follows_the_wall_deadline_when_it_is_earlier(monkeypatch):
    now = [FROZEN]
    monkeypatch.setattr(privacy.time, "monotonic", lambda: 1000.0)
    buffer = EphemeralBuffer(clock=lambda: now[0])
    buffer.put("segment", {"text": "synthetic"}, expires_at=FROZEN + timedelta(seconds=300))
    now[0] = FROZEN + timedelta(seconds=250)
    assert buffer.remaining_ms("segment") == 50_000


def test_the_call_view_reports_the_shrinking_lifetime(workspace, app, monkeypatch):
    from conftest import start

    service = app.state.service
    elapsed = [5000.0]
    monkeypatch.setattr(privacy.time, "monotonic", lambda: elapsed[0])
    call = start(workspace, key="raw-remaining")
    c, h = workspace["client"], workspace["headers"]
    for event in ("call.answered", "call.machine.detection.ended:human"):
        c.post(
            "/v1/simulation/event",
            headers=h,
            json={"event_id": event + call["id"], "event_type": event, "call_id": call["id"]},
        )
    response = c.post(
        f"/v1/calls/{call['id']}/transcript",
        headers=h,
        json={
            "segment_id": "raw-1",
            "revision": 1,
            "side": "remote",
            "text": "Synthetic turn.",
            "is_final": True,
        },
    )
    assert response.status_code == 200, response.text
    first = c.get(f"/v1/calls/{call['id']}", headers=h).json()["transcript"][0]["raw_remaining_ms"]
    elapsed[0] += 200
    later = c.get(f"/v1/calls/{call['id']}", headers=h).json()["transcript"]
    assert later and later[0]["raw_remaining_ms"] <= first - 199_000
    assert service.clock() == service.clock(), "The simulation clock stays frozen in this fixture"
