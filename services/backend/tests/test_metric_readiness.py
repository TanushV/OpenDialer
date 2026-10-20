"""E09: publication waits for all due units and recovers disposable state."""

from datetime import timedelta

from app.db import MODELS, put
from sqlalchemy import delete, select
from test_projection_replay import active_total, finish_rebuild
from test_remediation_privacy import principal


def initialized(service, p):
    from app.optimization import projections

    projections.rebuild_page(service, p, start=True)
    finish_rebuild(service, p)
    assert service.prepare_metric_read(p)


def test_missing_disposable_totals_restart_rebuild_even_with_old_pointer(workspace, app):
    service, p = app.state.service, principal(workspace)
    initialized(service, p)
    before, totals = active_total(service, p)
    assert before["as_of"] and totals["original_cohort"] == 3
    with service.db.transaction() as session:
        for table in ("metric_contributions", "metric_buckets"):
            session.execute(delete(MODELS[table]))
    assert not service.prepare_metric_read(p), "Restored pointers cannot certify absent derived rows"
    finish_rebuild(service, p)
    assert active_total(service, p)[1]["original_cohort"] == 3


def test_restore_during_partial_rebuild_cannot_reuse_a_cursor_without_its_rows(workspace, app):
    from app.optimization.projections import rebuild_page

    service, p = app.state.service, principal(workspace)
    initialized(service, p)
    backup = service.backup(p)
    page = rebuild_page(service, p, start=True)
    assert page["processed"] == 3 and page["cursor"]["phase"] == 1 and not page["published"]
    service.restore(p, backup["backup_id"])
    assert not service.prepare_metric_read(p)
    finish_rebuild(service, p)
    assert active_total(service, p)[1]["original_cohort"] == 3


def test_empty_workspace_has_a_ready_zero_total_without_endless_rebuild(workspace, app):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as session:
        for lead in service.scoped(session, "leads", p):
            session.delete(lead)
    initialized(service, p)
    pointer, totals = active_total(service, p)
    assert totals.get("original_cohort") == 0, "An empty rebuilt workspace still has a total bucket"
    for _ in range(3):
        assert service.prepare_metric_read(p)
        assert active_total(service, p)[0] == pointer


def test_more_than_one_due_page_cannot_publish_a_partial_current_total(workspace, app, clock):
    service, p = app.state.service, principal(workspace)
    with service.db.transaction() as session:
        lead = service.scoped(session, "leads", p)[0]
        for i in range(101):
            put(
                session,
                "call_attempts",
                p["tenant_id"],
                p["seller_id"],
                {
                    "lead_id": lead.id,
                    "campaign_id": workspace["campaign_id"],
                    "started_at": (clock.now - timedelta(days=13)).isoformat(),
                    "submitted_at": (clock.now - timedelta(days=13)).isoformat(),
                    "state": "ENDED",
                    "outcome": "no_answer",
                    "outcome_available_at": clock.now.isoformat(),
                },
                key=f"maturity-page:{i}",
                now=clock.now,
            )
    initialized(service, p)
    assert active_total(service, p)[1]["pending_outcome_windows"] == 101
    clock.now += timedelta(days=2)
    assert not service.prepare_metric_read(p), "The first hundred matured calls are not the whole total"
    assert service.prepare_metric_read(p)
    assert active_total(service, p)[1]["pending_outcome_windows"] == 0


def test_namespace_range_uses_ascii_order_on_both_authorities(workspace, app):
    service, p = app.state.service, principal(workspace)
    initialized(service, p)
    pointer, _ = active_total(service, p)
    prefix = f"v{pointer['active_definition']}:{pointer['active_namespace']}:"
    with service.db.transaction(False) as session:
        model = MODELS["metric_contributions"]
        identities = list(
            session.scalars(
                select(model.id).where(
                    model.tenant_id == p["tenant_id"],
                    model.seller_id == p["seller_id"],
                    model.external_key >= prefix,
                    model.external_key < prefix + "~",
                )
            )
        )
    assert len(identities) == 3, "Locale collation must not move hash suffixes outside the namespace range"


def test_dead_source_unit_is_surfaced_without_blocking_publication(workspace, app):
    from app.db import Job, update

    service, p = app.state.service, principal(workspace)
    initialized(service, p)
    with service.db.transaction() as session:
        lead = service.scoped(session, "leads", p)[0]
        update(lead, status="invalid")
    # Dirty units are queued at the source's commit; this one then fails outright.
    with service.db.transaction() as session:
        job = session.scalar(select(Job).where(Job.key == f"projection:{p['seller_id']}:lead:{lead.id}"))
        job.status, job.error = "dead", "Synthetic source failure"
    # The failed unit is surfaced; it does not block every other source's totals.
    assert service.rebuild_home_metrics(p)["status"] == "ready"
    projection = service.cached_home_metrics(p)["projection"]
    assert projection["failed_sources"] == 1 and projection["status"] != "pending"
