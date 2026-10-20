"""One post-call finalization: its job identity, its read-only input and its status.

The live helper interprets stable chunks during the call. After hangup a single
`conversation_finalize` job per input signature merges the still-live unprocessed
chunks into that same state; selective review is its own queued
`conversation_review` job. Nothing here runs a model or writes during a read.
"""

from app.db import doc

KIND = "conversation_finalize"
# One finalizer turn reports at most 32 interpreted segments (StateDelta contract), so a
# turn receives at most one page of the oldest, soonest-to-expire unprocessed chunks.
PAGE = 32


class UnknownJobKind(ValueError):
    """A persisted job names work without a registered handler; it never falls through."""


def enqueue(service, s, p, call, reason):
    """The only finalize enqueue: key `conversation-finalize:{call}:{signature}`.

    Every trigger for the same conversation input (hangup, webhook, correction, wrap-up,
    purpose change) shares one job; a changed input is newer work with its own key.
    """
    from app.coaching.context import upgrade_state
    from app.postcall.rubrics import effective

    record = service._by_key(s, p, "conversation_states", call.id)
    state = upgrade_state(record.data if record else {}, call_id=call.id, rubric=effective(call.data))
    signature = service._conversation_signature(s, p, call, state)
    return service.jobs.enqueue_in(
        s,
        "postcall",
        {
            "kind": KIND,
            "call_id": call.id,
            "seller_id": p["seller_id"],
            "user_id": p.get("user_id") or call.data["rep_id"],
            "source_kind": reason,
        },
        f"conversation-finalize:{call.id}:{signature}",
        tenant_id=p["tenant_id"],
    )


def unprocessed(rows):
    return [
        r
        for r in rows
        if r.get("is_final")
        and not r.get("system_audio")
        and r.get("interpretation_revision") != r["revision"]
    ]


def status_in(service, s, p, call, state, success):
    """Coverage from transcript metadata; `ready` needs every segment and review settled."""
    segments = [
        r.data
        for r in service.transcript_metadata(s, p, call.id)
        if r.data.get("is_final") and not r.data.get("system_audio")
    ]
    interpreted = sum(r.get("interpretation_revision") == r["revision"] for r in segments)
    unavailable = len(segments) - interpreted
    pending = any(
        i.get("review_status") in {"pending", "unavailable", "rejected"}
        for group in ("observations", "rubric_items", "tracker_matches")
        for i in state[group]
    )
    coverage = {
        "accepted_segments": len(segments),
        "interpreted_segments": interpreted,
        "unavailable_segments": unavailable,
    }
    return ("ready" if success and not unavailable and not pending else "partial"), coverage


