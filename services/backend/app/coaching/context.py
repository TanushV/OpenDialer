"""Bounded conversational context; semantic interpretation belongs to the configured model."""

import hashlib
import re

from app.coaching.contracts import ConversationStateV2, FieldAudit, ObservationDelta, StateDelta
from app.coaching.engine import CardRejected, validate_card
from app.grounding import require_review, review_request

STATE_FIELDS = (
    "needs",
    "business_impact",
    "objections",
    "answered_questions",
    "buying_signals",
    "commitments",
    "rundown_points",
    "coaching_points",
    "open_questions",
)


def memory_id(key, item):
    return (
        item.get("id")
        or hashlib.sha256(
            (key + ":" + ":".join(sorted(item.get("evidence_segment_ids", [])))).encode()
        ).hexdigest()[:24]
    )


def words(text):
    return set(re.findall(r"[a-z]+", text.casefold())) - {
        "a",
        "an",
        "the",
        "is",
        "are",
        "to",
        "of",
        "and",
        "or",
        "we",
        "you",
        "your",
        "our",
        "would",
        "could",
        "do",
        "does",
        "this",
        "that",
        "for",
        "with",
        "in",
        "it",
    }


def evidence(segment):
    return {
        "text": segment["text"],
        "evidence_segment_ids": [segment["segment_id"]],
        "evidence_revisions": {segment["segment_id"]: segment.get("revision", 1)},
    }


def build_context(segments, *, version, facts, commitments, prior=None, at=None):
    """`at` is the authority clock time stamped on invalidations; prior state requires it."""
    stable = [s for s in segments if s.get("is_final") and not s.get("system_audio")]
    prior = prior or {}
    current = upgrade_state(prior)
    current["transcript_version"] = version
    if at is None and (
        prior.get("_lineage") or any(current[k] for k in ("observations", "rubric_items", "tracker_matches"))
    ):
        raise ValueError("Invalidating prior conversation state requires the service clock time")
    for segment in stable:
        current = invalidate_state(
            current,
            segment["segment_id"],
            segment.get("revision", 1),
            at=at,
            lineage=prior.get("_lineage", {}),
        )
    state = {
        **state_view(current, lineage=prior.get("_lineage", {})),
        "_lineage": prior.get("_lineage", {}),
        "trigger": "NO_ACTION",
        "facts": facts[:8],
        "recent_dialogue": stable[-16:],
        "last_remote": None,
        "asked_questions": [],
        "previous_guidance": prior.get("previous_guidance", [])[-5:],
    }
    # Authoritative tasks are supplied separately on each request. Copying them into
    # generated memory would keep a cancelled or changed promise indefinitely.
    for segment in stable:
        if segment.get("side") == "rep" and "?" in segment["text"]:
            state["asked_questions"].append(evidence(segment))
        elif segment.get("side") == "remote":
            state["last_remote"] = evidence(segment)
            state["trigger"] = "conversation"
    # The model interprets refusal scope and changed interest. Explicit opt-outs
    # are enforced independently by transcript admission and call authorization.
    state["asked_questions"] = state["asked_questions"][-12:]
    return state


def retrieve_cards(state, cards):
    if state["stage"] == "declined" or state["trigger"] == "NO_ACTION":
        return []
    asked = [q["text"].casefold() for q in state["asked_questions"]] + [
        s.casefold() for s in state.get("previous_guidance", [])
    ]
    query = words((state.get("last_remote") or {}).get("text", ""))
    relevant = []
    for card in cards:
        text = card.get("text", "")
        if not text or len(text) > 1000 or text.casefold() in asked:
            continue
        tokens = words(text + " " + " ".join(card.get("tags", [])))
        score = len(tokens & query) / max(1, len(tokens | query))
        if score:
            relevant.append((score, card))
    return [c for _, c in sorted(relevant, key=lambda item: (-item[0], item[1]["id"]))[:4]]


class MeaningReviewRequired(CardRejected):
    """Valid transport/source references, but no independent support for its assertions."""


