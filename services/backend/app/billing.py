"""Local receipt review; never invokes a provider or treats a partial CDR as a final invoice."""

import math
from decimal import Decimal

from sqlalchemy import select

from app.compliance.operating import COSTS
from app.platform.db import MODELS, update
from app.service import dt


def scoped(kind, p):
    table = MODELS[kind]
    return select(table).where(table.tenant_id == p["tenant_id"], table.seller_id == p["seller_id"])


def by_key(s, kind, p, key):
    return s.scalar(scoped(kind, p).where(MODELS[kind].external_key == key))


NOT_PLACED = {
    "carrier_not_submitted",
    "carrier_channel_limit",
    "operator_confirmed_not_placed",
    "unsent_intent_cancelled",
}


def account_rate(service, s, p, component, field):
    """The seller's invoiced account rate when saved, else the dated reference rate."""
    seller = service.required(s, "sellers", p["seller_id"], p)
    saved = seller.data.get("operating_policy", {}).get("account_rates", {}).get(component)
    value = (saved or {}).get("values", {}).get(field)
    return Decimal(str(value if value is not None else COSTS[component][field]))


def call_estimate(service, s, p, call, fallback):
    """Estimated carrier cost of one finished call from its leg time and the cost register.

    Every leg minute is rounded up (ringing included) on the prospect leg and, while linked, the
    rep's agent leg; each requested transcription stream bills the connected minutes.
    """
    submitted, ended = dt(call.data.get("submitted_at")), dt(call.data.get("ended_at"))
    if not submitted or not ended or ended < submitted:
        return Decimal(str(fallback)), "configured_per_call_estimate"
    legs = 2 if call.data.get("agent_provider_id") else 1
    minutes = max(1, math.ceil((ended - submitted).total_seconds() / 60))
    total = legs * minutes * account_rate(service, s, p, "voice", "api_per_minute_usd")
    if call.data.get("transcription_layout") in {"leg_inbound", "direct_tracks"}:
        connected = math.ceil((call.data.get("connected_seconds") or 0) / 60)
        total += 2 * connected * account_rate(service, s, p, "stt", "soniox_per_minute_usd")
    return total, "call_duration_register_rates"


def settle_call_estimate(service, s, p, call):
    """A terminal call never keeps an open-ended hold: settle to a labeled estimate.

    A dial that never reached the carrier is released. A carrier statement or
    `close_call` later replaces the estimate with receipts.
    """
    row = by_key(s, "usage_ledger", p, "call:" + call.id)
    if not row or row.data.get("cost_usd") is not None or row.data.get("status") in {"settled", "released"}:
        return
    if call.data.get("end_reason") in NOT_PLACED or not (
        call.data.get("submitted_at") or call.data.get("provider_id")
    ):
        update(row, status="released", cost_usd=0, reserved_usd=0, billing_verified=False)
        return
    cost, source = call_estimate(service, s, p, call, row.data.get("reserved_usd") or 0.05)
    update(
        row,
        status="estimated",
        cost_usd=float(cost.quantize(Decimal("0.000001"))),
        reserved_usd=0,
        estimate_usd=row.data.get("estimate_usd", row.data.get("reserved_usd", 0.05)),
        billing_verified=False,
        estimate_source=source,
        duration_seconds=call.data.get("connected_seconds"),
    )
