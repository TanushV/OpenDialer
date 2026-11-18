from types import SimpleNamespace

from app.calling import state

from scripts import calling_budget


def test_state_budget_counts_real_transitions_and_timeouts():
    counts = calling_budget.state_counts(state)
    assert counts["states"] == len(state.STATES)
    assert counts["events"] == len(state.EVENTS)
    assert counts["transitions"] > counts["states"]
    assert counts["timed_states"] == len(set(state.STATES) - state.TERMINAL)


def test_a_new_path_exceeding_the_baseline_names_the_growth(capsys):
    current = calling_budget.measure()
    grown = {**current, "states": calling_budget.BUDGET["states"] + 1}
    assert calling_budget.report(grown) == 1
    assert "states" in capsys.readouterr().out
    assert calling_budget.report(current) == 0


def test_noop_events_do_not_count_as_new_paths():
    machine = SimpleNamespace(
        STATES=("ready", "ended"),
        EVENTS=("end", "duplicate"),
        TIMEOUTS={"ready": (1, "end")},
        transition=lambda state, event: "ended" if event == "end" else state,
    )
    assert calling_budget.state_counts(machine) == {
        "states": 2,
        "events": 2,
        "transitions": 1,
        "timed_states": 1,
    }
