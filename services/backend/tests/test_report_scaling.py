"""Background reports must join attempts by lead once, not rescan per assignment."""

from datetime import timedelta
from types import SimpleNamespace

from test_remediation_privacy import principal


def test_bounded_normal_reads_and_short_writes(workspace, app, monkeypatch):
    """C038: forecast/slot scoring must not run while an operational writer is held."""
    from contextlib import contextmanager
    from threading import local

    import app.optimization.workflows as workflows

    service, p = app.state.service, principal(workspace)
    state = local()
    transaction = service.db.transaction

    @contextmanager
    def traced(write=True):
        prior = getattr(state, "writing", False)
        state.writing = prior or write
        try:
            with transaction(write) as session:
                yield session
        finally:
            state.writing = prior

    slots, seen = workflows.future_slots, []

    def checked(*args, **kwargs):
        seen.append(True)
        assert not getattr(state, "writing", False), "Scheduling computes outside a write transaction"
        return slots(*args, **kwargs)

    monkeypatch.setattr(service.db, "transaction", traced)
    monkeypatch.setattr(workflows, "future_slots", checked)
    service.refresh_optimization(p)
    assert seen, "The fixture must actually exercise due-work planning"


def test_report_history_join_grows_linearly(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        campaign = service.scoped(s, "campaigns", p)[0]
        epoch = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
    original = service.scoped
    visits = []

    class Counted(list):
        def __iter__(self):
            for item in super().__iter__():
                visits.append(item.id)
                yield item

    def row(identity, values):
        return SimpleNamespace(id=identity, data=values, version=1)

    calls = Counted(
        [
            row(
                f"call-{i}",
                {
                    "lead_id": f"lead-{i}",
                    "rep_id": p["user_id"],
                    "started_at": (clock.now - timedelta(days=1)).isoformat(),
                    "submitted_at": (clock.now - timedelta(days=1)).isoformat(),
                    "ended_at": (clock.now - timedelta(days=1)).isoformat(),
                    "outcome": "no_answer",
                    "state": "FINALIZED",
                },
            )
            for i in range(300)
        ]
    )
    assignments = [
        row(
            f"assignment-{i}",
            {
                "epoch": epoch,
                "lead_id": f"lead-{i}",
                "arm": "control" if i % 2 else "candidate",
                "assigned_at": (clock.now - timedelta(days=2)).isoformat(),
                "coverage_due_at": clock.now.isoformat(),
                "goal": "meeting_held",
                "outcome_window_days": 14,
            },
        )
        for i in range(300)
    ]
    monkeypatch.setattr(
        service,
        "scoped",
        lambda s, kind, who: (
            calls
            if kind == "call_attempts"
            else assignments
            if kind == "experiment_assignments"
            else original(s, kind, who)
        ),
    )
    monkeypatch.setattr(service, "operational_dataset", lambda *args: [])
    monkeypatch.setattr(service, "forecast", lambda *args: {})
    monkeypatch.setattr(service, "inventory_evidence", lambda *args: [])
    result = service.optimization_report(p)
    assert sum(a["leads"] for a in result["trial"]["arms"].values()) == 300
    assert len(visits) <= 300 * 12, {"attempt_visits": len(visits)}
