"""Reversible contact/list controls and one shared membership predicate for both stores."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, exists, func, or_, select

from app.compliance.policy import authorize
from app.optimization.geography import normalize_state
from app.optimization.numbering import select_number, timestamp
from app.platform.db import MODELS, audit, doc, emit, update


def memberships(lead):
    return list(lead["list_names"]) if "list_names" in lead else [lead.get("list_name", "")]


def budget_scope(campaign, name):
    return campaign.get("lists", {}).get(name, {}).get("budget_scope", "list:" + name)


def in_list(model, name):
    values = func.json_each(model.data, "$.list_names").table_valued("value")
    return or_(
        exists(select(1).select_from(values).where(values.c.value == name)),
        and_(
            model.data["list_names"].as_string().is_(None),
            func.coalesce(model.data["list_name"].as_string(), "") == name,
        ),
    )


def active_membership(model, removed_names):
    values = func.json_each(model.data, "$.list_names").table_valued("value")
    return or_(
        exists(select(1).select_from(values).where(values.c.value.not_in(removed_names))),
        and_(
            model.data["list_names"].as_string().is_(None),
            func.coalesce(model.data["list_name"].as_string(), "").not_in(removed_names),
        ),
    )


def set_memberships(lead, names):
    names = list(dict.fromkeys(names))
    update(lead, list_names=names, list_name=next(iter(names), ""))


class LeadLists:
    def preparation_budget_scope(self, p, name):
        with self.db.transaction(False) as s:
            return budget_scope(self.selected_campaign(s, p).data, name)

    def list_permitted(self, s, p, lead):
        value = lead.data
        if value.get("archived") or value.get("dial_paused"):
            return False
        campaign = self.required(s, "campaigns", value["campaign_id"], p)
        catalog = campaign.data.get("lists", {})
        names = memberships(value)
        if p.get("callback_lead_id"):
            due = timestamp(value.get("callback_due_at"))
            return bool(
                lead.id == p["callback_lead_id"]
                and due
                and due <= self.clock()
                and any(not catalog.get(name, {}).get("archived") for name in names)
            )
        selected = p.get("dial_lists")
        return any(
            (selected is None or name in selected)
            and not catalog.get(name, {}).get("paused")
            and not catalog.get(name, {}).get("archived")
            for name in names
        )

    def list_summary(self, p, list_names=None):
        with self.db.transaction(False) as s:
            campaign = self.scoped(s, "campaigns", p)[0]
            model = MODELS["leads"]
            leads = list(
                s.scalars(
                    select(model).where(
                        model.tenant_id == p["tenant_id"],
                        model.seller_id == p["seller_id"],
                        model.op_campaign_id == campaign.id,
                    )
                )
            )
            self._queue_context(s, p, campaign.id, leads)
            grouped = {}
            for lead in leads:
                if key := lead.data.get("company_key"):
                    grouped.setdefault(key, []).append(lead)
            s.info["list_company_siblings"] = grouped
            catalog = campaign.data.get("lists", {})
            names = sorted(set(catalog) | {name for lead in leads for name in memberships(lead.data)})
            inventory = self.inventory_evidence(s, p, fast=True)
            numbers = {n.id: n for n in self.scoped(s, "number_inventory", p)}
            bindings = {
                r.data["lead_id"]: r.data["number_id"] for r in self.scoped(s, "lead_number_bindings", p)
            }
            result = {
                name: {
                    "name": name,
                    "paused": False,
                    "archived": False,
                    **catalog.get(name, {}),
                    **dict.fromkeys(
                        ("total", "ready", "preparing", "callable_now", "held", "completed", "callbacks_due"),
                        0,
                    ),
                }
                for name in names
            }
            outside = []
            policy = self.required(s, "policy_versions", campaign.data["policy_id"], p).data
            for lead in leads:
                value = lead.data
                due = timestamp(value.get("callback_due_at"))
                callback = bool(due and due <= self.clock())
                selected_p = {**p, "dial_lists": list_names}
                if callback and not self.list_permitted(s, selected_p, lead):
                    explicit = {**p, "callback_lead_id": lead.id}
                    outside.append(
                        {
                            "id": lead.id,
                            "name": value.get("name", ""),
                            "phone": value["phone"],
                            "list_names": memberships(value),
                            "due_at": value["callback_due_at"],
                            "blocked": not self.list_permitted(s, explicit, lead),
                        }
                    )
                allocation = select_number(doc(lead), inventory, bound_id=bindings.get(lead.id))
                number = numbers.get(allocation["number_id"])
                for name in memberships(value):
                    row = result[name]
                    row["total"] += 1
                    row["preparing"] += value.get("status") == "preparing"
                    row["ready"] += value.get("status") == "eligible"
                    held = bool(
                        value.get("outreach_hold")
                        or value.get("dial_paused")
                        or value.get("archived")
                        or row["paused"]
                        or row["archived"]
                    )
                    row["held"] += held
                    row["completed"] += bool(
                        value.get("conversion_goal_achieved")
                        or value.get("attempt_count", 0) >= policy.get("max_attempts", 4)
                    )
                    row["callbacks_due"] += callback
                    context = self.context(s, {**p, "dial_lists": [name]}, lead, number)
                    reasons = set(authorize(context).reasons) - {"REP_NOT_READY", "AUDIO_NOT_READY"}
                    row["callable_now"] += (
                        value.get("status") == "eligible"
                        and not reasons
                        and not self._company_contacted(s, p, lead, s.info["active_claims"], self.clock())
                        and lead.id not in {claim.lead_id for claim in s.info["active_claims"]}
                    )
            return {
                "lists": list(result.values()),
                "outside_callbacks": sorted(outside, key=lambda row: row["due_at"]),
                "as_of": self.clock().isoformat(),
            }

    def manage_list(self, p, body):
        from app.service import fail

        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            campaign = self.required(s, "campaigns", body["campaign_id"], p, lock=True)
            name, action = body["list_name"], body["action"]
            catalog = dict(campaign.data.get("lists", {}))
            model = MODELS["leads"]
            leads = list(
                s.scalars(
                    select(model).where(
                        model.tenant_id == p["tenant_id"],
                        model.seller_id == p["seller_id"],
                        model.op_campaign_id == campaign.id,
                        in_list(model, name),
                    )
                )
            )
            if not leads and name not in catalog:
                fail(404, "List not found")
            state = dict(catalog.get(name, {}))
            if action == "rename":
                new = (body.get("name") or "").strip()
                if not new:
                    fail(422, "Enter a list name")
                if new != name and (
                    new in catalog
                    or s.scalar(
                        select(model.id)
                        .where(
                            model.tenant_id == p["tenant_id"],
                            model.seller_id == p["seller_id"],
                            model.op_campaign_id == campaign.id,
                            in_list(model, new),
                        )
                        .limit(1)
                    )
                ):
                    fail(409, "That list name already exists")
                state.setdefault("budget_scope", budget_scope(campaign.data, name))
                catalog.pop(name, None)
                catalog[new] = state
                for lead in leads:
                    set_memberships(lead, [new if n == name else n for n in memberships(lead.data)])
                    emit(s, p["tenant_id"], "lead.updated", lead, self.clock())
            else:
                state["paused" if action in {"pause", "resume"} else "archived"] = action in {
                    "pause",
                    "archive",
                }
                catalog[name] = state
            update(campaign, lists=catalog)
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "list." + action,
                campaign.id,
                self.clock(),
                {"list_name": name},
            )
            emit(s, p["tenant_id"], "campaign.updated", campaign, self.clock())
        return {"saved": True}

    def bulk_leads(self, p, body):
        from app.service import fail

        action = body["action"]
        if action in {"move", "add", "remove"} and body.get("list_name") is None:
            fail(422, "Choose a list")
        if action == "location":
            if not body.get("timezone") and not body.get("state"):
                fail(422, "Enter a time zone or state")
            if body.get("timezone"):
                try:
                    ZoneInfo(body["timezone"])
                except (ZoneInfoNotFoundError, ValueError):
                    fail(422, "Use a valid IANA time zone")
            if body.get("state") and not normalize_state(body["state"]):
                fail(422, "Use a valid US state")
        with self.db.transaction() as s:
            self.db.authorize_stream(s, p)
            leads = [
                self.required(s, "leads", identity, p, lock=True)
                for identity in dict.fromkeys(body["lead_ids"])
            ]
            requested = len(leads)
            if action == "remove":
                name = body["list_name"].strip()
                leads = [lead for lead in leads if name in memberships(lead.data)]
                if not leads:
                    fail(422, "None of the selected leads belong to that list. Choose a list they belong to.")
            for lead in leads:
                if action in {"pause", "resume", "archive", "restore"}:
                    update(
                        lead,
                        **{
                            "dial_paused" if action in {"pause", "resume"} else "archived": action
                            in {"pause", "archive"}
                        },
                    )
                elif action == "location":
                    if body.get("timezone"):
                        update(lead, timezone=body["timezone"], timezone_confidence="confirmed")
                    if body.get("state"):
                        update(lead, state=normalize_state(body["state"]), state_source="provided")
                else:
                    name = body["list_name"].strip()
                    previous = memberships(lead.data)
                    set_memberships(
                        lead,
                        [name]
                        if action == "move"
                        else [n for n in previous if n != name]
                        if action == "remove"
                        else [*previous, name],
                    )
                    if action != "remove":
                        campaign = self.required(s, "campaigns", lead.data["campaign_id"], p, lock=True)
                        update(campaign, lists={name: {}, **campaign.data.get("lists", {})})
                emit(s, p["tenant_id"], "lead.updated", lead, self.clock())
            audit(
                s,
                p["tenant_id"],
                p["seller_id"],
                p["user_id"],
                "leads." + action,
                p["seller_id"],
                self.clock(),
                {"count": len(leads)},
            )
            return {
                "updated": len(leads),
                "unchanged": requested - len(leads),
                "leads": [doc(lead) for lead in leads],
            }
