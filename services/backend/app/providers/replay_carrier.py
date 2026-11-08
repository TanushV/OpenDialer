"""The only fake: a carrier that replays recorded real calls (docs/REBUILD_PLAN.md R1.4, rule 6).

A timeline from `tests/fixtures/carrier_timelines` becomes the signed webhook events Telnyx sent for
that call, re-keyed to the call under test: the prospect leg carries this call's client state, the
rep's audio leg carries the agent-leg state, and legs this app never placed (the rep's own SIP leg)
carry none. Variants reorder, drop or repeat events to reach orders real calls can produce.
"""

import base64
import json
import random
from datetime import datetime, timedelta

from app.providers.simulated import SimulatedCarrier


def _state(values):
    return base64.b64encode(json.dumps(values).encode()).decode()


class ReplayCarrier(SimulatedCarrier):
    @staticmethod
    def renewed_legs(timeline):
        """How many fresh rep audio legs the recording opened after its call."""
        return len({e["leg"] for e in timeline["events"] if e["leg"].startswith("agent-renewed")})

    def events(self, timeline, *, call_id, tenant_id, prospect_id, agent_ids, started_at: datetime):
        """Webhook events for one recorded timeline, in recorded order, with receipt times.

        `agent_ids` are real rep audio legs: the call's own first, then one per renewed leg.
        """
        legs = {"prospect": prospect_id, "agent": agent_ids[0]}
        legs.update({f"agent-renewed-{n}": leg for n, leg in enumerate(agent_ids[1:], 1)})
        produced = []
        for index, item in enumerate(timeline["events"]):
            leg = item["leg"]
            control = legs.setdefault(leg, f"replay-{leg}-{call_id[:8]}")
            payload = {"call_control_id": control}
            if leg == "prospect":
                payload["client_state"] = _state({"call_id": call_id, "tenant_id": tenant_id})
            elif leg == "agent" or leg.startswith("agent-renewed"):
                payload["client_state"] = _state({"agent_leg_id": control, "tenant_id": tenant_id})
            for key in ("hangup_cause", "hangup_source", "sip_hangup_cause", "result", "direction"):
                if item.get(key) is not None:
                    payload[key] = item[key]
            occurred = started_at + timedelta(seconds=item["t"])
            produced.append(
                {
                    "id": f"replay-{call_id[:8]}-{index}",
                    "event_type": item["event_type"],
                    "payload": payload,
                    "occurred_at": occurred.isoformat(),
                    "received_at": occurred + timedelta(seconds=item.get("received_after", 0.5)),
                }
            )
        return produced


# Variants (R1.5). Each takes and returns a list of events from ReplayCarrier.events.
def _prospect(events, name):
    return [e for e in events if e["event_type"] == name and "call_id" in _decode(e)]


def _decode(event):
    raw = event["payload"].get("client_state")
    return json.loads(base64.b64decode(raw)) if raw else {}


def hangup_during_dial(events):
    """The prospect's line ends before anyone answers: no answer, bridge or detection."""
    dropped = {"call.answered", "call.bridged", "call.machine.premium.detection.ended"}
    return [
        e for e in events if e["event_type"] not in dropped and not e["event_type"].startswith("call.speak")
    ]


def answer_after_hangup(events):
    """Delivery reorders a late answer behind the hangup."""
    answered = _prospect(events, "call.answered")
    rest = [e for e in events if e not in answered]
    hangup = next(i for i, e in enumerate(rest) if e in _prospect(rest, "call.hangup"))
    return rest[: hangup + 1] + answered + rest[hangup + 1 :]


def duplicated(events):
    """Every event delivered twice with the same event ID, as at-least-once delivery allows."""
    return [e for event in events for e in (event, dict(event))]


def reversed_order(events):
    return list(reversed(events))


def shuffled(events, seed):
    order = list(events)
    random.Random(seed).shuffle(order)  # noqa: S311 - test ordering, not security
    return order
