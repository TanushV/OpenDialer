"""Background reports must join attempts by lead once, not rescan per assignment."""

from datetime import timedelta

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
    service.refresh_schedule_plans(p, workspace["campaign_id"])
    assert seen, "The fixture must actually exercise due-work planning"


def test_report_history_join_grows_linearly(workspace, app, clock, monkeypatch):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction(False) as s:
        campaign = service.scoped(s, "campaigns", p)[0]
        epoch = campaign.id + ":" + campaign.data.get("optimization_epoch", "conversion-v2")
    from app.db import put
    from test_projection_replay import project_pending

    # Persist actual scoped sources. Mocking the retired all-history reader
    # cannot populate the replacement contribution store.
    with service.db.transaction() as s:
        sample = dict(service.scoped(s, "leads", p)[0].data)
        for i in range(300):
            person = put(
                s, "leads", p["tenant_id"], p["seller_id"], sample, key=f"scaling-lead-{i}", now=clock.now
            )
            put(
                s,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {
                    "lead_id": person.id,
                    "campaign_id": campaign.id,
                    "rep_id": p["user_id"],
                    "started_at": (clock.now - timedelta(days=1)).isoformat(),
                    "submitted_at": (clock.now - timedelta(days=1)).isoformat(),
                    "ended_at": (clock.now - timedelta(days=1)).isoformat(),
                    "outcome": "no_answer",
                    "state": "FINALIZED",
                },
                now=clock.now,
            )
            put(
                s,
                "experiment_assignments",
                p["tenant_id"],
                p["seller_id"],
                {
                    "epoch": epoch,
                    "lead_id": person.id,
                    "arm": "control" if i % 2 else "candidate",
                    "assigned_at": (clock.now - timedelta(days=2)).isoformat(),
                    "coverage_due_at": clock.now.isoformat(),
                    "goal": "meeting_held",
                    "outcome_window_days": 14,
                },
                key=epoch + ":" + person.id,
                now=clock.now,
            )
    project_pending(service, p)
    original = service.scoped

    def no_history(s, kind, who):
        assert kind not in {"call_attempts", "experiment_assignments"}, "Report scanned historical sources"
        return original(s, kind, who)

    monkeypatch.setattr(service, "scoped", no_history)
    result = service.optimization_report(p)
    assert (
        sum(a["leads"] for a in result["trial"]["arms"].values()) == 303
    )  # Original three enrolled leads remain in the denominator.
