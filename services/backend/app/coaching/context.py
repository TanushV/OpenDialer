"""Bounded conversational context; semantic interpretation belongs to the configured model."""

import hashlib
import re

from app.coaching.contracts import FieldAudit
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


def bounded_memory(items):
    # Preserve unresolved work before recent resolved details; report omitted material explicitly.
    selected = sorted(enumerate(items), key=lambda pair: (pair[1].get("status") != "open", -pair[0]))[:12]
    return [item for _, item in sorted(selected)]


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


def build_context(segments, *, version, facts, commitments, prior=None):
    stable = [s for s in segments if s.get("is_final") and not s.get("system_audio")]
    prior = prior or {}
    sources = {s["segment_id"]: s for s in stable}
    state = {
        "version": version,
        "stage": prior.get("stage", "introduction"),
        "trigger": "NO_ACTION",
        "facts": facts[:8],
        "recent_dialogue": stable[-16:],
        "last_remote": None,
        "asked_questions": [],
        "previous_guidance": prior.get("previous_guidance", [])[-5:],
        "memory_omitted_items": prior.get("memory_omitted_items", 0),
    }
    for key in STATE_FIELDS:
        # Expiry does not invalidate a paraphrase; a source correction does.
        state[key] = [
            {**i, "id": memory_id(key, i)}
            for i in prior.get(key, [])
            if not any(
                identifier in sources and revision != sources[identifier].get("revision", 1)
                for identifier, revision in i.get("evidence_revisions", {}).items()
            )
        ]
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
        if score or "discovery" in card.get("tags", []):
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
    return result


def guidance_fields(card, *, progressive=False):
    fields = {"say_or_ask": card.get("say_or_ask", "")}
    if not progressive or card.get("reason"):
        fields["reason"] = card.get("reason", "")
    fields.update({f"alternatives.{i}": text for i, text in enumerate(card.get("alternatives", []))})
    return fields


def guidance_review_request(card, request, *, progressive=False):
    return review_request(
        "guidance",
        [
            {"field": field, "value": value, "evidence": [], "allows_non_factual": True}
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
        manual=bool(request.get("private_question")),
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
        or isinstance(card.get("state_update"), dict)
        and any(card["state_update"].get(key) for key in ("commitments", "score_items"))
    ):
        raise MeaningReviewRequired("Complete independent review required")
    return True


def merge_semantic_state(state, update, segments):
    if update is None:
        return state
    from app.privacy import paraphrased

    sources = {s["segment_id"]: s for s in segments if s.get("is_final") and not s.get("system_audio")}
    if not isinstance(update, dict) or update.get("stage") not in (
        "introduction",
        "discovery",
        "objection",
        "next_step",
        "declined",
    ):
        raise CardRejected("Unknown conversation stage")
    merged = dict(state)
    for key in STATE_FIELDS:
        values = update.get(key, [])
        if not isinstance(values, list) or len(values) > 12:
            raise CardRejected("Unbounded conversation state")
        previous = {memory_id(key, i): {**i, "id": memory_id(key, i)} for i in state.get(key, [])}
        for item in values:
            if not isinstance(item, dict):
                raise CardRejected("Invalid observation")
            ids, quote, text = (
                item.get("evidence_segment_ids", []),
                item.get("quote", ""),
                item.get("text", ""),
            )
            if (
                not isinstance(ids, list)
                or not 0 < len(ids) <= 128
                or not all(isinstance(i, str) and i in sources for i in ids)
                or not isinstance(quote, str)
                or not 0 < len(quote) <= 400
                or not isinstance(text, str)
                or not 0 < len(text) <= 400
            ):
                raise CardRejected("Missing semantic evidence")
            replaces = item.get("replaces", [])
            if (
                not isinstance(replaces, list)
                or len(replaces) > 12
                or not all(isinstance(i, str) and i in previous for i in replaces)
                or len(set(replaces)) != len(replaces)
            ):
                raise CardRejected("Unknown memory replacement")
            if item.get("status", "open") not in ("open", "resolved", "confirmed"):
                raise CardRejected("Unknown observation state")
            if quote.casefold() not in " ".join(sources[i]["text"] for i in ids).casefold():
                raise CardRejected("State evidence is absent")
            if not paraphrased(text, segments):
                continue  # Never persist a transcript quotation as a supposed summary.
            revisions = {
                source_id: revision
                for identity in replaces
                for source_id, revision in previous[identity].get("evidence_revisions", {}).items()
            }
            revisions.update({i: sources[i].get("revision", 1) for i in ids})
            if len(revisions) > 128:
                raise CardRejected("Memory source lineage exceeds its bound")
            for identity in replaces:
                del previous[identity]
            identity = memory_id(key, {"evidence_segment_ids": list(revisions)})
            previous[identity] = {
                "id": identity,
                "text": text[:400],
                "status": item.get("status", "open"),
                "evidence_segment_ids": list(revisions),
                "evidence_revisions": revisions,
            }
        merged[key] = list(previous.values())
    merged["stage"] = update["stage"]
    return merged


def persistent_state(state):
    """Only derived observations survive. Raw context belongs to the TTL buffer."""
    return {
        "version": state["version"],
        "stage": state["stage"],
        "trigger": state["trigger"],
        **{key: bounded_memory(state.get(key, [])) for key in STATE_FIELDS},
        "memory_omitted_items": state.get("memory_omitted_items", 0)
        + sum(max(0, len(state.get(key, [])) - 12) for key in STATE_FIELDS),
    }
