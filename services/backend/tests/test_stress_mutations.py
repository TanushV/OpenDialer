"""The randomized calling suite must catch real defects (work package section 7).

Each case plants one product defect with monkeypatch, runs a fixed seed of the soak and expects the named invariant
to fail. A mutation the suite cannot catch fails this test. Nothing here ships: the product code is unchanged.
"""

import asyncio

import pytest
from app import ops

from services.backend.tests import test_fault_soak as soak
from services.backend.tests.stress_support import StressTrace

pytestmark = pytest.mark.slow
SEEDS = (101, 202, 303, 404, 505)


def plant(name, monkeypatch):
    if name == "short-call hold skipped":
        import app.calling.hold as hold

        monkeypatch.setattr(hold, "until", lambda *args, **kwargs: None)
    elif name == "opt-out ignored":
        import app.compliance.suppressions as suppressions

        # The opt-out request is accepted but never stored, so nothing downstream can honor it.
        monkeypatch.setattr(suppressions, "apply_in", lambda service, s, entry: {"phone": entry["phone"]})
    elif name == "calling window ignored":
        import app.compliance.policy as policy

        monkeypatch.setattr(policy, "outreach_open", lambda *args, **kwargs: True)
    elif name == "detection hangs up":
        real = ops.event

        def event(service, p, event_id, event_type, call_id, payload=None, **kwargs):
            result = real(service, p, event_id, event_type, call_id, payload, **kwargs)
            if event_type.startswith("call.machine"):
                asyncio.run(ops.hangup(service, p, call_id))
            return result

        monkeypatch.setattr(ops, "event", event)
    elif name == "transcript kept":
        from app import call_records as recording

        # Keep every segment, the prospect's side in a stricter state included (plan P1 broken).
        monkeypatch.setattr(recording, "keeps_text", lambda rules, row: True)
    elif name == "practice speech suppresses":
        monkeypatch.setattr(ops, "practice_call", lambda call: False)
    elif name == "dial submitted twice":
        real = ops.carrier

        class Twice:
            def __init__(self, provider):
                self.provider = provider

            def __getattr__(self, attribute):
                return getattr(self.provider, attribute)

            async def dial(self, payload):
                await self.provider.dial(payload)
                return await self.provider.dial(payload)

        monkeypatch.setattr(ops, "carrier", lambda service, scope: Twice(real(service, scope)))
    else:
        raise ValueError(name)


MUTANTS = {
    "short-call hold skipped": ("outreach", "short-call hold broken"),
    "opt-out ignored": ("outreach", "was dialed after it opted out"),
    "calling window ignored": ("outreach", "outside the calling window"),
    "detection hangs up": ("outreach", "late detection receipt caused a hang-up"),
    "transcript kept": ("outreach", "an ended call kept text plan P1 does not allow"),
    "practice speech suppresses": ("test", "practice speech on a test call added a hold"),
    "dial submitted twice": ("outreach", "more dials than the rep requested"),
}


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_the_soak_catches_a_planted_defect(name, workspace, carrier, clock, request, tmp_path, monkeypatch):
    kind, expected = MUTANTS[name]
    monkeypatch.setattr(soak, "STEPS", 160)
    failures = []
    for seed in SEEDS:
        plant(name, monkeypatch)
        try:
            with StressTrace(tmp_path, request.node.nodeid, seed) as trace:
                if kind == "outreach":
                    # Three leads, so an opted-out number comes round again within a short run. All three are in
                    # stricter consent states (CA, IL, MA), so any prospect text kept after a call is a defect,
                    # whichever zone prime-time ordering reaches first.
                    rows = [
                        {"name": f"Mutant {i}", "phone": f"+1{area}55502{i:02d}", "company": f"Mutant Co {i}"}
                        for i, area in enumerate(["415", "617", "312"])
                    ]
                    workspace["client"].post(
                        "/v1/leads/import",
                        headers=workspace["headers"],
                        json={
                            "campaign_id": workspace["campaign_id"],
                            "list_name": "Soak",
                            "rows": rows,
                            "prepare": False,
                        },
                    )
                    run = soak.Run(workspace, carrier, clock, seed, trace)
                else:
                    run = soak.TestPhoneRun(workspace, carrier, clock, seed, trace)
                soak.exercise(run, seed, clock, trace, monkeypatch)
        except AssertionError as error:
            failures.append(str(error))
            if expected in str(error):
                return
    pytest.fail(f"planted defect '{name}' was not caught by '{expected}': {failures or 'every seed passed'}")
