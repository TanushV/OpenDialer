"""A write conflict answers 409 and names, without values, which table or constraint conflicted.

Hangups and wrap-ups under shared load returned the generic 409 and nothing said why."""

import logging

from sqlalchemy.orm.exc import StaleDataError

PRESENCE = {"status": "paused"}


def conflicts(caplog):
    return [r.conflict for r in caplog.records if hasattr(r, "conflict")]


def test_a_stale_version_conflict_is_logged_by_table(workspace, app, monkeypatch, caplog):
    def stale(*args, **kwargs):
        raise StaleDataError(
            "UPDATE statement on table 'campaigns' expected to update 1 row(s); 0 were matched."
        )

    monkeypatch.setattr(app.state.service, "presence", stale)
    with caplog.at_level(logging.WARNING, logger="app.conflicts"):
        response = workspace["client"].post("/v1/rep/presence", headers=workspace["headers"], json=PRESENCE)
    assert response.status_code == 409
    assert conflicts(caplog) == [{"type": "StaleDataError", "table": "campaigns", "constraint": None}]


def test_a_unique_conflict_names_its_table_but_never_the_duplicate_value(workspace, app, monkeypatch, caplog):
    from app.db import Job, uid

    service, secret = app.state.service, "projection:secret-identity-value"

    def duplicate(*args, **kwargs):
        with service.db.transaction() as s:
            for _ in range(2):
                s.add(
                    Job(
                        id=uid(),
                        tenant_id=workspace["tenant_id"],
                        queue="ml",
                        key=secret,
                        payload={},
                        priority=0,
                        max_attempts=1,
                        available_at=service.clock(),
                    )
                )
                s.flush()

    monkeypatch.setattr(service, "presence", duplicate)
    with caplog.at_level(logging.WARNING, logger="app.conflicts"):
        response = workspace["client"].post("/v1/rep/presence", headers=workspace["headers"], json=PRESENCE)
    assert response.status_code == 409
    [conflict] = conflicts(caplog)
    assert conflict["type"] == "IntegrityError" and conflict["table"] == "jobs"
    assert secret not in caplog.text and secret not in str(conflict)
