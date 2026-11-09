"""R9E.6: a self-clearing blocker that outlives its promise is repaired, then called stuck if it stays."""

from app.calling.watchdog import Watchdog


def test_a_blocker_within_its_promise_is_left_alone():
    dog = Watchdog()
    assert dog.observe({"CONNECTION_RECONNECTING"}, 0) == []
    assert dog.observe({"CONNECTION_RECONNECTING"}, 59) == []
    assert dog.stuck == set()


def test_an_overdue_blocker_is_repaired_once_then_called_stuck_if_it_stays():
    dog = Watchdog()
    dog.observe({"CONNECTION_DELIVERY_GAP"}, 0)
    assert dog.observe({"CONNECTION_DELIVERY_GAP"}, 61) == ["CONNECTION_DELIVERY_GAP"]
    assert dog.stuck == set(), "one repair before calling it stuck"
    assert dog.observe({"CONNECTION_DELIVERY_GAP"}, 100) == []
    assert dog.observe({"CONNECTION_DELIVERY_GAP"}, 122) == ["CONNECTION_DELIVERY_GAP"]
    assert dog.stuck == {"CONNECTION_DELIVERY_GAP"}


def test_a_blocker_that_clears_is_forgotten():
    dog = Watchdog()
    dog.observe({"CONNECTION_RECONNECTING"}, 0)
    dog.observe({"CONNECTION_RECONNECTING"}, 61)
    dog.observe({"CONNECTION_RECONNECTING"}, 122)
    assert dog.stuck
    dog.observe(set(), 130)
    assert dog.stuck == set() and dog.since == {}


def test_a_blocker_only_the_rep_can_clear_is_never_repaired():
    dog = Watchdog()
    dog.observe({"TELNYX_NOT_CONFIGURED"}, 0)
    assert dog.observe({"TELNYX_NOT_CONFIGURED"}, 10_000) == []
