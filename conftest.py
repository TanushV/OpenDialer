"""Disposable test encryption key; tests never access the user's credential store.

Also guards the fast tier of scripts/check.py: a test without the `slow` marker that runs longer than
SLOW_LIMIT seconds fails the session, so the fast tier stays fast without anyone having to remember.
"""

import os

import pytest
from cryptography.fernet import Fernet

# Tests are marked slow from 1 second on this Mac; the limit leaves room for a loaded or slower machine.
SLOW_LIMIT = float(os.environ.get("OPENDIALER_SLOW_LIMIT", "5"))
# The fast tier only runs on SQLite; PostgreSQL runs are CI-only until Phase 2 removes them.
GUARDED = not os.environ.get("OPENDIALER_TEST_POSTGRES_URL")
_too_slow = []


@pytest.fixture(scope="session", autouse=True)
def test_secret_key():
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("OPENDIALER_SECRET_KEY", Fernet.generate_key().decode())
        patch.delenv("OPENDIALER_SECRET_KEY_FILE", raising=False)
        yield


def pytest_runtest_logreport(report):
    if (
        GUARDED
        and report.when == "call"
        and report.passed
        and report.duration > SLOW_LIMIT
        and "slow" not in report.keywords
    ):
        _too_slow.append((report.nodeid, report.duration))


def pytest_sessionfinish(session):
    if _too_slow and not hasattr(session.config, "workerinput"):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter):
    if _too_slow:
        terminalreporter.section("unmarked slow tests")
        for nodeid, duration in sorted(_too_slow, key=lambda item: -item[1]):
            terminalreporter.line(f"{duration:6.1f}s {nodeid}  (add @pytest.mark.slow)")
