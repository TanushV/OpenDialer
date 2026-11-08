"""R4.1 and G3: one pure table moves every call state; every state has a limit and an exit."""

import importlib.util
import re
from pathlib import Path

import pytest
from app.calling.state import APP, CARRIER, EVENTS, ORDER, STATES, TERMINAL, TIMEOUTS, transition

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("event", [*EVENTS, "call.unknown"])
def test_every_state_and_event_lands_on_a_known_state(state, event):
    after = transition(state, event)
    assert after in STATES
    if event in CARRIER:
        assert ORDER[after] >= ORDER[state], "carrier events never move a call backward"
    if state in TERMINAL and event != "outcome.saved":
        assert after == state, "a terminal call stays terminal"


def test_a_hangup_ends_every_live_call_and_nothing_revives_it():
    for state in set(STATES) - TERMINAL:
        assert transition(state, "call.hangup") == "ENDED"
    for event in CARRIER:
        assert transition("ENDED", event) == "ENDED"
    assert transition("ENDED", "outcome.saved") == "FINALIZED"
    assert transition("RINGING", "outcome.saved") == "RINGING"


def test_the_dial_steps_follow_the_app_events():
    assert transition("AUTHORIZED", "dial.sent") == "DIALING"
    assert transition("DIALING", "dial.uncertain") == "RECONCILING"
    assert transition("RECONCILING", "dial.unconfirmed") == "DIAL_UNCONFIRMED"
    assert transition("DIAL_UNCONFIRMED", "call.initiated") == "DIALING"  # A late receipt still counts.
    assert transition("HUMAN", "dial.sent") == "HUMAN"
    assert transition("HUMAN", "hangup.requested") == "ENDING"


def test_every_non_terminal_state_times_out_into_an_exit_that_reaches_terminal():
    assert set(TIMEOUTS) == set(STATES) - TERMINAL
    for state in TIMEOUTS:
        seen, current = [], state
        while current not in TERMINAL:
            assert current not in seen, f"{state} loops through {seen}"
            seen.append(current)
            seconds, event = TIMEOUTS[current]
            assert seconds > 0 and event in APP
            after = transition(current, event)
            assert after != current, f"{current} does not leave on {event}"
            current = after


def test_unknown_states_from_older_versions_are_left_alone():
    assert transition("VM_PLAYING", "call.hangup") == "VM_PLAYING"
    assert transition("VM_PLAYING", "call.answered") == "VM_PLAYING"


def test_only_the_table_names_call_states_in_writes():
    app = ROOT / "services/backend/app"
    literal = re.compile(r"update\(call, [^)]*state=\"[A-Z_]+\"|\bstate=\"[A-Z_]+\"")
    found = [
        f"{path.relative_to(app)}:{number}"
        for path in app.rglob("*.py")
        if "calling" not in path.parts and "__pycache__" not in path.parts
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if literal.search(line) and "leg" not in line
    ]
    assert found == []


def test_the_typescript_state_list_is_generated_from_this_table():
    spec = importlib.util.spec_from_file_location("gen_call_states", ROOT / "scripts/gen_call_states.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    assert generator.TARGET.read_text() == generator.render(), "run scripts/gen_call_states.py"
