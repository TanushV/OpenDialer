"""Bounded conversational context; semantic interpretation belongs to the configured model."""

import re

from app.coaching.engine import CardRejected, validate_card

STATE_FIELDS = (
    "needs",
    "business_impact",
    "objections",
    "answered_questions",
    "buying_signals",
    "commitments",
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
    }
    for key in STATE_FIELDS:
        # Expiry does not invalidate a paraphrase; a source correction does.
        state[key] = [
            i
            for i in prior.get(key, [])
            if not any(
                identifier in sources and revision != sources[identifier].get("revision", 1)
                for identifier, revision in i.get("evidence_revisions", {}).items()
            )
        ][-12:]
    state["commitments"] = (state["commitments"] + commitments)[-12:]
    for segment in stable:
        if segment.get("side") == "rep" and "?" in segment["text"]:
            state["asked_questions"].append(evidence(segment))
        elif segment.get("side") == "remote":
            state["last_remote"] = evidence(segment)
            state["trigger"] = "conversation"
    # Only explicit refusal/opt-out is a local safety signal; no fixed objection taxonomy.
    latest = (state["last_remote"] or {}).get("text", "")
    if re.search(
        r"\b(not interested|no thanks|goodbye|do not call|don't call|stop calling|remove me)\b", latest, re.I
    ):
        state.update(stage="declined", trigger="NO_ACTION")
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


def ground_guidance(card, request):
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
    if request.get("state", {}).get("stage") == "declined" and not request.get("private_question"):
        raise CardRejected("Conversation has declined")
    knowledge = " ".join(str(f.get("statement", f.get("text", ""))) for f in request.get("facts", []))
    knowledge += " " + " ".join(str(c) for c in request.get("approved_claims", []))
    knowledge += " " + " ".join(c.get("text", "") for c in cards.values())
    transcript = " ".join(sources[i]["text"] for i in card.get("evidence_segment_ids", []))
    visible = card.get("say_or_ask", "") + " " + card.get("reason", "")

    def numbers(text):
        return set(re.findall(r"\d+(?:\.\d+)?", text))

    if not numbers(visible) <= numbers(knowledge + " " + transcript):
        raise CardRejected("Unsupported numerical assertion")
    # New questions and guidance are freeform. Business assertions require knowledge support.
    # This is an omission guard, not a claim to prove arbitrary semantic entailment.
    for sentence in re.split(r"(?<=[.!?])\s+", card.get("say_or_ask", "")):
        factual = re.search(
            r"\b(?:our|your|their|the company|the product)\b.*\b(?:is|are|has|have|will|can|does|supports|offers|includes|opened)\b",
            sentence,
            re.I,
        )
        if factual and not (
            sentence.rstrip().endswith("?")
            and not re.search(r"\b(?:our|your) (?:price|guarantee)\b", sentence, re.I)
        ):
            support = words(knowledge)
            if not words(sentence) <= support:
                raise CardRejected("Business assertion needs verified knowledge or a clarifying question")
    return True


def merge_semantic_state(state, update, segments):
    if not update:
        return state
    from app.privacy import paraphrased

    sources = {s["segment_id"]: s for s in segments if s.get("is_final") and not s.get("system_audio")}
    if update.get("stage") not in {"introduction", "discovery", "objection", "next_step", "declined"}:
        raise CardRejected("Unknown conversation stage")
    merged = dict(state)
    for key in STATE_FIELDS:
        values = update.get(key, [])
        if not isinstance(values, list) or len(values) > 12:
            raise CardRejected("Unbounded conversation state")
        accepted = []
        for item in values:
            ids, quote, text = (
                item.get("evidence_segment_ids", []),
                item.get("quote", ""),
                item.get("text", ""),
            )
            if not ids or not set(ids) <= set(sources) or not quote or not text:
                raise CardRejected("Missing semantic evidence")
            if quote.casefold() not in " ".join(sources[i]["text"] for i in ids).casefold():
                raise CardRejected("State evidence is absent")
            if not paraphrased(text, segments):
                continue  # Never persist a transcript quotation as a supposed summary.
            accepted.append(
                {
                    "text": text[:400],
                    "status": item.get("status", "open"),
                    "evidence_segment_ids": ids,
                    "evidence_revisions": {i: sources[i].get("revision", 1) for i in ids},
                }
            )
        previous = {tuple(i.get("evidence_segment_ids", [])): i for i in state.get(key, [])}
        previous.update({tuple(i["evidence_segment_ids"]): i for i in accepted})
        merged[key] = list(previous.values())[-12:]
    if state["stage"] != "declined":
        merged["stage"] = update["stage"]
    return merged


def persistent_state(state):
    """Only derived observations survive. Raw context belongs to the TTL buffer."""
    return {
        "version": state["version"],
        "stage": state["stage"],
        "trigger": state["trigger"],
        **{key: state.get(key, [])[-12:] for key in STATE_FIELDS},
    }
