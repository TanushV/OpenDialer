import asyncio
import threading
import time
from collections import OrderedDict

import httpx

from app.coaching.contracts import Guidance
from app.providers.inference import InferenceError

ADMISSION_SECONDS = 0.1


class CardRejected(ValueError):
    pass


def monotonic():
    """Coaching's one deadline clock. Tests replace this seam to drive deadlines deterministically."""
    return time.monotonic()


def transport_fault(exc):
    """Only an unreachable or overloaded route opens a breaker, never a rejected response."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, TimeoutError | ConnectionError | httpx.TransportError)


def failure(exc):
    """A bounded receipt reason; provider text never becomes a status."""
    if transport_fault(exc):
        return "transport_fault"
    reason = getattr(exc, "reason", None) if isinstance(exc, InferenceError) else None
    return reason if reason in {"provider_unverified", "provider_mismatch"} else "invalid_response"


async def finish_shielded(work):
    """Finish a short storage/settlement operation even under repeated cancellation."""
    task, cancelled = asyncio.ensure_future(work), False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError()
    return result


def validate_card(card, *, state_version, segment_ids, card_ids, manual=False):
    if not isinstance(card, dict):
        raise CardRejected("Invalid guidance object")
    if card.get("action") == "NO_ACTION":
        return True
    try:
        # Bounds the primary text, explanation, alternatives and lifetime.
        Guidance.model_validate(
            {"alternatives": [], **{key: card[key] for key in Guidance.model_fields if key in card}}
        )
    except ValueError as exc:
        raise CardRejected("Invalid bounded guidance") from exc
    if card.get("action") != "SHOW_CARD" or card.get("state_version") != state_version:
        raise CardRejected("Obsolete conversation state")
    if (not manual and not card.get("evidence_segment_ids")) or not set(
        card.get("evidence_segment_ids", [])
    ).issubset(segment_ids):
        raise CardRejected("Unrecognized transcript evidence")
    if not set(card.get("playbook_card_ids", [])).issubset(card_ids):
        raise CardRejected("Unapproved playbook source")
    return True


class CoachEngine:
    def __init__(self, limits=None):
        from app.config import RuntimeLimits

        self.limits = limits or RuntimeLimits()
        self.active = {}
        self._manual = set()
        self._identities = {}
        self._pending = {}
        self._lock = threading.RLock()
        self._circuits = OrderedDict()
        self.capacity = asyncio.Semaphore(self.limits.live_active)
        self.max_pending = self.limits.live_pending

    def _circuit(self, key, failed=None):
        with self._lock:
            count, until = self._circuits.get(key, (0, 0))
            if failed is not None:
                count = count + 1 if failed else 0
                until = monotonic() + 30 if count >= 3 else 0
                self._circuits[key] = (count, until)
                self._circuits.move_to_end(key)
                while len(self._circuits) > 256:
                    self._circuits.popitem(last=False)
            return monotonic() < until

    def cancel(self, call_id):
        """Invalidate first; cancellation from a DB thread must use the owning loop."""
        with self._lock:
            task = self.active.pop(call_id, None)
            self._manual.discard(call_id)
            self._identities.pop(call_id, None)
            if task and not task.done() and not task.get_loop().is_closed():
                task.get_loop().call_soon_threadsafe(task.cancel)
            return task is not None

    def current(self, call_id, identity):
        with self._lock:
            task = self.active.get(call_id)
            return bool(task and not task.cancelling() and self._identities.get(call_id) == identity)

    async def suggest(
        self,
        call_id,
        request,
        provider,
        fallback,
        deadline_ms=1200,
        *,
        admit=None,
        settle=None,
        publish=None,
        circuit_key=None,
        owner_key=None,
    ):
        """Run the one current request for a call.

        `fallback` (cached seller knowledge) is used only while this route and mode's circuit is
        open. A failed request stays quiet with an explicit unavailable status. `admit(submit,
        admission_deadline)` must not count or reserve work after the admission deadline.
        """
        manual = bool(request.get("manual_help") or request.get("private_question"))
        # Manual and automatic help have separate breakers: a slow manual answer cannot
        # silence proactive help, nor the reverse.
        circuit = (circuit_key, "manual" if manual else "automatic")
        began = monotonic()
        deadline = began + deadline_ms / 1000
        admission_deadline = min(deadline, began + ADMISSION_SECONDS)

        def current():
            with self._lock:
                return self.active.get(call_id) is task and not task.cancelling()

        async def perform():
            submitted, acquired, settled = False, False, False
            try:
                try:
                    remaining = admission_deadline - monotonic()
                    if remaining <= 0:
                        return {"action": "NO_ACTION", "status": "busy"}
                    await asyncio.wait_for(self.capacity.acquire(), remaining)
                    acquired = True
                except TimeoutError:
                    return {"action": "NO_ACTION", "status": "busy"}
                if monotonic() >= admission_deadline:
                    return {"action": "NO_ACTION", "status": "busy"}
                if not current():
                    return {"action": "NO_ACTION", "status": "superseded"}
                can_submit = not self._circuit(circuit)
                admitted = admit is None or await admit(can_submit, admission_deadline)
                # One ceiling covers all of admission, including its storage/budget step.
                if monotonic() >= admission_deadline:
                    return {"action": "NO_ACTION", "status": "busy"}
                result = {"action": "NO_ACTION"} if can_submit else fallback
                remaining = deadline - monotonic()
                if admitted and can_submit and current() and remaining > 0:
                    submitted = True
                    try:
                        async with asyncio.timeout(remaining) as limit:
                            result = await provider.suggest(
                                request, deadline_ms=max(1, int(remaining * 1000))
                            )
                        # A provider can swallow cancellation and return late advice.
                        if limit.expired():
                            raise TimeoutError()
                        self._circuit(circuit, False)
                    except Exception as exc:
                        if transport_fault(exc):
                            self._circuit(circuit, True)
                        result = {"action": "NO_ACTION", "status": "unavailable", "failure": failure(exc)}
                if settle:
                    settled = True
                    await finish_shielded(settle(submitted))
                if not current():
                    return {"action": "NO_ACTION", "status": "superseded"}
                # Publication includes optional review. Retain this same capacity
                # slot until it finishes; another semaphore would multiply work.
                return await publish(result) if publish else result
            finally:
                try:
                    if settle and not settled:
                        await finish_shielded(settle(submitted))
                finally:
                    if acquired:
                        self.capacity.release()

        with self._lock:
            if call_id in self._manual and not manual:
                return {"action": "NO_ACTION", "status": "manual_pending"}
            if len(self._pending) >= self.max_pending or (
                owner_key is not None
                and list(self._pending.values()).count(owner_key) >= self.limits.live_per_rep
            ):
                return {"action": "NO_ACTION", "status": "busy"}
            self.cancel(call_id)
            task = asyncio.create_task(perform())
            self.active[call_id] = task
            self._identities[call_id] = request.get("help_request_id")
            self._pending[task] = owner_key
            if manual:
                self._manual.add(call_id)
        try:
            result = await task
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError()
            return result
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            return {"action": "NO_ACTION", "status": "superseded"}
        finally:
            with self._lock:
                self._pending.pop(task, None)
                if self.active.get(call_id) is task:
                    self.active.pop(call_id, None)
                    self._manual.discard(call_id)
                    self._identities.pop(call_id, None)
