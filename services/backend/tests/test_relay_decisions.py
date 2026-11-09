"""R9E.7: every combination of what the relay loop sees after an exchange leads somewhere, and never to a dead end."""

from itertools import product

from app.calling.relay import after_exchange, after_failure


def test_every_combination_after_an_exchange():
    for gap, acknowledged, caught, idle in product([False, True], repeat=4):
        nxt = after_exchange(gap=gap, acknowledged=acknowledged, local_caught_up=caught, idle=idle)
        where = (gap, acknowledged, caught, idle)
        # Calling is told events flow only when nothing is pending and there is no gap.
        assert nxt.caught_up == (not gap and not acknowledged and caught), where
        # A gap is cleared exactly when nothing here waits on the missed events.
        assert nxt.clear_gap == (gap and not acknowledged and caught and idle), where
        # Pending acknowledgements or a cleared gap mean exchange again at once; nothing waits on a timer.
        assert nxt.again == (acknowledged or nxt.clear_gap), where
        # A gap that cannot be cleared yet is reported, never hidden as connected.
        if gap and not nxt.clear_gap:
            assert nxt.state == "delivery_gap", where


def test_a_failed_exchange_names_why():
    assert after_failure(RuntimeError("This setup is in use on another computer")) == "in_use_elsewhere"
    assert after_failure(TimeoutError("timed out")) == "reconnecting"
