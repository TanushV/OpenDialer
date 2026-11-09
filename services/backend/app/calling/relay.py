"""R9E.7: what the relay loop decides after each exchange, as one pure function.

`ManagedConnection.run` (managed_webhooks.py) fetches events, applies them and acknowledges them; everything it then
decides about calling comes from `after_exchange` and `after_failure`, so every combination is checked in tests.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Next:
    state: str  # "connected" or "delivery_gap"
    caught_up: bool  # Calling may use this exchange as proof that events are flowing.
    clear_gap: bool  # Ask the relay to clear its delivery gap now.
    again: bool  # Exchange again at once (acknowledgements to send, or a gap just cleared).


def after_exchange(*, gap, acknowledged, local_caught_up, idle):
    """After one exchange: `gap` the relay reports a delivery gap; `acknowledged` events were applied and must be
    acknowledged; `local_caught_up` every received event is applied; `idle` no call or calling operation is open."""
    if gap and not acknowledged and local_caught_up and idle:
        # The missed events belonged to calls long over: clear the gap and exchange again (November 9).
        return Next(state="connected", caught_up=False, clear_gap=True, again=True)
    return Next(
        state="delivery_gap" if gap else "connected",
        caught_up=not acknowledged and not gap and local_caught_up,
        clear_gap=False,
        again=acknowledged,
    )


def after_failure(error):
    """A failed exchange: another computer holds this setup, or the network or relay is down."""
    return "in_use_elsewhere" if "another computer" in str(error) else "reconnecting"
