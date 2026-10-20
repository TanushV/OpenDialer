"""Bounded workspace previews and resumable encrypted history; no new search store."""

import hashlib
import hmac

from sqlalchemy import and_, func, literal_column, or_, select

from app.db import MODELS, ActiveClaim, Event, Job, doc
from app.ml.training import retired_reason
from app.operational_schema import PAGE_TABLES
from app.service import fail

PAGE_SIZE = 50
SEARCH_SCAN = 200
REGISTRY_FIELDS = {
    "id",
    "version",
    "kind",
    "npas",
    "refreshed_at",
    "checksum",
    "synthetic",
    "published",
    "count",
    "jurisdiction",
    "subscription_reference",
    "subscription_id",
    "authorization_expires_at",
}


def scoped_query(model, p):
    return select(model).where(model.tenant_id == p["tenant_id"], model.seller_id == p["seller_id"])


def after(model, anchor, descending):
    at, time = anchor.created_at, model.created_at
    return (
        or_(time < at, and_(time == at, model.id < anchor.id))
        if descending
        else or_(time > at, and_(time == at, model.id > anchor.id))
    )


class WorkspaceReads:
    def quiescence_in(self, s, p):
        """Indexed authority obligations; a local mode change stops the whole process."""
        from app.telephony.reducer import TERMINAL

        local = self.settings.mode == "local"
        reasons = []
        for table, condition, reason in (
            # Recovery metadata is non-null for every nonterminal/unknown call.
            ("call_attempts", MODELS["call_attempts"].op_due_at.is_not(None), "ACTIVE_CALL"),
            (
                "call_legs",
                MODELS["call_legs"].op_status.not_in([*TERMINAL, "ended", "failed", "cancelled"]),
                "ACTIVE_LEG",
            ),
            ("followup_tasks", MODELS["followup_tasks"].op_active.is_(True), "ACTIVE_INBOUND"),
            (
                "command_outbox",
                MODELS["command_outbox"].op_status.in_(
                    [
                        "sending",
                        "uncertain",
                        "reconciling",
                        "review_required",
                        "unconfirmed",
                    ]
                ),
                "EXTERNAL_OPERATION_UNRESOLVED",
            ),
        ):
            model = MODELS[table]
            query = select(model.id).where(model.tenant_id == p["tenant_id"], condition)
            if not local:
                # Reps block only on their own work. Admins additionally wait for seller-owned
                # admin effects (no rep owner), never for another rep's call commands, including
                # call commands written before they carried their call's rep.
                query = query.where(model.seller_id == p["seller_id"])
                if p["role"] in {"owner", "admin"} and table == "command_outbox":
                    seller_effect = and_(
                        model.op_owner_id.is_(None),
                        or_(model.op_kind.is_(None), model.op_kind.not_in(["dial", "hangup", "action"])),
                    )
                    query = query.where(or_(model.op_owner_id == p["user_id"], seller_effect))
                else:
                    query = query.where(model.op_owner_id == p["user_id"])
            if s.scalar(query.limit(1)):
                reasons.append(reason)
        return {"can_disconnect": not reasons, "reasons": reasons}

    def workspace_page(self, p, section, *, cursor=None, q="", filter="all", limit=PAGE_SIZE, session=None):
        if section not in PAGE_TABLES and section != "jobs":
            fail(404, "Workspace section not found")
        if len(q) > 200 or len(cursor or "") > 100 or (q and section != "leads"):
            fail(422, "Invalid history search")
        valid = {"all"}
        if section == "calls":
            valid |= {"unconfirmed", "wrong_party", "callback_requested", "meeting_held"}
        elif section == "tasks":
            valid |= {"open", "closed", "overdue"}
        if filter not in valid:
            fail(422, "Invalid history filter")
        with self.db.transaction(False, session=session) as s:
            # Recheck a changed role even for internal callers holding an old principal.
            user = self.required(s, "users", p["user_id"], p)
            if user.data.get("disabled"):
                fail(403, "Account is disabled")
            p = {**p, "role": "rep" if "rep" in {p["role"], user.data["role"]} else "admin"}
            if section == "jobs":
                return self._job_page(s, p, cursor, limit)
            model = MODELS[PAGE_TABLES[section]]
            query = scoped_query(model, p)
            if section in {"calls", "tasks"} and p["role"] == "rep":
                query = query.where(model.op_owner_id == p["user_id"])
            if section == "tasks":
                query = query.where(or_(model.op_status != "aliased", model.op_status.is_(None)))
            if section == "registries":
                query = query.where(model.ctx_key.in_(["national", "state"]), model.ctx_status != "staging")
            if section == "tasks" and filter != "all":
                closed = model.op_status.in_(["completed", "cancelled"])
                query = query.where(closed if filter == "closed" else or_(~closed, model.op_status.is_(None)))
                if filter == "overdue":
                    query = query.where(model.op_due_at <= self.clock())
            total = s.scalar(select(func.count()).select_from(query.with_only_columns(model.id).subquery()))
            descending = section not in {"leads", "playbooks", "trackers", "task_owners"}
            if cursor:
                anchor = s.scalar(query.where(model.id == cursor))
                if not anchor:
                    fail(404, "History cursor is no longer available; reload this view")
                query = query.where(after(model, anchor, descending))
            order = [model.created_at, model.id]
            query = query.order_by(*(c.desc() for c in order) if descending else order)
            # Encrypted substring search is deliberately incremental; no plaintext index.
            scan_limit = SEARCH_SCAN if q or (section == "calls" and filter != "all") else limit
            records = list(s.scalars(query.limit(scan_limit + 1)))
            examined, selected = [], []
            for row in records[:scan_limit]:
                examined.append(row)
                value = doc(row)
                if (
                    q.casefold()
                    not in " ".join(str(value.get(k, "")) for k in ("name", "company", "phone")).casefold()
                ):
                    continue
                if section == "calls" and filter != "all":
                    if (
                        bool(value.get("outcome"))
                        if filter == "unconfirmed"
                        else value.get("outcome") != filter
                    ):
                        continue
                if section == "task_owners":
                    if value.get("disabled"):
                        continue
                    value = {"id": row.id, "name": value["name"]}
                if section == "numbers":
                    value.update(self.number_readiness_in(s, p, row))
                if section == "registries":
                    value = {k: v for k, v in value.items() if k in REGISTRY_FIELDS}
                if section == "models" and (reason := retired_reason(value)):
                    value["serving_block"] = reason
                selected.append(value)
                if len(selected) >= limit:
                    break
            more = len(records) > len(examined)
            if section == "leads":
                self._mark_suppressed(s, p, selected)
            elif section in {"calls", "tasks"}:
                self._label_leads(s, p, selected)
            return {
                "items": selected,
                "total": total,
                "next_cursor": examined[-1].id if more and examined else None,
                "scanned": len(examined),
                "search_complete": not more,
                "filter": filter,
                "query": q,
            }

    def _mark_suppressed(self, s, p, leads):
        keys = {
            hmac.new(self.db.identity_key, lead["phone"].encode(), hashlib.sha256).hexdigest()
            for lead in leads
            if lead.get("phone")
        }
        model = MODELS["suppressions"]
        blocked = (
            {
                r.data["phone"]
                for r in s.scalars(scoped_query(model, p).where(model.external_key.in_(keys)))
                if r.data.get("active", True)
            }
            if keys
            else set()
        )
        for lead in leads:
            lead["suppressed"] = lead.get("phone") in blocked

    def _label_leads(self, s, p, records):
        ids = {r["lead_id"] for r in records if r.get("lead_id")}
        model = MODELS["leads"]
        known = (
            {r.id: r.data for r in s.scalars(scoped_query(model, p).where(model.id.in_(ids)))} if ids else {}
        )
        for item in records:
            lead = known.get(item.get("lead_id"), {})
            item.update(lead_name=lead.get("name"), lead_company=lead.get("company"))
            status = item.get("status")
            if (
                item.get("kind") == "callback"
                and item.get("source") == "inferred"
                and not item.get("human_edited")
                and status in {"open", "snoozed", "needs_confirmation"}
                and (status == "needs_confirmation" or not item.get("due_at"))
            ):
                # Mirrors the lead's callback sync: an unconfirmed AI suggestion holds
                # outreach only until this bounded deadline, unlike a rep-confirmed callback.
                item["callback_suggestion_until"] = lead.get("callback_suggestion_until")

    def _job_page(self, s, p, cursor, limit):
        fields = [Job.id, Job.queue, Job.status, Job.attempts, Job.error]
        query = select(*fields).where(Job.tenant_id == p["tenant_id"], Job.seller_id == p["seller_id"])
        if p["role"] == "rep":
            query = query.where(Job.owner_id == p["user_id"])
        if cursor:
            if not s.execute(query.where(Job.id == cursor)).first():
                fail(404, "Job cursor unavailable; reload")
            query = query.where(Job.id < cursor)
        records = list(s.execute(query.order_by(Job.id.desc()).limit(limit + 1)).mappings())
        items = [
            {**row, "error": "Job failed; inspect recovery status" if row["error"] else None}
            for row in records[:limit]
        ]
        return {
            "items": items,
            "total": None,
            "next_cursor": records[limit - 1]["id"] if len(records) > limit else None,
        }

    def snapshot(self, p):
        with self.db.transaction(False) as s:
            # Read the live cursor first: a later event may replay harmlessly, none is skipped.
            event_cursor = (
                s.scalar(
                    select(Event.sequence)
                    .where(Event.tenant_id == p["tenant_id"])
                    .order_by(Event.sequence.desc())
                    .limit(1)
                )
                or 0
            )
            campaign = self.scoped(s, "campaigns", p)[0]
            policy = self.required(s, "policy_versions", campaign.data["policy_id"], p)
            user = self.required(s, "users", p["user_id"], p)
            pages = {
                section: self.workspace_page(
                    p, section, session=s, limit=25 if section in {"calls", "tasks"} else PAGE_SIZE
                )
                for section in (*PAGE_TABLES, "jobs")
            }
            # Current responsibilities are independent of arbitrary history page boundaries.
            call_model = MODELS["call_attempts"]
            owed = list(
                s.scalars(
                    scoped_query(call_model, p)
                    .where(
                        call_model.op_owner_id == p["user_id"],
                        call_model.op_status != literal_column("'FINALIZED'"),
                    )
                    .order_by(call_model.created_at.desc(), call_model.id.desc())
                    .limit(50)
                )
            )
            task_model = MODELS["followup_tasks"]
            open_tasks = scoped_query(task_model, p).where(
                or_(
                    task_model.op_status.not_in(["completed", "cancelled", "aliased"]),
                    task_model.op_status.is_(None),
                )
            )
            if p["role"] == "rep":
                open_tasks = open_tasks.where(task_model.op_owner_id == p["user_id"])
            owed_tasks = list(
                s.scalars(
                    open_tasks.order_by(task_model.op_due_at.asc().nullsfirst(), task_model.id).limit(25)
                )
            )
            claim = s.scalar(
                select(ActiveClaim).where(
                    ActiveClaim.tenant_id == p["tenant_id"],
                    ActiveClaim.seller_id == p["seller_id"],
                    ActiveClaim.rep_id == p["user_id"],
                )
            )
            for section, extra in (
                ("calls", [doc(r) for r in owed]),
                ("tasks", [doc(r) for r in owed_tasks]),
            ):
                if claim and claim.call_id:
                    row = s.scalar(
                        scoped_query(MODELS[PAGE_TABLES[section]], p).where(
                            MODELS[PAGE_TABLES[section]].id == claim.call_id
                        )
                    )
                    if row and all(item["id"] != row.id for item in extra):
                        extra.append(doc(row))
                self._label_leads(s, p, extra)
                present = {item["id"] for item in pages[section]["items"]}
                pages[section]["items"] += [item for item in extra if item["id"] not in present]
            current = {
                "mode": self.settings.mode,
                "simulation": self.settings.simulation,
                "seller": doc(self.required(s, "sellers", p["seller_id"], p)),
                "campaign": doc(campaign),
                "policy": doc(policy),
                "user": {
                    "id": user.id,
                    "name": user.data["name"],
                    "role": user.data["role"],
                    "presence": user.data.get("presence", {}),
                },
                "quiescence": self.quiescence_in(s, p),
                "event_cursor": event_cursor,
                **{section: value["items"] for section, value in pages.items()},
                "active_playbook": doc(book) if (book := self.approved_playbook(s, p)) else None,
                "pages": {
                    section: {k: v for k, v in value.items() if k != "items"}
                    for section, value in pages.items()
                },
            }
        return {**current, "metrics": self.cached_home_metrics(p), "readiness": self.preflight(p, full=False)}
