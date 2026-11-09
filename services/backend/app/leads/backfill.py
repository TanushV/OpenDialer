"""Leads imported before the column map (R9.3) get its fields from the row they kept, once, at startup.

Rows from before 1.1 were stored whole (up to 60 columns), so the map can still read their signals, profile, number
source and live status. A lead that already has a profile is never touched again.
"""

import logging

from sqlalchemy import select

from app.leads import intake
from app.platform.db import MODELS, update

log = logging.getLogger(__name__)


def backfill_leads(db):
    """Fill mapped fields on every lead that lacks them; returns how many changed."""
    changed = 0
    with db.transaction() as s:
        for lead in s.scalars(select(MODELS["leads"])):
            if "profile" in lead.data:
                continue
            row = (lead.data["row"] if "row" in lead.data else None) or {}
            fields = intake.read(row, intake.guess_map(list(row)))
            update(
                lead,
                profile=fields["profile"],
                signals=fields["signals"],
                number_source=(lead.data["number_source"] if "number_source" in lead.data else None)
                or fields["number_source"],
                live_status=fields["live_status"],
            )
            changed += 1
    if changed:
        log.info("Filled mapped fields on %d earlier leads", changed)
    return changed