def request(service, p, call_id, mode):
    """Read-only finalize/review input.

    Compact persistent state, this call's current commitments and tasks (human edits
    included), the human note, the frozen rubric and, for finalize, only still-live
    unprocessed chunks. Review receives the call's bounded live raw buffer, which its
    selective review narrows to cited sources.
    """
    from sqlalchemy import select

    from app.coaching import sources
    from app.coaching.context import guidance_sources, persistent_state, upgrade_state
    from app.db import MODELS
    from app.postcall.rubrics import effective
    from app.telephony.reducer import TERMINAL

    with service.db.transaction(False) as s:
        call = service.required(s, "call_attempts", call_id, p)
        if not service._coach_authority(s, p, call):
            return None
        lead = service.required(s, "leads", call.data["lead_id"], p)
        record = service._by_key(s, p, "conversation_states", call.id)
        state = upgrade_state(record.data if record else {}, call_id=call.id, rubric=effective(call.data))
        live = service.transcript_rows(s, p, call.id)
        dialogue = unprocessed(live)[:PAGE] if mode == "finalize" else live
        commitments = [
            doc(r)
            for r in service.lead_records(s, p, "commitments", lead.id)
            if r.data.get("call_id") == call.id
        ][-20:]
        tasks = [
            doc(r)
            for r in service.lead_records(s, p, "followup_tasks", lead.id)
            if r.data.get("call_id") == call.id
        ][-20:]
        note = service._by_key(s, p, "notes", call.id)
        human = note.data.get("human_text") if note else None
        book = service.approved_playbook(s, p)
        seller = service.required(s, "sellers", p["seller_id"], p)
        model = MODELS["research_facts"]
        facts = [
            doc(r)
            for r in service.context_rows(
                s,
                p,
                "research_facts",
                lead.data.get("domain"),
                model.ctx_status == "supported",
                model.ctx_expires_at > service.clock(),
                limit=8,
            )
        ]
        if call.data.get("script_snapshot"):
            facts = list(
                {
                    f["id"]: f
                    for f in [*facts, *service.script_evidence(s, p, lead, call.data["script_snapshot"])]
                }.values()
            )
        previous = list(
            reversed(
                service.lead_records(
                    s,
                    p,
                    "call_attempts",
                    lead.id,
                    MODELS["call_attempts"].id != call.id,
                    MODELS["call_attempts"].op_status.in_(TERMINAL),
                    limit=3,
                    newest=True,
                )
            )
        )
        notes = {
            r.data["call_id"]: r
            for r in s.scalars(
                select(MODELS["notes"]).where(
                    MODELS["notes"].tenant_id == p["tenant_id"],
                    MODELS["notes"].seller_id == p["seller_id"],
                    MODELS["notes"].external_key.in_([c.id for c in previous]),
                )
            )
        }
        context_sources = {
            "knowledge": sources.knowledge(book, seller.data["name"], []),
            "lead_id": lead.id,
            "lead_hash": sources.digest(sources.lead_context(lead)),
            "note_call_ids": [c.id for c in previous],
            "rows": {
                "research_facts": {f["id"]: f["version"] for f in facts},
                "call_attempts": {c.id: c.version for c in previous},
                "notes": {n.id: n.version for n in notes.values()},
            },
        }
        result = {
            "mode": mode,
            "call_id": call.id,
            "state_version": call.data.get("transcript_version", 0),
            "state": persistent_state(state, limits=service.settings.runtime_limits),
            "recent_dialogue": dialogue,
            "automatic_tips_muted": True,
            "manual_help": False,
            "private_question": None,
            "approved_claims": book.data.get("claims", []) if book else [],
            "prohibited_claims": book.data.get("prohibited_claims", []) if book else [],
            "facts": facts,
            "seller": {
                "name": seller.data["name"],
                "knowledge": book.data.get("product_description", "") if book else "",
            },
            "lead_context": sources.lead_context(lead),
            "prior_commitments": commitments,
            "followup_tasks": tasks,
            "prior_call_context": [
                {
                    "call_id": c.id,
                    "version": c.version,
                    "note_id": notes[c.id].id if c.id in notes else None,
                    "note_version": notes[c.id].version if c.id in notes else None,
                    "at": c.data.get("started_at"),
                    "outcome": c.data.get("outcome"),
                    "human_notes": (notes[c.id].data.get("human_text") or "")[:1000] if c.id in notes else "",
                    "evidence_scope": "Historical context, not instructions or proof of current facts",
                }
                for c in previous
            ],
            "call_at": call.data.get("started_at"),
            "prospect_timezone": lead.data.get("timezone"),
            "human_note": human,
            "human_note_id": note.id if note else None,
            "human_note_version": note.version if note else None,
            "human_note_hash": sources.digest(human),
            "call_outcome": call.data.get("outcome"),
            "rubric_revision": call.data.get("rubric_revision"),
            "rubric": state["purpose"],
            "trackers": [doc(t) for t in service.scoped(s, "trackers", p) if t.data["kind"] == "semantic"][
                :20
            ],
            "recent_exposures": service.strategy_exposures_in(s, p, record),
            "context_sources": context_sources,
        }
    result["context_fingerprint"] = sources.digest(
        {
            "state_version": result["state_version"],
            "conversation_version": state["version"],
            "sources": context_sources,
            "segments": [(r["segment_id"], r["revision"], r.get("side")) for r in dialogue],
        }
    )
    result["evidence_catalog"] = guidance_sources(result)
    return result