def guidance_sources(request):
    """A bounded source catalog. Private questions provide intent, never factual authority."""
    book = request.get("context_sources", {}).get("knowledge", {}).get("book") or ["seller", 0]
    result = [
        {
            "kind": "knowledge",
            "id": book[0],
            "version": book[1],
            "text": request.get("seller", {}).get("knowledge", ""),
            "claims": request.get("approved_claims", []),
            "prohibited_claims": request.get("prohibited_claims", []),
        }
    ]
    for kind, values, identity, version in (
        ("segment", request.get("recent_dialogue", []), "segment_id", "revision"),
        ("fact", request.get("facts", []), "id", "version"),
        ("card", request.get("approved_cards", []), "id", "version"),
        ("commitment", request.get("prior_commitments", []), "id", "version"),
    ):
        for item in values:
            if not item.get(identity):
                continue
            result.append(
                {
                    "kind": kind,
                    "id": item[identity],
                    "version": item.get(version, 1),
                    "text": item.get("text", item.get("statement", item.get("action", ""))),
                    "side": item.get("side"),
                    "status": item.get("status"),
                    "raw_expires_at": item.get("raw_expires_at"),
                }
            )
    state = request.get("state", {})
    for item in state.get("observations", []):
        if item.get("state") not in {"stale", "retracted"} and item.get("review_status") not in {
            "rejected",
            "unavailable",
        }:
            if item.get("id"):
                result.append(
                    {
                        "kind": "observation",
                        "id": item["id"],
                        "version": item.get("version", state.get("version", 0)),
                        "text": item.get("text", ""),
                        "status": item.get("state"),
                        "epistemic_status": item.get("epistemic_status", "model_interpreted"),
                    }
                )
    for call in request.get("prior_call_context", [])[:3]:
        if call.get("outcome"):
            result.append(
                {
                    "kind": "outcome",
                    "id": call["call_id"],
                    "version": call.get("version", 1),
                    "text": call["outcome"],
                    "scope": "historical_call",
                }
            )
        if call.get("human_notes") and call.get("note_id"):
            result.append(
                {
                    "kind": "human_note",
                    "id": call["note_id"],
                    "version": call.get("note_version", 1),
                    "text": call["human_notes"],
                    "epistemic_status": "human_confirmed",
                }
            )
    if request.get("human_note") and request.get("human_note_id"):
        result.append(
            {
                "kind": "human_note",
                "id": request["human_note_id"],
                "version": request["human_note_version"],
                "text": request["human_note"],
                "epistemic_status": "human_confirmed",
            }
        )
    return result


def guidance_fields(card, *, progressive=False):
    fields = {"say_or_ask": card.get("say_or_ask", "")}
    if not progressive or card.get("reason"):
        fields["reason"] = card.get("reason", "")
    fields.update({f"alternatives.{i}": text for i, text in enumerate(card.get("alternatives", []))})
    return fields


def nonassertive_fields(card):
    """Fields the generator audited, exactly once, as nonassertive with no implied premise."""
    try:
        audits = [FieldAudit.model_validate(a) for a in card.get("field_audit") or []]
    except (TypeError, ValueError):
        return set()
    counts = {}
    for item in audits:
        counts[item.field] = counts.get(item.field, 0) + 1
    return {
        item.field
        for item in audits
        if counts[item.field] == 1 and item.classification == "nonassertive" and not item.implied_premises
    }


def guidance_review_request(card, request, *, progressive=False):
    # A reviewer's NON_FACTUAL verdict supports only a field the generator itself declared
    # nonassertive; it never approves a factual, premise-bearing or unaudited field.
    allowed = nonassertive_fields(card)
    return review_request(
        "guidance",
        [
            {"field": field, "value": value, "evidence": [], "allows_non_factual": field in allowed}
            for field, value in guidance_fields(card, progressive=progressive).items()
        ],
        [{**source, "id": source["kind"] + ":" + source["id"]} for source in guidance_sources(request)],
        {
            "state_version": request["state_version"],
            "context_fingerprint": request.get("context_fingerprint"),
            "private_question": request.get("private_question"),
            "state": request.get("state", {}),
            "prior_call_context": request.get("prior_call_context", []),
        },
    )


