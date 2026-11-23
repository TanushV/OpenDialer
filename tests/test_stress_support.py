"""A failed randomized test must leave a usable replay even when artifact storage fails."""

import json
import os
from types import SimpleNamespace

import pytest

from services.backend.tests import stress_support
from services.backend.tests.stress_support import StressTrace, soak_seeds


def test_fresh_seed_ranges_keep_regressions_and_a_replay_selects_only_one(monkeypatch):
    monkeypatch.delenv("SOAK_SEED", raising=False)
    monkeypatch.setenv("SOAK_SEED_START", "293")
    monkeypatch.setenv("SOAK_RUNS", "3")
    assert soak_seeds("SOAK_RUNS", 50, pinned=[1, 294]) == [1, 294, 293, 295]
    monkeypatch.setenv("SOAK_SEED", "294")
    assert soak_seeds("SOAK_RUNS", 50, pinned=[1, 294]) == [294]


@pytest.mark.parametrize("writable", [True, False])
@pytest.mark.parametrize(
    ("platform", "assignments"),
    [
        pytest.param("posix", "SOAK_SEED=294 SOAK_STEPS=200", id="posix"),
        pytest.param("nt", "$env:SOAK_SEED = '294'; $env:SOAK_STEPS = '200';", id="powershell"),
    ],
)
def test_stress_failure_keeps_the_original_error_and_replay_in_the_log(
    tmp_path, monkeypatch, writable, platform, assignments
):
    monkeypatch.setattr(stress_support, "os", SimpleNamespace(name=platform, environ=os.environ))
    artifacts = tmp_path / "artifacts"
    if not writable:
        artifacts.write_text("A file prevents creating the artifact directory", encoding="utf-8")
    monkeypatch.setenv("SOAK_ARTIFACTS", str(artifacts))
    nodeid = "tests/test_fault.py::test_schedule[294]"
    with pytest.raises(AssertionError, match="Duplicate carrier submission") as failed:
        with StressTrace(tmp_path, nodeid, 294, SOAK_STEPS=200) as trace:
            trace.record("carrier accepted", leg="old-leg")
            trace.record("restart", rep="fixture-rep")
            raise AssertionError("Duplicate carrier submission")
    note = failed.value.__notes__[0]
    assert "CALLING STRESS FAILURE (seed 294)" in note
    assert f"Replay: {assignments} uv run pytest -x -q '{nodeid}'" in note
    assert '"leg": "old-leg"' in note
    if not writable:
        assert "Trace could not be written" in note
        return
    data = json.loads((artifacts / "test_schedule-294.json").read_text(encoding="utf-8"))
    assert data["replay_env"] == {"SOAK_SEED": 294, "SOAK_STEPS": 200}
    assert data["failure"] == {"type": "AssertionError", "message": "Duplicate carrier submission"}
    assert [event["action"] for event in data["events"]] == ["carrier accepted", "restart"]
    assert [event["step"] for event in data["events"]] == [1, 2]


def test_passing_runs_add_their_coverage_to_the_ledger_and_failures_do_not(tmp_path, monkeypatch):
    """A sweep can show what its runs reached, not only that they passed."""
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("SOAK_LEDGER", str(ledger))
    for seed, coverage in ((1, {"dial": 3, "held": 1}), (2, {"dial": 2, "held": 0})):
        with StressTrace(tmp_path, f"tests/test_fault.py::test_schedule[{seed}]", seed) as trace:
            trace.record("choose", operation="dial")
            trace.record("coverage", **coverage)
    with StressTrace(tmp_path, "tests/test_other.py::test_shared[3]", 3):
        pass
    with (
        pytest.raises(AssertionError),
        StressTrace(tmp_path, "tests/test_fault.py::test_schedule[4]", 4) as trace,
    ):
        trace.record("coverage", dial=9)
        raise AssertionError("failed run")
    assert stress_support.ledger_totals(ledger) == {
        "test_schedule": {
            "runs": 2,
            "effects": {"dial": {"total": 5, "runs": 2}, "held": {"total": 1, "runs": 1}},
        },
        "test_shared": {"runs": 1, "effects": {}},
    }
