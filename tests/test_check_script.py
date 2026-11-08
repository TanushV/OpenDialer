"""The local check loop (scripts/check.py) and GitHub CI run the same steps, and the fast tier stays fast."""

import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check", ROOT / "scripts/check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def workflow_steps():
    """Every `check.py run STEP` that a Verify job calls."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    called = set()
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            words = shlex.split(step.get("run", ""))
            if "scripts/check.py" in words:
                rest = words[words.index("scripts/check.py") + 1 :]
                assert rest[0] == "run", step["run"]
                called.update(rest[1:])
    return called


def test_ci_calls_only_named_steps_and_covers_the_full_tier():
    called = workflow_steps()
    assert called <= set(check.STEPS), called - set(check.STEPS)
    # Whatever the local full tier checks, CI checks too, so a local pass and a CI pass mean the same thing.
    assert set(check.TIERS["full"]) <= called, set(check.TIERS["full"]) - called
    assert "browser" in called


def test_fast_tier_skips_slow_tests_and_the_browser_suite():
    fast = check.tier_steps("fast")
    assert "backend" not in fast and "browser" not in fast
    command, _ = fast["backend-fast"][0]
    assert command[-2:] == ["-m", "not slow"]


def test_full_tier_says_what_it_leaves_to_ci():
    assert "browser" not in check.TIERS["full"]
    assert "R1.2a" in check.NOT_YET_LOCAL["full"]


def test_a_keyword_reaches_each_test_runner_as_one_argument():
    keyword = "it's a dial and hang"
    steps = check.keyword_steps(keyword)
    for name in ("backend-match", "frontend-match"):
        command, _ = steps[name][0]
        assert shlex.split(check._join(command))[-1] == keyword


def test_checks_never_see_saved_credentials(monkeypatch):
    monkeypatch.setenv("OPENDIALER_SECRET_KEY_FILE", "/Users/someone/Library/key")
    monkeypatch.setenv("OPENDIALER_LOCAL_TOKEN", "real-token")
    env = check._env()
    assert "OPENDIALER_SECRET_KEY_FILE" not in env and "OPENDIALER_LOCAL_TOKEN" not in env


def test_hooks_run_the_fast_tier_on_commit_and_the_full_tier_on_push():
    for hook, tier in (("pre-commit", "fast"), ("pre-push", "full")):
        path = ROOT / ".githooks" / hook
        assert os.access(path, os.X_OK)
        assert f"scripts/check.py {tier}" in path.read_text()


@pytest.mark.slow
@pytest.mark.parametrize("workers", [[], ["-n", "2"]])
def test_an_unmarked_slow_test_fails_the_run_and_a_marked_one_passes(tmp_path, workers):
    shutil.copy(ROOT / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_timing.py").write_text(
        "import time, pytest\n"
        "def test_unmarked():\n    time.sleep(0.3)\n"
        "@pytest.mark.slow\ndef test_marked():\n    time.sleep(0.3)\n"
    )
    env = {**os.environ, "OPENDIALER_SLOW_LIMIT": "0.1"}
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-W", "ignore", *workers]
    result = subprocess.run(  # noqa: S603 - fixed pytest command
        command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 1, result.stdout
    assert "test_timing.py::test_unmarked" in result.stdout
    assert "test_timing.py::test_marked" not in result.stdout.split("unmarked slow tests")[-1]
    (tmp_path / "test_timing.py").write_text(
        "import time, pytest\n@pytest.mark.slow\ndef test_marked():\n    time.sleep(0.3)\n"
    )
    result = subprocess.run(  # noqa: S603 - fixed pytest command
        command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout
