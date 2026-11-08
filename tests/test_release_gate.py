"""R0.13a: a signed release is built only from a commit whose Verify run passed."""

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_gate", ROOT / "scripts/release_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
SHA = "ee90345" + "0" * 33


def run(status="completed", conclusion="success", sha=SHA):
    return {"head_sha": sha, "status": status, "conclusion": conclusion}


def test_a_passed_verify_on_this_commit_allows_the_release():
    assert gate.verdict([run(conclusion="failure"), run()], SHA)[0] is True


def test_a_failed_or_unfinished_verify_refuses_it():
    # ee90345 and c5e9da5 failed Verify yet got signed candidates; this is the case under test.
    allowed, reason = gate.verdict([run(conclusion="failure")], SHA)
    assert not allowed and "has not passed" in reason
    assert gate.verdict([run(status="in_progress", conclusion=None)], SHA)[0] is False


def test_a_pass_on_another_commit_does_not_count():
    allowed, reason = gate.verdict([run(sha="f" * 40)], SHA)
    assert not allowed and "No Verify run" in reason
    assert gate.verdict([], SHA)[0] is False


def test_every_release_build_waits_for_the_gate():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    jobs = workflow["jobs"]
    assert "scripts/release_gate.py" in str(jobs["verified"]["steps"])
    for name, job in jobs.items():
        if name == "verified":
            continue
        needs = job.get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        upstream = set(needs)
        for need in needs:
            more = jobs[need].get("needs", [])
            upstream |= {more} if isinstance(more, str) else set(more)
        assert "verified" in upstream, name
