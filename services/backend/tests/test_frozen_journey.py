"""The packaged acceptance harness must drive the current public journey, including expiry."""

import importlib
from datetime import timedelta

from app.main import create_app
from fastapi.testclient import TestClient


def test_packaged_journey_uses_real_public_routes_and_retained_session(workspace, app, clock, carrier):
    harness = importlib.import_module("scripts.frozen_acceptance")
    elapsed = []

    def advance(seconds):
        elapsed.append(seconds)
        clock.now += timedelta(seconds=seconds)

    result = harness.call_journey(
        workspace["client"], workspace["headers"], workspace["campaign_id"], pause=advance
    )
    assert sum(elapsed) == 720
    assert result["raw_expired"] and result["task_id"] and result["helper_cancelled"]
    restarted = create_app(app.state.service.settings, clock=clock, carrier=carrier)
    try:
        with TestClient(restarted) as client:
            retained = harness.check_persisted(client, workspace["headers"], result)
            assert retained["task_id"] == result["task_id"]
            assert retained["note_hash"] == result["note_hash"]
            assert retained["integrity_ok"]
    finally:
        restarted.state.db.engine.dispose()
