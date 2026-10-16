"""Fast local state/retrieval plus evidence-checked semantic updates from the configured coach."""

import re

from app.coaching.engine import CardRejected, validate_card

TOPICS = {
    "budget": r"budget|afford|expens|cost|price|investment|funding|subscription",
    "provider": r"provider|supplier|vendor|incumbent|already (?:use|have)|existing (?:tool|system)|contract|agreement",
    "timing": r"busy|later|next (?:week|month)|call.*back|follow.up|not.*time|schedule",
    "need": r"problem|struggl|difficult|need help|challenge|pain|manual|wast.*time",
    "next_step": r"arrange.*meeting|book.*meeting|schedule.*demo|send.*proposal|next step|let.s (?:meet|talk)|interested.*learn",
}


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


def evidence(segment, **extra):
    return {
        "text": segment["text"],
        "quote": segment["text"],
        "evidence_segment_ids": [segment["segment_id"]],
        "evidence_revisions": {segment["segment_id"]: segment.get("revision", 1)},
        **extra,
    }


def build_context(segments, *, version, facts, commitments, prior=None):
    stable = [s for s in segments if s.get("is_final") and not s.get("system_audio")]
    state = {
        "version": version,
        "stage": "introduction",
        "trigger": "NO_ACTION",
        "needs": [],
        "business_impact": [],
        "objections": [],
        "asked_questions": [],
        "answered_questions": [],
        "buying_signals": [],
        "commitments": commitments[-5:],
        "facts": facts[:8],
        "recent_dialogue": stable[-16:],
        "last_remote": None,
        "previous_guidance": (prior or {}).get("previous_guidance", [])[-5:],
    }
    last_question = None
    for segment in stable:
        text = segment["text"]
        if segment.get("side") == "rep":
            if "?" in text:
                item = evidence(segment)
                state["asked_questions"].append(item)
                last_question = item
            continue
        if segment.get("side") != "remote":
            continue
        state["last_remote"] = evidence(segment)
        if last_question:
            state["answered_questions"].append({**evidence(segment), "question": last_question["text"]})
            last_question = None
        if re.search(
            r"not interested|no thanks|goodbye|do not call|don't call|stop calling|remove (?:me|my number)",
            text,
            re.I,
        ):
            state.update(stage="declined", trigger="NO_ACTION")
            continue
        if state["stage"] == "declined":
            continue
        state["stage"] = "discovery"
        topics = [topic for topic, pattern in TOPICS.items() if re.search(pattern, text, re.I)]
        resolved_budget = bool(
            re.search(
                r"(?:budget|funding).*(?:approved|allocated|available)|cost is not.*(?:issue|problem)",
                text,
                re.I,
            )
        )
        provider_happy = bool(
            re.search(r"(?:provider|supplier|vendor).*(?:works well|happy|satisfied)|^nothing\b", text, re.I)
        )
        if resolved_budget:
            for objection in state["objections"]:
                if objection["kind"] == "budget":
                    objection["status"] = "resolved"
                    objection["resolution_evidence"] = [segment["segment_id"]]
            topics = [topic for topic in topics if topic != "budget"]
        if provider_happy:
            for objection in state["objections"]:
                if objection["kind"] == "provider":
                    objection["status"] = "resolved"
                    objection["resolution_evidence"] = [segment["segment_id"]]
            topics = [topic for topic in topics if topic != "provider"]
        for topic in topics:
            if topic in {"budget", "provider", "timing"}:
                state["objections"].append(evidence(segment, kind=topic, status="unresolved"))
                state.update(stage="objection", trigger=topic)
            elif topic == "need":
                state["needs"].append(evidence(segment, status="confirmed"))
                state["trigger"] = "discovery"
            elif topic == "next_step":
                state["buying_signals"].append(evidence(segment, status="confirmed"))
                state.update(stage="next_step", trigger="next_step")
        if not topics:
            state["trigger"] = (
                "question" if "?" in text else "conversation_gap" if len(text.split()) >= 8 else "NO_ACTION"
            )
    state["asked_questions"] = state["asked_questions"][-12:]
    state["answered_questions"] = state["answered_questions"][-12:]
    sources = {s["segment_id"]: s for s in stable}
    for key in (
        "needs",
        "business_impact",
        "objections",
        "answered_questions",
        "buying_signals",
        "commitments",
    ):
        for item in (prior or {}).get(key, []):
            ids, revisions, quote = (
                item.get("evidence_segment_ids", []),
                item.get("evidence_revisions", {}),
                item.get("quote"),
            )
            if not ids or not quote or item.get("text") != quote:
                continue
            if any(
                identifier not in sources
                or revisions.get(identifier) != sources[identifier].get("revision", 1)
                for identifier in ids
            ):
                continue
            if quote.casefold() not in " ".join(sources[identifier]["text"] for identifier in ids).casefold():
                continue
            if not any(
                current.get("text") == item["text"] and current.get("evidence_segment_ids") == ids
                for current in state[key]
            ):
                state[key].append(item)
        state[key] = state[key][-12:]
    return state


