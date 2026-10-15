import asyncio
import time

from app.research.validation import BANNED


class CardRejected(ValueError):
    pass


def validate_card(card, *, state_version, segment_ids, card_ids):
    if card.get("action") == "NO_ACTION":
        return True
    if card.get("action") != "SHOW_CARD" or card.get("state_version") != state_version:
        raise CardRejected("Obsolete conversation state")
    if not card.get("evidence_segment_ids") or not set(card["evidence_segment_ids"]).issubset(segment_ids):
        raise CardRejected("Unrecognized transcript evidence")
    if not card.get("playbook_card_ids") or not set(card["playbook_card_ids"]).issubset(card_ids):
        raise CardRejected("Unapproved playbook source")
    if len(card.get("say_or_ask", "").split()) > 25 or BANNED.search(card.get("say_or_ask", "")):
        raise CardRejected("Unsafe or excessive guidance")
    if not 0 < card.get("expires_in_ms", 0) <= 10000:
        raise CardRejected("Invalid card lifetime")
    return True


class CoachEngine:
    def __init__(self):
        self.active = {}
        self.failures = 0
        self.open_until = 0.0

    async def suggest(self, call_id, request, provider, fallback, deadline_ms=1200):
        old = self.active.get(call_id)
        if old:
            old.cancel()
        if time.monotonic() < self.open_until:
            return fallback

        async def perform():
            try:
                result = await asyncio.wait_for(provider.suggest(request, deadline_ms), deadline_ms / 1000)
                self.failures = 0
                return result
            except (TimeoutError, Exception):
                self.failures += 1
                if self.failures >= 3:
                    self.open_until = time.monotonic() + 30
                return fallback

        task = asyncio.create_task(perform())
        self.active[call_id] = task
        try:
            return await task
        finally:
            if self.active.get(call_id) is task:
                self.active.pop(call_id, None)
