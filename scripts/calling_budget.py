"""Report the size of the calling state machine and decisions on the Telnyx path.

The end-to-end calling tests separately check the effects that reach the carrier.
"""

import ast
from pathlib import Path
from runpy import run_path
from types import SimpleNamespace

APP = Path(__file__).resolve().parents[1] / "services/backend/app"
BUDGET = {
    "states": 13,
    # Voicemail detection removed (work package section 6): 19 events and 123 transitions down to 14 and 87.
    "events": 14,
    "transitions": 87,
    "timed_states": 11,
    "calling/audio_leg.py:create": 14,
    "calling/dial.py:_prepare_call": 6,
    "calling/dial.py:_claim_dial": 8,  # Refuse a rep's request that expired before the carrier send.
    "calling/dial.py:_dispatch_call": 10,
    # An explicit stop may cancel a still-unclaimed dial and free its hold. +1: the short-call hold (work package 6.10)
    # either waits for 7 seconds after answer or drops a held stop the carrier already ended.
    "calling/dial.py:_claim_hangup": 15,
    "calling/dial.py:hangup": 7,
    "calling/recovery.py:next_step": 3,
    "telephony/recovery.py:_recover_call": 7,
    "providers/telnyx.py:dial": 7,
    "providers/telnyx.py:action": 2,
    "providers/telnyx.py:reconcile": 5,
}
DECISIONS = (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.match_case)


def state_counts(machine):
    return {
        "states": len(machine.STATES),
        "events": len(machine.EVENTS),
        "transitions": sum(
            machine.transition(state, event) != state for state in machine.STATES for event in machine.EVENTS
        ),
        "timed_states": len(machine.TIMEOUTS),
    }


def measure():
    measured = state_counts(SimpleNamespace(**run_path(str(APP / "calling/state.py"))))
    for path in {key.split(":")[0] for key in BUDGET if ":" in key}:
        tree = ast.parse((APP / path).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = f"{path}:{node.name}"
                if key in BUDGET:
                    measured[key] = sum(isinstance(part, DECISIONS) for part in ast.walk(node))
    return measured


def report(measured):
    print("Calling complexity budget (states, transitions and control-flow decisions)")
    failed = False
    for key, limit in BUDGET.items():
        current = measured.get(key)
        if current is None or current > limit:
            failed = True
            print(f"  FAIL {key}  {current if current is not None else 'missing'} / {limit}")
        else:
            print(f"  {key}  {current} / {limit}")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(report(measure()))
