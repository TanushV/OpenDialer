import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { ApiError } from "../api/client";
import { useWorkspace } from "../context";
import { boundKeypad, KEYPAD_REPLAY_MS } from "../state/privacy";

type Pending = {
  call_id: string;
  idempotency_key: string;
  digits: string;
  created_at: number;
};
type Receipt = { status: string; sent: boolean; reason?: string };

const fresh = (value: Pending) =>
  Number.isFinite(value.created_at) &&
  Date.now() - value.created_at >= 0 &&
  Date.now() - value.created_at < KEYPAD_REPLAY_MS;

export function Keypad({
  callId,
  close,
}: {
  callId: string;
  close: () => void;
}) {
  const { api, session } = useWorkspace();
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(true);
  const [message, setMessage] = useState("");
  const [retry, setRetry] = useState(false);
  const [reviewedChoice, setReviewedChoice] = useState(false);
  const locked = useRef(false);
  const mounted = useRef(true);
  const storageKey = `opendialer.keypad-${session.user_id}`;

  async function save(next: Pending | null) {
    // Digits are kept only for the short replay window; the key remains checkable.
    const value = boundKeypad(next);
    if (!mounted.current) return;
    if (value) sessionStorage.setItem(storageKey, JSON.stringify(value));
    else sessionStorage.removeItem(storageKey);
    setPending(value);
  }
  useEffect(() => {
    if (!pending?.digits) return;
    const timer = setTimeout(
      () => void save(pending).catch(() => {}),
      Math.max(0, pending.created_at + KEYPAD_REPLAY_MS - Date.now()),
    );
    return () => clearTimeout(timer);
  }, [pending]);
  async function receive(value: Receipt) {
    if (!mounted.current) return;
    setRetry(false);
    setReviewedChoice(value.status === "review_required");
    if (["completed", "cancelled", "failed"].includes(value.status)) {
      await save(null);
      setMessage(
        value.sent
          ? "Keypad request acknowledged"
          : "Keypad request cancelled; listen before pressing again",
      );
    } else {
      setMessage(
        value.status === "review_required"
          ? "Keypad delivery is uncertain. Listen to the call before choosing another key."
          : "Keypad delivery is pending or uncertain; check its status before retrying.",
      );
    }
  }
  async function check(value: Pending) {
    try {
      await receive(
        await api<Receipt>(`/v1/calls/${callId}/dtmf/${value.idempotency_key}`),
      );
    } catch (error) {
      if (!mounted.current) return;
      const missing = error instanceof ApiError && error.status === 404;
      setRetry(missing && fresh(value));
      setReviewedChoice(missing && !fresh(value));
      setMessage(
        missing
          ? fresh(value)
            ? "The authority has not received this request."
            : "This old key was not received. Check the current menu before choosing a new key."
          : "Keypad status is unavailable; the original request is saved.",
      );
    }
  }
  useEffect(() => {
    let disposed = false;
    mounted.current = true;
    void (async () => {
      let loaded = false;
      try {
        const stored = sessionStorage.getItem(storageKey);
        if (disposed) return;
        const saved = stored ? (JSON.parse(stored) as Pending | null) : null;
        if (
          saved &&
          (typeof saved.call_id !== "string" ||
            !/^[A-Za-z0-9_-]{1,128}$/.test(saved.idempotency_key) ||
            !/^[0-9*#]{0,30}$/.test(saved.digits))
        )
          throw new Error("Invalid saved keypad state");
        loaded = true;
        const value = boundKeypad(saved);
        if (value !== saved) await save(value);
        if (value?.call_id === callId) {
          setPending(value);
          await check(value);
        }
      } catch {
        setMessage(
          "Saved keypad state could not be read. Close and reopen the keypad before sending.",
        );
        return;
      } finally {
        if (!disposed) setBusy(!loaded);
      }
    })();
    return () => {
      disposed = true;
      mounted.current = false;
    };
  }, [callId]);

  async function press(digit?: string) {
    if (locked.current || busy || (digit && pending)) return;
    if (pending && !fresh(pending)) {
      setRetry(false);
      setReviewedChoice(true);
      setMessage(
        "This key is too old to replay. Check the current menu before choosing a new key.",
      );
      return;
    }
    locked.current = true;
    setBusy(true);
    const value = pending || {
      call_id: callId,
      digits: digit!,
      idempotency_key: crypto.randomUUID(),
      created_at: Date.now(),
    };
    try {
      await save(value); // Persist identity before submitting, including a possible lost HTTP response.
      if (!mounted.current) return;
      await receive(
        await api<Receipt>(`/v1/calls/${callId}/dtmf`, "POST", {
          digits: value.digits,
          idempotency_key: value.idempotency_key,
        }),
      );
    } catch {
      if (!mounted.current) return;
      setRetry(fresh(value));
      setReviewedChoice(!fresh(value));
      setMessage(
        "Keypad response unavailable; retry retains the same request.",
      );
    } finally {
      locked.current = false;
      setBusy(false);
    }
  }
  return (
    <div className="keypad-popover" role="region" aria-label="Call keypad">
      <div>
        <strong>Keypad</strong>
        <button
          className="icon-button"
          aria-label="Close keypad"
          onClick={close}
        >
          <X size={15} />
        </button>
      </div>
      <div className="digits">
        {"123456789*0#".split("").map((d) => (
          <button
            key={d}
            disabled={busy || Boolean(pending)}
            onClick={() => void press(d)}
          >
            {d}
          </button>
        ))}
      </div>
      {message && <p role="status">{message}</p>}
      {pending && (
        <>
          {reviewedChoice && (
            <button
              disabled={busy}
              onClick={() => {
                setBusy(true);
                void save(null)
                  .then(() => {
                    setReviewedChoice(false);
                    setMessage(
                      "Previous delivery remains uncertain. Choose a new key only for the current menu.",
                    );
                  })
                  .catch(() =>
                    setMessage("Could not save the local review; try again."),
                  )
                  .finally(() => setBusy(false));
              }}
            >
              I checked the menu; allow a new key
            </button>
          )}
          {retry && (
            <button disabled={busy} onClick={() => void press()}>
              Retry same keypad request
            </button>
          )}
          <button
            disabled={busy}
            onClick={() => {
              setBusy(true);
              void check(pending).finally(() => setBusy(false));
            }}
          >
            Check keypad status
          </button>
        </>
      )}
    </div>
  );
}
