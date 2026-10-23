"""One budget liability per provider attempt, settled by an attributable receipt."""

import math

from sqlalchemy import select

from app.db import MODELS, put, update
from app.operational_schema import receipt_key


class RequestAccounting:
    def _request_hold(self, s, p, request_key):
        model = MODELS["usage_ledger"]
        return s.scalar(
            select(model).where(
                model.tenant_id == p["tenant_id"],
                model.seller_id == p["seller_id"],
                model.external_key == "optional:" + request_key,
            )
        )

    def reserve_optional_budget(self, p, request_key, estimate_usd, *, object_id=None, work_kind=None):
        with self.db.transaction() as s:
            return self.reserve_budget_in(
                s, p, request_key, estimate_usd, object_id=object_id, work_kind=work_kind
            )

    def reserve_budget_in(self, s, p, request_key, estimate_usd, *, object_id=None, work_kind=None):
        if (
            not isinstance(request_key, str)
            or not request_key
            or isinstance(estimate_usd, bool)
            or not isinstance(estimate_usd, (int, float))
            or not math.isfinite(estimate_usd)
            or estimate_usd <= 0
        ):
            raise ValueError("Optional work requires a request identity and positive reserved estimate")
        # Serialize only this seller's budget/authorization stream, never provider I/O.
        self.db.authorize_stream(s, p)
        prior = self._request_hold(s, p, request_key)
        if prior:
            data = prior.data
            if data.get("object_id") != object_id or data.get("work_kind") != work_kind:
                raise ValueError("Request identity belongs to different work")
            if data.get("status") != "reserved":
                return False
            if data.get("estimate_usd", data.get("reserved_usd")) != estimate_usd:
                raise ValueError("Request identity already has a different budget")
            return True
        # A second admission in this transaction must include the preceding writes.
        s.info.pop(("budget_totals", p["tenant_id"], p["seller_id"]), None)
        if not self.budget_ok(s, p, estimate_usd):
            return False
        put(
            s,
            "usage_ledger",
            p["tenant_id"],
            p["seller_id"],
            {
                "kind": "optional_work_reservation",
                "object_id": object_id,
                "work_kind": work_kind,
                "user_id": p.get("user_id"),
                "request_key": request_key,
                "estimate_usd": estimate_usd,
                "reserved_usd": estimate_usd,
                "at": self.clock().isoformat(),
                "cost_usd": None,
                "billing_verified": False,
                "status": "reserved",
            },
            key="optional:" + request_key,
            now=self.clock(),
        )
        # Admission precedes every possible submission; a crash may happen on either side
        # of the network write. Only release_unsubmitted_budget proves it stayed unsent.
        return True

    def usage(self, p, provider, kind, result, *, object_id, request_key, reserved=0.01, session=None):
        from app.privacy import usage_metadata

        if not isinstance(request_key, str) or not request_key:
            raise ValueError("Usage must identify its original local request")
        usage = usage_metadata(result.usage)
        cost = usage.get("cost", usage.get("cost_usd"))
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(cost)
            or cost < 0
        ):
            cost = None
        # A request included in the user's own ChatGPT plan has no marginal charge; the plan's
        # subscription is the owner's existing cost, so this is a labeled estimate, not a bill.
        plan = provider == "chatgpt_plan"
        if plan:
            cost = 0.0
        if (
            isinstance(reserved, bool)
            or not isinstance(reserved, (int, float))
            or not math.isfinite(reserved)
            or reserved < 0
        ):
            raise ValueError("A finite nonnegative liability is required")
        with self.db.transaction(session=session) as s:
            self.db.authorize_stream(s, p)
            model = MODELS["usage_ledger"]
            receipts = (
                list(
                    s.scalars(
                        select(model)
                        .where(
                            model.tenant_id == p["tenant_id"],
                            model.seller_id == p["seller_id"],
                            model.op_provider_id == receipt_key(provider, result.request_id),
                        )
                        .limit(2)
                    )
                )
                if result.request_id
                else []
            )
            if len(receipts) > 1:
                raise ValueError("Duplicate historical provider receipts require reconciliation")
            receipt = receipts[0] if receipts else None
            hold = self._request_hold(s, p, request_key)
            if receipt and (not hold or receipt.id != hold.id):
                raise ValueError("Provider receipt belongs to a different request")
            target = receipt or hold
            if target:
                data = target.data
                if (
                    data.get("object_id") != object_id
                    or data.get("work_kind", data.get("kind")) != kind
                    or data.get("provider")
                    and data["provider"] != provider
                    or data.get("request_id")
                    and result.request_id
                    and data["request_id"] != result.request_id
                ):
                    raise ValueError("Provider receipt attribution conflict")
                if data.get("estimate_source") and cost is None:
                    return target.id  # An unpriced receipt cannot erase the standing estimate.
                if data.get("cost_usd") is not None and not data.get("estimate_source"):
                    if cost is not None and cost != data["cost_usd"]:
                        raise ValueError("Conflicting cost receipt requires reconciliation")
                    return target.id
            values = {
                "provider": provider,
                "kind": kind,
                "work_kind": kind,
                "object_id": object_id,
                "request_id": result.request_id or (target.data if target else {}).get("request_id"),
                "usage": usage,
                "received_at": self.clock().isoformat(),
                "reserved_usd": 0
                if cost is not None
                else max(reserved, (target.data if target else {}).get("reserved_usd", 0)),
                "cost_usd": cost,
                "billing_verified": cost is not None and not plan,
                "status": "settled" if cost is not None else "uncertain",
                "estimate_source": "chatgpt_plan_included" if plan else None,
            }
            if target:
                update(target, **values)
            else:
                target = put(
                    s,
                    "usage_ledger",
                    p["tenant_id"],
                    p["seller_id"],
                    {**values, "request_key": request_key, "at": self.clock().isoformat()},
                    key="optional:" + request_key,
                    now=self.clock(),
                )
            return target.id

    def settle_estimate_in(self, s, p, request_key, cost, source):
        """A definitive answer without a priced receipt: the hold becomes a labeled estimate.

        It counts against budgets at the estimate instead of holding forever; a later provider
        receipt replaces it through `usage` or billing reconciliation.
        """
        self.db.authorize_stream(s, p)
        hold = self._request_hold(s, p, request_key)
        if not hold or hold.data.get("cost_usd") is not None or hold.data.get("status") == "released":
            return hold
        update(
            hold,
            cost_usd=cost,
            reserved_usd=0,
            status="estimated",
            billing_verified=False,
            estimate_source=source,
            received_at=self.clock().isoformat(),
        )
        return hold

    def release_refused_budget(self, p, request_key, reason):
        """The provider refused before doing any work, so the request carries no charge."""
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            hold = self._request_hold(s, p, request_key)
            if hold and hold.data.get("cost_usd") is None and hold.data.get("status") != "released":
                update(
                    hold,
                    status="released",
                    cost_usd=0,
                    reserved_usd=0,
                    billing_verified=False,
                    review_reason=reason,
                )

    def release_unsubmitted_budget(self, p, request_key):
        """Use only when no provider request was submitted; timeouts remain liabilities."""
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            target = self._request_hold(s, p, request_key)
            if target and target.data.get("status") == "reserved" and not target.data.get("request_id"):
                update(target, status="released", cost_usd=0, reserved_usd=0, billing_verified=False)