def ground_guidance(card, request, *, review=None, cached=False, progressive=False):
    sources = {
        s["segment_id"]: s
        for s in request.get("recent_dialogue", [])
        if s.get("is_final") and not s.get("system_audio")
    }
    cards = {c["id"]: c for c in request.get("approved_cards", [])}
    validate_card(
        card,
        state_version=request["state_version"],
        segment_ids=set(sources),
        card_ids=set(cards),
        manual=bool(request.get("manual_help") or request.get("private_question")),
    )
    if card["action"] == "NO_ACTION":
        return True
    if any(not isinstance(text, str) or len(text) > 1000 for text in card.get("alternatives", [])):
        raise CardRejected("Excessive alternative guidance")
    if card.get("context_fingerprint") is not None and card["context_fingerprint"] != request.get(
        "context_fingerprint"
    ):
        raise CardRejected("Guidance belongs to an obsolete context")
    knowledge = " ".join(str(f.get("statement", f.get("text", ""))) for f in request.get("facts", []))
    knowledge += " " + " ".join(str(c) for c in request.get("approved_claims", []))
    knowledge += " " + request.get("seller", {}).get("knowledge", "")
    knowledge += " " + " ".join(c.get("text", "") for c in cards.values())
    transcript = " ".join(sources[i]["text"] for i in card.get("evidence_segment_ids", []))
    visible = " ".join(guidance_fields(card, progressive=progressive).values())

    def numbers(text):
        return set(re.findall(r"\d+(?:\.\d+)?", text))

    if not numbers(visible) <= numbers(knowledge + " " + transcript):
        raise CardRejected("Unsupported numerical assertion")
    if cached and any(card.get("say_or_ask") == c.get("text") for c in cards.values()):
        return True  # Only the application-created fallback can supply this flag.
    if review is not None:
        try:
            require_review(guidance_review_request(card, request, progressive=progressive), review)
            return True
        except ValueError as exc:
            raise CardRejected("Meaning review is not current for these complete fields") from exc
    if not isinstance(card.get("field_audit", []), list) or len(card.get("field_audit", [])) > 40:
        raise CardRejected("Unbounded generator audit")
    try:
        audits = [FieldAudit.model_validate(a) for a in card.get("field_audit", [])]
    except ValueError as exc:
        raise MeaningReviewRequired("Incomplete generator audit") from exc
    catalog = {(s["kind"], s["id"], s["version"]) for s in guidance_sources(request)}
    if any((s.kind, s.id, s.version) not in catalog for audit in audits for s in audit.sources):
        raise CardRejected("Generator audit cites an unknown or obsolete source")
    expected = set(guidance_fields(card, progressive=progressive))
    selected = [a for a in audits if a.field in expected]
    # Audits route work; they do not establish semantic truth. The independent
    # semantic evaluation retains generator misclassification as a separate risk.
    if (
        not card.get("context_fingerprint")
        or len(selected) != len(expected)
        or {a.field for a in selected} != expected
        or any(a.classification != "nonassertive" or a.sources or a.implied_premises for a in selected)
        or not progressive
        and {a.field for a in audits} != expected
        or material_update(card.get("state_update"))
    ):
        raise MeaningReviewRequired("Complete independent review required")
    return True


def material_update(update):
    """A proposed commitment or rating keeps guidance off the single-inference path."""
    if not isinstance(update, dict):
        return False

    def items(key):
        value = update.get(key)
        return [i for i in value if isinstance(i, dict)] if isinstance(value, list) else []

    return bool(
        update.get("commitments")  # Legacy untyped interpretation field.
        or any(i.get("kind") == "commitment" or i.get("commitment") for i in items("observations"))
        or any(i.get("status") == "scored" or i.get("score") is not None for i in items("rubric_items"))
    )


