"""One budget liability per provider attempt, settled by an attributable receipt."""

import math

from app.db import put, update


class RequestAccounting:
    def reserve_optional_budget(self, p, request_key, estimate_usd, *, object_id=None, work_kind=None):
        if not math.isfinite(estimate_usd) or estimate_usd <= 0:
            raise ValueError("Optional work requires a positive reserved estimate")
        with self.db.transaction() as s:
            prior = next(
                (r for r in self.scoped(s, "usage_ledger", p) if r.external_key == "optional:" + request_key),
                None,
            )
            if prior:
                return True
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
                    "reserved_usd": estimate_usd,
                    "at": self.clock().isoformat(),
                    "cost_usd": None,
                    "billing_verified": False,
                    "status": "reserved",
                },
                key="optional:" + request_key,
                now=self.clock(),
            )
            return True

    def usage(self, p, provider, kind, result, *, object_id, reserved=0.01, request_key=None):
        usage = result.usage or {}
        cost = usage.get("cost", usage.get("cost_usd"))
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(cost)
            or cost < 0
        ):
            cost = None
        with self.db.transaction() as s:
            entries = self.scoped(s, "usage_ledger", p)
            receipt = next(
                (
                    r
                    for r in entries
                    if result.request_id
                    and r.data.get("request_id") == result.request_id
                    and r.data.get("provider") == provider
                ),
                None,
            )
            hold = next(
                (r for r in entries if request_key and r.data.get("request_key") == request_key), None
            )
            if receipt and hold and receipt.id != hold.id:
                raise ValueError("Provider receipt belongs to a different request")
            if receipt and (receipt.data.get("object_id") != object_id or receipt.data.get("kind") != kind):
                raise ValueError("Provider receipt attribution conflict")
            if not hold and not receipt and not request_key:
                # Legacy callers may settle a single unambiguous hold, never guess among retries.
                candidates = [
                    r
                    for r in entries
                    if r.data.get("object_id") == object_id
                    and r.data.get("work_kind") == kind
                    and r.data.get("status") == "reserved"
                ]
                if len(candidates) == 1:
                    hold = candidates[0]
            target = receipt or hold
            if target and target.data.get("cost_usd") is not None:
                if cost is not None and cost != target.data["cost_usd"]:
                    raise ValueError("Conflicting cost receipt requires reconciliation")
                return target.id
            values = {
                "provider": provider,
                "kind": kind,
                "work_kind": kind,
                "object_id": object_id,
                "request_id": result.request_id,
                "usage": usage,
                "received_at": self.clock().isoformat(),
                "reserved_usd": 0
                if cost is not None
                else max(reserved, (target.data if target else {}).get("reserved_usd", 0)),
                "cost_usd": cost,
                "billing_verified": cost is not None,
                "status": "settled" if cost is not None else "uncertain",
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
                    key="optional:" + request_key if request_key else None,
                    now=self.clock(),
                )
            return target.id

    def release_unsubmitted_budget(self, p, request_key):
        """Use only when no provider request was submitted; timeouts remain liabilities."""
        with self.db.transaction() as s:
            target = next(
                (r for r in self.scoped(s, "usage_ledger", p) if r.data.get("request_key") == request_key),
                None,
            )
            if target and target.data.get("status") == "reserved" and not target.data.get("request_id"):
                update(target, status="released", cost_usd=0, reserved_usd=0, billing_verified=False)
