"""Reconcile source-linked local commitments in the analysis transaction."""

import hashlib
import json

from app.db import emit, put, update

ACTIVE = {"open", "snoozed", "needs_confirmation"}


def publish(service, s, p, call, items, *, retracted_observations=()):
    lead = service.required(s, "leads", call.data["lead_id"], p)
    records = [
        r for r in service.lead_records(s, p, "commitments", lead.id) if r.data.get("call_id") == call.id
    ]
    tasks = service.lead_records(s, p, "followup_tasks", lead.id)
    seen, identities = set(), set()
    for item in items:
        identity = (item["kind"], item["owner"], tuple(sorted(set(item["evidence"]))))
        if identity in identities:
            raise ValueError("Ambiguous commitments share the same source and owner")
        identities.add(identity)
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
            raise ValueError("Commitment identity needs human review")
        record = candidates[0] if candidates else None
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
            "status": "open" if item["due_at"] and item["owner"] == "rep" else "needs_confirmation",
            "confirmation_state": "confirmed" if item["due_at"] and item["owner"] == "rep" else "suggested",
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
    service._sync_callback(s, p, lead)
    service.refresh_packet_in(s, p, lead)


def invalidate(service, s, p, call, segment_id=None, *, observation_ids=()):
    lead = service.required(s, "leads", call.data["lead_id"], p)
    affected = set()
    for record in service.lead_records(s, p, "commitments", lead.id):
        if record.data.get("call_id") == call.id and (
            segment_id is not None
            and segment_id in record.data.get("evidence", [])
            or record.data.get("observation_id") in observation_ids
        ):
            update(record, source_stale=True, status="needs_confirmation")
            affected.add(record.id)
    if not affected:
        return
    for task in service.lead_records(s, p, "followup_tasks", lead.id):
        if task.data.get("commitment_id") not in affected:
            continue
        changes = {"source_stale": True, "review_required": True, "confirmation_state": "needs_review"}
        if (
            not task.data.get("human_edited")
            and task.data.get("source") == "inferred"
            and task.data["status"] in ACTIVE
        ):
            changes["status"] = "needs_confirmation"
        update(task, **changes)
        emit(s, p["tenant_id"], "task.updated", task, service.clock())
    service._sync_callback(s, p, lead)
    service.refresh_packet_in(s, p, lead)