def reviewed_guidance(card, request, receipt):
    """Publishable guidance after one complete independent review of every visible field.

    After any rejection no factual field is shown. A field the generator audited nonassertive,
    and the reviewer accepted, may still be shown as the primary help.
    """
    ground_guidance(card, request, review=receipt)
    if not receipt.rejected:
        return {**card, "meaning_review": receipt.metadata()}
    safe = nonassertive_fields(card) - set(receipt.rejected)
    fields = guidance_fields(card)
    clarifications = [field for field in fields if field != "reason" and field in safe]
    if not clarifications:
        raise CardRejected("Independent review withheld every displayable field")
    primary, *others = clarifications
    shown = {primary: "say_or_ask"}
    if primary == "say_or_ask" and "reason" in safe:
        shown["reason"] = "reason"  # A reason explains only its own primary suggestion.
    shown.update({field: f"alternatives.{i}" for i, field in enumerate(others)})
    audits = {audit["field"]: audit for audit in card["field_audit"]}
    return {
        **card,
        "say_or_ask": fields[primary],
        "reason": fields["reason"] if "reason" in shown else "",
        "alternatives": [fields[field] for field in others],
        "field_audit": [{**audits[field], "field": name} for field, name in shown.items()],
        "meaning_review": {**receipt.metadata(), "displayed_fields": list(shown)},
    }


# The canonical record owns interpretation; former arrays are read-only serializers.
KINDS = dict(
    zip(
        STATE_FIELDS,
        ("need", "impact", "objection", "answer", "signal", "commitment", "rundown", "coaching", "question"),
        strict=True,
    )
)


def upgrade_state(state, *, call_id=None, rubric=None):
    from copy import deepcopy

    if state.get("schema_version") == 2:
        value = deepcopy({k: state[k] for k in ConversationStateV2.model_fields if k in state})
    else:
        observations = []
        for field, kind in KINDS.items():
            for item in state.get(field, []):
                refs = [
                    dict(
                        kind="segment",
                        id=i,
                        version=v,
                        occurred_at=None,
                        available_at=None,
                        content_hash=None,
                        raw_expires_at=None,
                    )
                    for i, v in item.get("evidence_revisions", {}).items()
                ][:32]
                observations.append(
                    dict(
                        id=memory_id(field, item),
                        version=1,
                        kind=kind,
                        text=item["text"][:400],
                        state=item.get("status", "open"),
                        sources=refs,
                        replaces=[],
                        epistemic_status="model_interpreted",
                        observed_at=None,
                        available_at=None,
                        invalidated_at=None,
                        review_receipt_id=None,
                        review_status="pending" if kind == "commitment" else "not_required",
                        lineage_depth=0,
                        commitment=None,
                    )
                )
        value = dict(
            schema_version=2,
            call_id=call_id or state.get("call_id", ""),
            version=state.get("version", 0),
            transcript_version=state.get("version", 0),
            context_fingerprint="",
            stage=state.get("stage", "introduction"),
            trigger=state.get("trigger", "NO_ACTION"),
            purpose={},
            observations=observations,
            rubric_items=[],
            tracker_matches=[],
            coverage={"omitted_items": state.get("memory_omitted_items", 0)},
            finalization={"status": "pending", "source_version": None, "last_request_id": None},
            source_conflicts=[],
        )
    if call_id:
        value["call_id"] = call_id
    if rubric and value["purpose"] != rubric:
        value["purpose"] = deepcopy(rubric)
        value["rubric_items"] = [
            dict(
                dimension=d["id"],
                status="unassessed",
                score=None,
                explanation="No reviewed opportunity yet.",
                sources=[],
                epistemic_status="model_interpreted",
                review_status="not_required",
                review_receipt_id=None,
                version=1,
            )
            for d in rubric["dimensions"]
        ]
        value["finalization"]["status"] = "pending"
    return value


def lineage_key(identity, version):
    return f"{identity}:{version}"


