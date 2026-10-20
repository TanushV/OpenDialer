"""Reconcile source-linked local commitments in the analysis transaction."""

import hashlib
import json
from collections import Counter

from app.db import emit, put, update

ACTIVE = {"open", "snoozed", "needs_confirmation"}


def identity_of(item):
    return (item["kind"], item["owner"], tuple(sorted(set(item["evidence"]))))


def publish(service, s, p, call, items, *, retracted_observations=()):
    """Returns the items rejected for review; an ambiguous item never aborts the batch."""
    lead = service.required(s, "leads", call.data["lead_id"], p)
    records = [
        r for r in service.lead_records(s, p, "commitments", lead.id) if r.data.get("call_id") == call.id
    ]
    tasks = service.lead_records(s, p, "followup_tasks", lead.id)
    seen, rejected = set(), []
    # Two items with one source identity cannot both be the same promise: reject both.
    shared = {key for key, count in Counter(identity_of(i) for i in items).items() if count > 1}
    for item in items:
        identity = identity_of(item)
        if identity in shared:
            rejected.append(item)
            continue
        key = "inferred:" + call.id + ":" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        candidates = [r for r in records if r.external_key == key]
        if not candidates:
            candidates = [
                r
                for r in records
                if r.id not in seen
                and r.data.get("kind") == item["kind"]
                and r.data.get("owner") == item["owner"]
                and set(r.data.get("evidence", [])) & set(item["evidence"])
            ]
        if len(candidates) > 1:
            rejected.append(item)  # Commitment identity needs human review.
            continue
        record = candidates[0] if candidates else None
        if record and (record.data.get("status") == "retracted" or record.data.get("source_retracted")):
            # Only new evidence makes a new promise; re-extraction never resurrects a retraction.
            seen.add(record.id)
            continue
        values = {
            **item,
            "lead_id": lead.id,
            "call_id": call.id,
            "privacy_contract": 1,
            "source_version": call.data["transcript_version"],
            "source_stale": False,
            "source_retracted": False,
            "status": "confirmed" if not item["needs_time_confirmation"] else "needs_confirmation",
            "interpreted_at": service.clock().isoformat(),
        }
        if record:
            update(record, **values)
        else:
            record = put(
                s, "commitments", p["tenant_id"], p["seller_id"], values, key=key, now=service.clock()
            )
            records.append(record)
        seen.add(record.id)
        task = next((t for t in tasks if t.data.get("commitment_id") == record.id), None)
        if not task and item["kind"] == "callback":
            task = next((t for t in tasks if t.external_key == "wrapup-callback:" + call.id), None)
        confirmed = bool(item["due_at"] and item["owner"] == "rep")
        task_values = {
            "commitment_id": record.id,
            "call_id": call.id,
            "lead_id": lead.id,
            "kind": item["kind"],
            "action": item["action"],
            "owner_id": call.data["rep_id"],
            "due_at": item["due_at"],
            "timezone": item["timezone"],
            "time_window": item["time_window"],
            "status": "open" if confirmed else "needs_confirmation",
            "confirmation_state": "confirmed" if confirmed else "suggested",
            "suggested_at": None if confirmed else service.clock().isoformat(),
            "source": "inferred",
            "evidence": item["evidence"],
            "source_version": call.data["transcript_version"],
            "source_stale": False,
            "source_retracted": False,
            "review_required": item["needs_time_confirmation"],
            "privacy_contract": 1,
        }
        if not task:
            task = put(
                s,
                "followup_tasks",
                p["tenant_id"],
                p["seller_id"],
                task_values,
                key="commitment:" + record.id,
                now=service.clock(),
            )
            tasks.append(task)
        elif task.data.get("human_edited") or task.data.get("source") == "rep_reported":
            update(
                task,
                commitment_id=record.id,
                suggested_update=task_values,
                review_required=True,
                confirmation_state="needs_review",
            )
        elif task.data["status"] in ACTIVE:
            update(task, **task_values)
        update(record, task_id=task.id, task_status=task.data["status"])
        emit(s, p["tenant_id"], "task.updated", task, service.clock())
    # Only explicit, independently reviewed retraction may remove a promise.
    for record in records:
        if record.data.get("observation_id") not in retracted_observations:
            continue
        update(
            record, status="retracted", source_retracted=True, source_version=call.data["transcript_version"]
        )
        for task in tasks:
            if task.data.get("commitment_id") != record.id:
                continue
            changes = {
                "source_retracted": True,
                "review_required": True,
                "confirmation_state": "needs_review",
            }
            if (
                not task.data.get("human_edited")
                and task.data.get("source") == "inferred"
                and task.data["status"] in ACTIVE
            ):
                changes["status"] = "cancelled"
            update(task, **changes)
            emit(s, p["tenant_id"], "task.updated", task, service.clock())
    service._sync_callback(s, p, lead, tasks=tasks)
    service.refresh_packet_in(s, p, lead)
    return rejected


def human_retracted(service, s, p, call, observation_ids):
    """Whether a human retraction (not a source correction) caused this invalidation."""
    record = service._by_key(s, p, "conversation_states", call.id)
    return any(
        item.get("id") in observation_ids
        and item.get("state") == "retracted"
        and item.get("epistemic_status") == "human_confirmed"
        for item in (record.data.get("observations", []) if record else [])
    )


def invalidate(service, s, p, call, segment_id=None, *, observation_ids=()):
    """A source correction asks for confirmation; a human retraction cancels inferred work."""
    lead = service.required(s, "leads", call.data["lead_id"], p)
    retracted = bool(observation_ids) and human_retracted(service, s, p, call, set(observation_ids))
    affected = {}
    for record in service.lead_records(s, p, "commitments", lead.id):
        if record.data.get("call_id") == call.id and (
            segment_id is not None
            and segment_id in record.data.get("evidence", [])
            or record.data.get("observation_id") in observation_ids
        ):
            update(record, source_stale=True, status="needs_confirmation")
            affected[record.id] = record
    if not affected:
        return
    tasks = service.lead_records(s, p, "followup_tasks", lead.id)
    for task in tasks:
        if task.data.get("commitment_id") not in affected:
            continue
        changes = {"source_stale": True, "review_required": True, "confirmation_state": "needs_review"}
        if (
            not task.data.get("human_edited")
            and task.data.get("source") == "inferred"
            and task.data["status"] in ACTIVE
        ):
            if retracted:
                changes["status"] = "cancelled"
                update(affected[task.data["commitment_id"]], status="cancelled", task_status="cancelled")
            else:
                changes.update(status="needs_confirmation", suggested_at=service.clock().isoformat())
        update(task, **changes)
        emit(s, p["tenant_id"], "task.updated", task, service.clock())
    service._sync_callback(s, p, lead, tasks=tasks)
    service.refresh_packet_in(s, p, lead)
