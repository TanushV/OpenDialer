"""The section 4 bans and section 12 caps measure what they claim (R1.7, R1.8)."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rules", ROOT / "scripts/rules.py")
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)


def test_a_broad_except_must_log_or_raise():
    source = """
import logging
def quiet():
    try:
        work()
    except Exception:
        pass
def logged():
    try:
        work()
    except Exception as exc:
        logging.getLogger(__name__).warning("failed: %s", exc)
def reraised():
    try:
        work()
    except (ValueError, Exception):
        cleanup()
        raise
def narrow():
    try:
        work()
    except ValueError:
        pass
"""
    assert rules.silent_broad_excepts(source) == [6]


def test_nested_functions_are_measured_on_their_own():
    source = "def outer():\n    x = 1\n    def inner():\n        return 2\n    return inner\n"
    sizes = dict(rules.function_sizes(source))
    assert sizes == {"outer": 3, "inner": 2}


@pytest.mark.slow
def test_the_report_warns_on_legacy_code_and_fails_when_strict(capsys):
    assert rules.main([]) == 0
    assert "Legacy hits and whole-app caps are warnings" in capsys.readouterr().out
    assert rules.main(["--strict"]) == 1


def test_a_hit_inside_a_strict_package_fails_but_platform_and_legacy_do_not():
    hits = {
        "tenant_id in core": ["platform/db.py:1"],
        ".data.get( in core": ["calling/state.py:3"],
    }
    assert rules.strict_hits(hits, []) == [".data.get( in core: calling/state.py:3"]
    # A silent except fails anywhere, legacy code included (since November 10).
    assert rules.strict_hits({"silent except Exception": ["service.py:9"]}, []) == [
        "silent except Exception: service.py:9"
    ]
    assert rules.strict_hits({"silent except Exception": ["platform/jobs.py:4"]}, []) == [
        "silent except Exception: platform/jobs.py:4"
    ]
    assert rules.strict_hits({}, [("leads/intake.py:parse", 121, 120), ("service.py", 900, 700)]) == [
        "121 lines over 120: leads/intake.py:parse"
    ]