def lineage_metadata(item):
    return {
        k: item.get(k)
        for k in (
            "id",
            "version",
            "sources",
            "replaces",
            "epistemic_status",
            "review_status",
            "review_receipt_id",
            "invalidated_at",
            "lineage_depth",
            "kind",
            "observed_guidance_id",
            "strategy_id",
            "state",
            "available_at",
        )
    }


def segment_revisions(item, lineage=None):
    history, result, seen = lineage or {}, {}, set()
    pending = list(item.get("sources", []))
    while pending:
        ref = pending.pop()
        if ref["kind"] == "segment":
            result[ref["id"]] = max(result.get(ref["id"], 0), ref["version"])
        elif ref["kind"] == "observation":
            key = lineage_key(ref["id"], ref["version"])
            if key not in seen:
                seen.add(key)
                pending.extend(history.get(key, {}).get("sources", []))
    return result


def state_view(state, *, lineage=None):
    value = upgrade_state(state)
    history = lineage or state.get("_lineage", {})
    for key, kind in KINDS.items():
        value[key] = []
        for item in value["observations"]:
            if (
                item["kind"] != kind
                or item["state"] in {"stale", "retracted"}
                or item["review_status"] in {"rejected", "unavailable"}
            ):
                continue
            revisions = segment_revisions(item, history)
            value[key].append(
                {
                    **item,
                    "status": item["state"],
                    "evidence_segment_ids": list(revisions),
                    "evidence_revisions": revisions,
                }
            )
    value["memory_omitted_items"] = value["coverage"].get("omitted_items", 0)
    return value


def persistent_state(state, *, limits=None):
    import json

    from app.config import RuntimeLimits

    limits = limits or RuntimeLimits()
    value = upgrade_state(state)

    def priority(item):
        return (
            item["state"] in {"stale", "retracted"},
            item["epistemic_status"] != "human_confirmed",
            item["kind"] not in {"commitment", "question"},
            item["state"] != "open",
        )

    ranked = sorted(reversed(value["observations"]), key=priority)
    selected, counts, omitted = [], {}, 0
    for item in ranked:
        if (
            len(selected) >= limits.observation_count
            or counts.get(item["kind"], 0) >= limits.observation_per_kind
        ):
            omitted += 1
        else:
            counts[item["kind"]] = counts.get(item["kind"], 0) + 1
            selected.append(item)
    chosen = {i["id"] for i in selected}
    value["observations"] = [i for i in value["observations"] if i["id"] in chosen]
    value["coverage"]["omitted_items"] = value["coverage"].get("omitted_items", 0) + omitted
    while len(json.dumps(value, separators=(",", ":")).encode()) > limits.conversation_bytes and selected:
        removed = selected.pop()
        value["observations"] = [i for i in value["observations"] if i["id"] != removed["id"]]
        value["coverage"]["omitted_items"] += 1
    if len(json.dumps(value, separators=(",", ":")).encode()) > limits.conversation_bytes:
        raise CardRejected("Conversation metadata exceeds its byte budget")
    return ConversationStateV2.model_validate(value).model_dump()


def interpretation_delta(update, segments):
    """Only the new schema is generated; migrate old in-flight fixture/client responses once."""
    if "observations" in update:
        return StateDelta.model_validate(update).model_dump()
    if set(update) - {"stage", *STATE_FIELDS}:
        raise CardRejected("Unknown conversation fields")
    source = {s["segment_id"]: s for s in segments if s.get("is_final") and not s.get("system_audio")}
    observations = []
    for field, kind in KINDS.items():
        items = update.get(field, [])
        if not isinstance(items, list) or len(items) > 12:
            raise CardRejected("Unbounded conversation state")
        for item in items:
            if not isinstance(item, dict) or set(item) - {
                "text",
                "quote",
                "evidence_segment_ids",
                "status",
                "replaces",
            }:
                raise CardRejected("Invalid observation or replacement reference")
            ids = item.get("evidence_segment_ids", [])
            if (
                not isinstance(ids, list)
                or not 0 < len(ids) <= 32
                or not all(isinstance(i, str) and i in source for i in ids)
            ):
                raise CardRejected("Missing semantic evidence")
            observations.append(
                ObservationDelta.model_validate(
                    dict(
                        kind=kind,
                        text=item.get("text"),
                        quote=item.get("quote"),
                        state=item.get("status", "open"),
                        sources=[
                            dict(kind="segment", id=i, version=source[i].get("revision", 1)) for i in ids
                        ],
                        replaces=item.get("replaces", []),
                        commitment=None,
                    )
                ).model_dump()
            )
    return StateDelta.model_validate(
        dict(
            stage=update.get("stage"),
            observations=observations,
            rubric_items=[],
            tracker_matches=[],
            interpreted_segments=[
                dict(kind="segment", id=i, version=s.get("revision", 1)) for i, s in source.items()
            ],
        )
    ).model_dump()


