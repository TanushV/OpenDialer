import asyncio
import threading
import time
from collections import OrderedDict

from app.coaching.contracts import Guidance


class CardRejected(ValueError):
    pass


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
    if not card.get("say_or_ask") or len(card.get("say_or_ask", "")) > 1000:
        raise CardRejected("Unsafe or excessive guidance")
    if not 0 < card.get("expires_in_ms", 0) <= 300000:
        raise CardRejected("Invalid card lifetime")
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

    @property
    def failures(self):
        return self._circuits.get(None, (0, 0))[0]

    @property
    def open_until(self):
        return self._circuits.get(None, (0, 0))[1]

    def _circuit(self, key, failed=None):
        with self._lock:
            count, until = self._circuits.get(key, (0, 0))
            if failed is not None:
                count = count + 1 if failed else 0
                until = time.monotonic() + 30 if count >= 3 else 0
                self._circuits[key] = (count, until)
                self._circuits.move_to_end(key)
                while len(self._circuits) > 256:
                    self._circuits.popitem(last=False)
            return time.monotonic() < until

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
        manual = bool(request.get("manual_help") or request.get("private_question"))
        began = time.monotonic()
        deadline = began + deadline_ms / 1000
        admission_deadline = min(deadline, began + 0.1)

        def current():
            with self._lock:
                return self.active.get(call_id) is task and not task.cancelling()

        async def perform():
            submitted, acquired, settled = False, False, False
            result = fallback
            try:
                try:
                    remaining = admission_deadline - time.monotonic()
                    if remaining <= 0:
                        return {"action": "NO_ACTION", "status": "busy"}
                    await asyncio.wait_for(self.capacity.acquire(), remaining)
                    acquired = True
                except TimeoutError:
                    return {"action": "NO_ACTION", "status": "busy"}
                if time.monotonic() >= admission_deadline:
                    return {"action": "NO_ACTION", "status": "busy"}
                if not current():
                    return {"action": "NO_ACTION", "status": "superseded"}
                can_submit = not self._circuit(circuit_key)
                if (admit is None or await admit(can_submit)) and can_submit:
                    remaining = deadline - time.monotonic()
                    if current() and remaining > 0:
                        submitted = True
                        try:
                            result = await asyncio.wait_for(
                                provider.suggest(request, deadline_ms=max(1, int(remaining * 1000))),
                                remaining,
                            )
                            # wait_for cannot force an upstream coroutine to honor cancellation.
                            if time.monotonic() > deadline:
                                raise TimeoutError()
                            self._circuit(circuit_key, False)
                        except Exception:
                            self._circuit(circuit_key, True)
                            result = fallback
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
