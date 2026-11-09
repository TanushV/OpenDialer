"""R9.3: leads imported before the column map get its fields from their stored row, once."""

from app.leads.backfill import backfill_leads
from app.platform.db import MODELS, put, update
from sqlalchemy import select


def test_earlier_leads_get_signals_profile_and_live_status_from_their_row(app, workspace):
    db = app.state.db
    with db.transaction() as s:
        tenant = s.scalars(select(MODELS["campaigns"])).first()
        assert tenant is not None
        lead = put(
            s,
            "leads",
            tenant.tenant_id,
            tenant.seller_id,
            {
                "name": "Old Row",
                "row": {
                    "name": "Old Row",
                    "title": "Head of Sales",
                    "network status": "DEAD",
                    "finder": "wiza",
                    "signal summaries": "Hiring SDRs | New office",
                    "signal urls": "https://o.example/a; https://o.example/b",
                },
                "signals": [],
            },
        )
        identity = lead.id
    assert backfill_leads(db) >= 1
    with db.transaction(False) as s:
        data = s.get(MODELS["leads"], identity).data
    assert data["live_status"] == "dead"
    assert data["number_source"] == "wiza"
    assert data["profile"]["title"] == "Head of Sales"
    assert [item["url"] for item in data["signals"]["items"]] == [
        "https://o.example/a",
        "https://o.example/b",
    ]
    with db.transaction() as s:
        update(s.get(MODELS["leads"], identity), live_status="checked-by-hand")
    backfill_leads(db)
    with db.transaction(False) as s:
        assert s.get(MODELS["leads"], identity).data["live_status"] == "checked-by-hand"