def merge_semantic_state(
    state,
    update,
    segments,
    *,
    request_id=None,
    at=None,
    hash_sources=False,
    limits=None,
    catalog=None,
    call_at=None,
    timezone=None,
    exposures=(),
):
    import hashlib
    from copy import deepcopy
    from uuid import NAMESPACE_URL, uuid5

    from app.privacy import paraphrased

    if update is None:
        return state
    try:
        delta = interpretation_delta(update, segments)
    except (ValueError, TypeError) as exc:
        raise CardRejected("Invalid conversation interpretation") from exc
    value = upgrade_state(state)
    history = deepcopy(state.get("_lineage", {}))
    prior = {i["id"]: i for i in value["observations"]}
    for item in prior.values():
        history[lineage_key(item["id"], item["version"])] = lineage_metadata(item)
    now = at.isoformat() if at is not None else None
    live = {s["segment_id"]: s for s in segments if s.get("is_final") and not s.get("system_audio")}
    allowed = {(s["kind"], s["id"], s["version"]): s for s in catalog or []}
    for identity, item in prior.items():
        allowed[("observation", identity, item["version"])] = item
    for identity, item in live.items():
        allowed[("segment", identity, item.get("revision", 1))] = item
    request_id = (
        request_id
        or hashlib.sha256(repr([(i, s.get("revision", 1)) for i, s in live.items()]).encode()).hexdigest()
    )
    interpreted = []
    for ref in delta["interpreted_segments"]:
        if ref["kind"] != "segment" or (ref["kind"], ref["id"], ref["version"]) not in allowed:
            raise CardRejected("Interpretation named an unavailable segment revision")
        if ref not in interpreted:
            interpreted.append(ref)

    def references(refs):
        result = []
        for ref in refs:
            source = allowed.get((ref["kind"], ref["id"], ref["version"]))
            if (
                source is None
                or source.get("state") in {"stale", "retracted"}
                or source.get("invalidated_at")
            ):
                raise CardRejected("Unknown or stale conversation source reference")
            raw = str(source.get("text", source.get("statement", "")))
            result.append(
                {
                    **ref,
                    "occurred_at": source.get("occurred_at") or source.get("server_received_at"),
                    "available_at": source.get("available_at") or source.get("server_received_at"),
                    "raw_expires_at": source.get("raw_expires_at"),
                    "content_hash": hashlib.sha256(raw.encode()).hexdigest()
                    if hash_sources
                    else source.get("content_hash"),
                }
            )
        if len({(r["kind"], r["id"], r["version"]) for r in result}) != len(result):
            raise CardRejected("Duplicate source reference")
        return result

    changed, lineage_updates = False, []
    for index, item in enumerate(delta["observations"]):
        identity = str(uuid5(NAMESPACE_URL, value["call_id"] + ":" + request_id + ":" + str(index)))
        if identity in prior:
            continue
        replacement = item["replaces"]
        if len(set(replacement)) != len(replacement) or any(
            i not in prior or i == identity for i in replacement
        ):
            raise CardRejected("Unknown memory replacement reference")
        refs = references(item["sources"])
        for old in replacement:
            ref = {"kind": "observation", "id": old, "version": prior[old]["version"]}
            if not any(all(r[k] == ref[k] for k in ref) for r in refs):
                refs.extend(references([ref]))
        if len(refs) > 32:
            raise CardRejected("Observation source count exceeds its bound")
        source_text = " ".join(live[r["id"]]["text"] for r in refs if r["kind"] == "segment")
        if item["quote"] and item["quote"].casefold() not in source_text.casefold():
            raise CardRejected("State evidence is absent")
        if not paraphrased(item["text"], segments):
            continue
        if item.get("approach") and (
            not paraphrased(item["approach"], segments)
            or not any(r["kind"] == "segment" and live[r["id"]].get("side") == "rep" for r in refs)
        ):
            raise CardRejected("An observed approach requires actual rep speech, not suggested advice")
        exposure = None
        if item.get("observed_guidance_id"):
            exposure = next((e for e in exposures if e["id"] == item["observed_guidance_id"]), None)
            if (
                not exposure
                or not exposure.get("displayed_at")
                or not exposure.get("approach")
                or item["kind"] != "approach"
                or not item.get("approach")
                or not any(
                    r["kind"] == "segment"
                    and live[r["id"]].get("side") == "rep"
                    and live[r["id"]].get("server_received_at")
                    and live[r["id"]]["server_received_at"] >= exposure["displayed_at"]
                    for r in refs
                )
            ):
                raise CardRejected(
                    "Observed strategy needs displayed guidance and subsequent received rep evidence"
                )
        depths = [prior[r["id"]]["lineage_depth"] for r in refs if r["kind"] == "observation"]
        depth = 1 + max(depths, default=-1)
        if depth > 8:
            if any(
                prior[r["id"]]["epistemic_status"] != "model_reviewed"
                for r in refs
                if r["kind"] == "observation"
            ):
                raise CardRejected("A reviewed checkpoint is required before extending source lineage")
            depth = 1
        material = (
            item["kind"] in {"commitment", "approach"}
            or item.get("outcome") is not None
            or item["state"] in {"resolved", "retracted"}
            or depth == 8
            or item["review_required"]
        )
        status = item["state"]
        if status == "confirmed" and any(
            live[r["id"]].get("side") == "unknown" for r in refs if r["kind"] == "segment"
        ):
            status = "open"
        if now is None:
            raise ValueError("Conversation interpretation requires the service clock time")
        commitment = None
        if item.get("outcome") and item["kind"] != "signal":
            raise CardRejected("An outcome suggestion belongs to a source-linked signal")
        if item["commitment"]:
            from app.postcall.dates import resolve_time

            if item["kind"] != "commitment" or not source_text:
                raise CardRejected("A commitment requires live evidence")
            promise = item["commitment"]
            if promise["verbatim_time_expression"].casefold() not in source_text.casefold():
                raise CardRejected("Commitment time expression is absent")
            normalized = resolve_time(promise, call_at=call_at, timezone=timezone)
            if status != "confirmed":
                normalized.update(due_at=None, needs_time_confirmation=True)
            commitment = {k: v for k, v in normalized.items() if k != "verbatim_time_expression"}
        obs = dict(
            id=identity,
            version=1,
            kind=item["kind"],
            text=item["text"],
            state=status,
            sources=refs,
            replaces=replacement,
            epistemic_status="model_interpreted",
            observed_at=now,
            available_at=now,
            invalidated_at=None,
            review_receipt_id=None,
            review_status="pending" if material else "not_required",
            lineage_depth=depth,
            commitment=commitment,
            approach=item.get("approach"),
            outcome=item.get("outcome"),
            observed_guidance_id=item.get("observed_guidance_id"),
            strategy_id=exposure["strategy_id"] if exposure else None,
        )
        if not material:
            for old in replacement:
                prior.pop(old)
        prior[identity] = obs
        meta = lineage_metadata(obs)
        history[lineage_key(identity, 1)] = meta
        lineage_updates.append(meta)
        changed = True
    rubric = {i["dimension"]: i for i in value["rubric_items"]}
    seen = set()
    for item in delta["rubric_items"]:
        if item["dimension"] not in rubric or item["dimension"] in seen:
            raise CardRejected("Unknown or duplicate rubric dimension")
        seen.add(item["dimension"])
        if (
            (item["status"] == "scored") != (item["score"] is not None)
            or item["status"] != "unassessed"
            and not item["sources"]
        ):
            raise CardRejected("A rating requires evidence and a consistent score")
        if not paraphrased(item["explanation"], segments):
            raise CardRejected("Rubric explanation must paraphrase")
        rubric[item["dimension"]] = {
            **item,
            "sources": references(item["sources"]),
            "epistemic_status": "model_interpreted",
            "review_status": "pending",
            "review_receipt_id": None,
            "version": rubric[item["dimension"]].get("version", 0) + 1,
        }
        changed = True
    trackers = {i["tracker_id"]: i for i in value["tracker_matches"]}
    seen = set()
    for item in delta["tracker_matches"]:
        if item["tracker_id"] in seen:
            raise CardRejected("Duplicate tracker match")
        seen.add(item["tracker_id"])
        trackers[item["tracker_id"]] = {
            **item,
            "sources": references(item["sources"]),
            "epistemic_status": "model_interpreted",
            "review_status": "pending",
            "review_receipt_id": None,
        }
        changed = True
    value.update(
        stage=delta["stage"],
        observations=list(prior.values()),
        rubric_items=list(rubric.values()),
        tracker_matches=list(trackers.values())[:20],
    )
    if changed:
        value["version"] += 1
        value["finalization"]["status"] = "pending"
    value = persistent_state(value, limits=limits)
    return {
        **state_view(value, lineage=history),
        "_lineage": history,
        "_lineage_updates": lineage_updates,
        "_interpreted_segments": interpreted,
    }