def retrieve_cards(state, cards):
    if state["stage"] == "declined" or state["trigger"] == "NO_ACTION":
        return []
    asked = [item["text"] for item in state["asked_questions"]] + state.get("previous_guidance", [])
    trigger = state["trigger"]
    aliases = {
        "question": {"question", "discovery"},
        "conversation_gap": {"discovery"},
        "manual_help": {"discovery", "question"},
        "next_step": {"next_step", "meeting", "callback"},
        "timing": {"timing", "callback"},
    }
    wanted = aliases.get(trigger, {trigger})
    query = words((state.get("last_remote") or {}).get("text", ""))
    result = []
    for card in cards:
        text, tokens = card.get("text", ""), words(card.get("text", ""))
        if not text or len(text.split()) > 25:
            continue
        if any(
            text.casefold() == prior.casefold()
            or len(tokens & words(prior)) / max(1, len(tokens | words(prior))) > 0.7
            for prior in asked
        ):
            continue
        tags = set(card.get("tags", []))
        resolved = {o.get("kind") for o in state["objections"] if o["status"] == "resolved"}
        if tags & resolved:
            continue
        score = 10 * bool(tags & wanted) + len(tokens & query) / max(1, len(tokens))
        # Unrelated objection cards are not a generic fallback.
        if not tags & wanted and "discovery" not in tags and score < 0.3:
            continue
        result.append((score, card))
    return [card for _, card in sorted(result, key=lambda item: (-item[0], item[1]["id"]))[:4]]


def ground_guidance(card, request):
    segments = {
        s["segment_id"]: s
        for s in request["recent_dialogue"]
        if s.get("is_final") and not s.get("system_audio")
    }
    cards = {c["id"]: c for c in request["approved_cards"]}
    validate_card(
        card, state_version=request["state_version"], segment_ids=set(segments), card_ids=set(cards)
    )
    if card["action"] == "NO_ACTION":
        return True
    approved = [
        text
        for identifier in card["playbook_card_ids"]
        for text in [cards[identifier]["text"], *cards[identifier].get("approved_variants", [])]
    ]
    if card["say_or_ask"].strip().casefold() not in {text.strip().casefold() for text in approved}:
        raise CardRejected("Guidance meaning is outside the reviewed approved formulations")
    if request.get("state", {}).get("stage") == "declined":
        raise CardRejected("Conversation has declined")
    support = " ".join([segments[i]["text"] for i in card["evidence_segment_ids"]] + approved)
    if not set(re.findall(r"\d+(?:\.\d+)?", card.get("reason", ""))).issubset(
        set(re.findall(r"\d+(?:\.\d+)?", support))
    ):
        raise CardRejected("Unsupported numerical rationale")
    return True


def merge_semantic_state(state, update, segments):
    if not update:
        return state
    sources = {
        s["segment_id"]: s["text"] for s in segments if s.get("is_final") and not s.get("system_audio")
    }
    allowed_stages = {"introduction", "discovery", "objection", "next_step", "declined"}
    if update.get("stage") not in allowed_stages:
        raise CardRejected("Unknown conversation stage")
    merged = dict(state)
    for key in (
        "needs",
        "business_impact",
        "objections",
        "answered_questions",
        "buying_signals",
        "commitments",
    ):
        values = update.get(key, [])
        if not isinstance(values, list) or len(values) > 12:
            raise CardRejected("Unbounded conversation state")
        for item in values:
            ids, quote = item.get("evidence_segment_ids", []), item.get("quote", "")
            if not ids or not set(ids).issubset(sources) or not quote or item.get("text") != quote:
                raise CardRejected("Ungrounded semantic state")
            if quote.casefold() not in " ".join(sources[i] for i in ids).casefold():
                raise CardRejected("State evidence is absent")
        if values:
            revisions = {s["segment_id"]: s.get("revision", 1) for s in segments}
            merged[key] = (
                state.get(key, [])
                + [
                    {
                        **item,
                        "evidence_revisions": {
                            identifier: revisions[identifier] for identifier in item["evidence_segment_ids"]
                        },
                    }
                    for item in values
                ]
            )[-12:]
    if state["stage"] != "declined":
        merged["stage"] = update["stage"]
    return merged
