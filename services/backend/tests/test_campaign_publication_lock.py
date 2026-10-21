"""Background publishers lock the campaign before reading it (PostgreSQL).

The shared load profile lost a learning-refresh attempt to StaleDataError: the job read the
campaign, another writer committed a newer version, and the optimistic version check failed.
A concurrent campaign edit must instead wait for the publisher's short transaction."""

import threading

import pytest
from app.db import MODELS
from app.optimization import workflows
from test_remediation_rep_workflow import p

CAMPAIGN = MODELS["campaigns"]


def edit_concurrently(service, campaign_id):
    """Another session's locked edit, as any campaign writer makes it."""
    committed = threading.Event()

    def edit():
        with service.db.transaction() as s:
            row = s.get(CAMPAIGN, campaign_id, with_for_update=True)
            row.data = {**row.data, "concurrent_edit": True}
            row.version += 1
        committed.set()

    thread = threading.Thread(target=edit, daemon=True)
    thread.start()
    # A held row lock makes the edit wait; an unlocked read lets it commit first.
    return thread, committed.wait(1.0)


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("entry", "field"),
    [
        ("refresh_learning", "conversation_report"),
        ("save_operational_dataset", "dataset_summaries"),
        ("refresh_optimization", "selection_profile"),
    ],
)
def test_a_campaign_publisher_holds_the_row_so_a_concurrent_edit_waits(
    workspace, app, monkeypatch, entry, field
):
    service, who = app.state.service, p(workspace)
    with service.db.transaction() as s:
        row = s.get(CAMPAIGN, workspace["campaign_id"])
        row.data = {**row.data, "optimization": {**row.data.get("optimization", {}), "auto_train": False}}
        row.version += 1
    original, seen = workflows.update, {}

    def publishing(obj, **data):
        if isinstance(obj, CAMPAIGN) and field in data and "thread" not in seen:
            seen["thread"], seen["committed_first"] = edit_concurrently(service, obj.id)
        return original(obj, **data)

    monkeypatch.setattr(workflows, "update", publishing)
    getattr(service, entry)(who)
    seen["thread"].join(10)
    assert not seen["thread"].is_alive()
    assert seen["committed_first"] is False, "The concurrent edit must wait for the publisher"
    with service.db.transaction(False) as s:
        data = s.get(CAMPAIGN, workspace["campaign_id"]).data
    assert field in data and data["concurrent_edit"] is True, "Both writes survive, in order"