def invalidate_state(state, source_id, revision, *, at, lineage=None, source_kind="segment"):
    value = upgrade_state(state)
    history = dict(lineage or state.get("_lineage", {}))
    for item in value["observations"]:
        history[lineage_key(item["id"], item["version"])] = lineage_metadata(item)
    invalid = {
        k
        for k, item in history.items()
        if any(
            r["kind"] == source_kind and r["id"] == source_id and r["version"] != revision
            for r in item.get("sources", [])
        )
    }
    while True:
        found = {
            k
            for k, item in history.items()
            if any(
                r["kind"] == "observation" and lineage_key(r["id"], r["version"]) in invalid
                for r in item.get("sources", [])
            )
        }
        if found <= invalid:
            break
        invalid |= found

    def affected(item):
        return lineage_key(item.get("id", ""), item.get("version", 0)) in invalid or any(
            r["kind"] == source_kind
            and r["id"] == source_id
            and r["version"] != revision
            or r["kind"] == "observation"
            and lineage_key(r["id"], r["version"]) in invalid
            for r in item.get("sources", [])
        )

    conflicts = []
    for item in [*value["observations"], *value["rubric_items"], *value["tracker_matches"]]:
        if affected(item) and (
            item.get("review_status") != "unavailable" or "state" in item and item["state"] != "stale"
        ):
            if "state" in item:
                item["state"] = "stale"
                item["invalidated_at"] = at.isoformat()
            item["review_status"] = "unavailable"
            if "score" in item:
                item.update(score=None, status="unassessed")
            conflicts.append(
                {
                    "id": item.get("id") or item.get("dimension") or item.get("tracker_id"),
                    "source_id": source_id,
                    "source_kind": source_kind,
                    "reason": "source_corrected",
                    "at": at.isoformat(),
                }
            )
    if conflicts:
        value["source_conflicts"] = (value["source_conflicts"] + conflicts)[-96:]
        value["version"] += 1
        value["finalization"]["status"] = "pending"
    value["_lineage_updates"] = [
        dict(history[key], invalidated_at=at.isoformat(), review_status="unavailable")
        for key in invalid
        if not history[key].get("invalidated_at")
    ]
    return value
