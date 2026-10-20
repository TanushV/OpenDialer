import { useState, useEffect, useRef } from "react";
import { Sparkles, Pin, MicOff, X } from "lucide-react";
import { useWorkspace } from "../context";
import { isActiveCall, type Call } from "../types";

/** Primary helper feedback stays one click away; rarer reasons sit under "More". */
const PRIMARY_FEEDBACK: [string, string, Record<string, boolean>][] = [
  ["Useful", "Guidance was useful", { useful: true }],
  ["Not useful", "Guidance was not useful", { useful: false }],
  ["Wrong", "Guidance was wrong", { grounded: false }],
];
const MORE_FEEDBACK: [string, Record<string, boolean>][] = [
  ["Guidance arrived too late", { timely: false }],
  ["Guidance was distracting", { distracting: true }],
  ["I used this guidance", { used_by_rep: true }],
  ["I did not use this guidance", { used_by_rep: false }],
];

const PAUSED: Record<string, string> = {
  transport_fault: "connection interrupted",
  invalid_response: "last answer could not be used",
  provider_unverified: "provider not verified",
  provider_mismatch: "provider route changed",
};

/** The authority's last helper receipt, as calm status text beside the script. */
function HelperStatus({ call }: { call: Call | null }) {
  const helper = call?.conversation_state?.helper_status;
  if (!helper) return null;
  const text =
    helper.status === "available"
      ? "Live help available"
      : helper.status === "cached_knowledge"
        ? "Using your approved examples"
        : `Live help paused · ${PAUSED[helper.reason ?? ""] ?? "unavailable"}. Keep using your script.`;
  return (
    <span
      className="fine helper-status"
      role="status"
      data-testid="helper-status"
    >
      {text}
    </span>
  );
}

export function LiveCoach({
  call,
  onChange,
  deliveryTiming,
}: {
  call: Call | null;
  onChange: () => Promise<void>;
  deliveryTiming?: { receivedAt: number | null; eventAt: number | null };
}) {
  const { api, run, notice } = useWorkspace();
  const strip = useRef<HTMLElement>(null);
  const displayRequest = useRef<{ id: string; requestId: string } | null>(null);
  const [savingFeedback, setSavingFeedback] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [question, setQuestion] = useState("");
  const [waiting, setWaiting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const pending = useRef<{ id: string; callId: string } | null>(null);
  const questionInput = useRef<HTMLInputElement>(null);
  const expandButton = useRef<HTMLButtonElement>(null);
  const [dismissed, setDismissed] = useState<string | null>(null);
  const hasQuestion = !!question;
  useEffect(() => {
    if (expanded) questionInput.current?.focus();
  }, [expanded]);
  useEffect(() => {
    const timer = setTimeout(() => setQuestion(""), 300000);
    return () => clearTimeout(timer);
  }, [hasQuestion]);
  useEffect(() => {
    setQuestion("");
    setDismissed(null);
    setExpanded(false);
  }, [call?.id]);
  const advice = call?.coach?.id !== dismissed ? call?.coach : null;
  const active = isActiveCall(call) && call?.state !== "ENDING";
  useEffect(() => {
    if (!advice || !call || !active) return;
    const id = advice.id,
      callId = call.id;
    if (displayRequest.current?.id !== id)
      displayRequest.current = { id, requestId: crypto.randomUUID() };
    const requestId = displayRequest.current.requestId;
    let sent = false,
      frame = 0;
    const acknowledge = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const rect = strip.current?.getBoundingClientRect();
        if (
          sent ||
          document.visibilityState !== "visible" ||
          !rect ||
          rect.bottom <= 0 ||
          rect.top >= window.innerHeight
        )
          return;
        sent = true;
        const at = performance.now();
        void api(`/v1/calls/${callId}/coach/feedback`, "POST", {
          coach_event_id: id,
          request_id: requestId,
          displayed: true,
          render_ms:
            deliveryTiming?.receivedAt == null
              ? undefined
              : Math.max(0, at - deliveryTiming.receivedAt),
          event_to_render_ms:
            deliveryTiming?.eventAt == null
              ? undefined
              : Math.max(0, at - deliveryTiming.eventAt),
        }).catch(() => {
          /* Missing acknowledgements remain unknown. */
        });
      });
    };
    const observer = new IntersectionObserver(acknowledge);
    if (strip.current) observer.observe(strip.current);
    document.addEventListener("visibilitychange", acknowledge);
    acknowledge();
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      document.removeEventListener("visibilitychange", acknowledge);
    };
  }, [api, advice?.id, call?.id, active]);
  async function saveFeedback(fields: Record<string, boolean>) {
    if (!call || !advice || savingFeedback) return;
    setSavingFeedback(true);
    const path = `/v1/calls/${call.id}/coach/feedback`;
    try {
      const previous = await api<{ version: number }>(
        `${path}?coach_event_id=${encodeURIComponent(advice.id)}`,
      );
      await api(path, "POST", {
        coach_event_id: advice.id,
        request_id: crypto.randomUUID(),
        expected_version: previous.version,
        ...fields,
      });
      notice("Guidance feedback saved");
    } finally {
      setSavingFeedback(false);
    }
  }
  useEffect(() => {
    setWaiting(false);
    setCancelling(false);
    return () => {
      const old = pending.current;
      pending.current = null;
      if (old)
        void api(`/v1/calls/${old.callId}/coach/cancel`, "POST", {
          request_id: old.id,
        }).catch(() => {
          /* Server request lifetime and call-state fences still apply. */
        });
    };
  }, [call?.id, active, api]);
  // One window listener reads the latest state instead of re-registering per render.
  const keys = useRef({ expanded, waiting, active, help, cancelHelp });
  keys.current = { expanded, waiting, active, help, cancelHelp };
  useEffect(() => {
    const hotkey = (event: KeyboardEvent) => {
      const current = keys.current;
      if (event.key === "Escape" && current.expanded) {
        event.preventDefault();
        if (current.waiting) void run(current.cancelHelp);
        setExpanded(false);
        expandButton.current?.focus();
        return;
      }
      if (event.key !== "F1" || !current.active) return;
      event.preventDefault();
      if (!event.repeat) void run(current.help);
    };
    window.addEventListener("keydown", hotkey);
    return () => window.removeEventListener("keydown", hotkey);
  }, [run]);
  async function preference(fields: { muted?: boolean; pinned?: boolean }) {
    if (!call) return;
    await api(`/v1/calls/${call.id}/coach`, "PATCH", fields);
    await onChange();
  }
  async function help() {
    if (!call || !active || pending.current) return;
    const current = { id: crypto.randomUUID(), callId: call.id };
    pending.current = current;
    setWaiting(true);
    const submitted = question;
    setQuestion("");
    try {
      const response = await api<{ status?: string }>(
        `/v1/calls/${call.id}/coach`,
        "POST",
        {
          question: submitted || null,
          urgent: true,
          request_id: current.id,
        },
      );
      if (pending.current !== current) return;
      if (response.status === "busy")
        notice("The helper is busy. Try again shortly.");
      else if (
        ["no_action", "unavailable", "expired_or_corrected"].includes(
          response.status || "",
        )
      )
        notice(
          "No supported suggestion is available for this context. Keep using your prepared script.",
        );
      setDismissed(null);
      await onChange();
    } finally {
      if (pending.current === current) {
        pending.current = null;
        setWaiting(false);
      }
    }
  }
  async function cancelHelp() {
    const current = pending.current;
    if (!current || cancelling) return;
    setCancelling(true);
    try {
      await api(`/v1/calls/${current.callId}/coach/cancel`, "POST", {
        request_id: current.id,
      });
      if (pending.current === current) {
        await onChange();
        if (pending.current === current) {
          pending.current = null;
          setWaiting(false);
          setCancelling(false);
        }
      }
    } finally {
      if (pending.current === current) setCancelling(false);
    }
  }
  return (
    <section
      ref={strip}
      className="live-coaching-strip"
      data-testid="live-coaching-strip"
      aria-label="Live coaching"
    >
      <div className="helper-topline">
        <strong>
          <Sparkles size={15} /> Live helper
        </strong>
        <span className="fine">
          {advice?.status === "streaming"
            ? "Answer ready · finishing explanation…"
            : waiting
              ? "Thinking with your call context…"
              : call?.coach_preferences?.muted
                ? "Automatic tips muted"
                : "Quiet guidance alongside your script"}
        </span>
        <HelperStatus call={call} />
        <div className="helper-actions">
          {waiting && (
            <button
              className="text-button"
              disabled={cancelling}
              onClick={() => void run(cancelHelp)}
            >
              {cancelling ? "Cancelling…" : "Cancel help"}
            </button>
          )}
          <button
            className="text-button"
            disabled={!active}
            onClick={() =>
              void run(() =>
                preference({ muted: !call?.coach_preferences?.muted }),
              )
            }
          >
            <MicOff size={13} />
            {call?.coach_preferences?.muted ? "Enable tips" : "Mute tips"}
          </button>
          <button
            className="text-button"
            aria-expanded={expanded}
            ref={expandButton}
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? "Less" : "Private help"}
          </button>
        </div>
      </div>
      <div className="helper-primary" aria-live="polite">
        <p>
          {advice?.say_or_ask ||
            (active
              ? "Listening for useful context. Keep following your script."
              : "Guidance will follow the call. Your prepared script stays in front of you.")}
        </p>
        {advice && (
          <div className="helper-actions">
            {PRIMARY_FEEDBACK.map(([text, name, fields]) => (
              <button
                key={name}
                className="text-button"
                aria-label={name}
                disabled={savingFeedback}
                onClick={() => void run(() => saveFeedback(fields))}
              >
                {text}
              </button>
            ))}
            <details className="feedback-more">
              <summary>More</summary>
              <div className="guidance-feedback" aria-label="More feedback">
                {MORE_FEEDBACK.map(([name, fields]) => (
                  <button
                    key={name}
                    className="text-button"
                    disabled={savingFeedback}
                    onClick={() => void run(() => saveFeedback(fields))}
                  >
                    {name}
                  </button>
                ))}
              </div>
            </details>
            <button
              className="icon-button"
              aria-label={advice.pinned ? "Unpin guidance" : "Pin guidance"}
              onClick={() =>
                void run(() => preference({ pinned: !advice.pinned }))
              }
            >
              <Pin size={15} />
            </button>
            <button
              className="icon-button"
              aria-label="Dismiss guidance"
              onClick={() => {
                setDismissed(advice.id);
                void run(() => preference({ pinned: false }));
              }}
            >
              <X size={15} />
            </button>
          </div>
        )}
      </div>
      {expanded && (
        <div className="helper-detail">
          {advice && (
            <>
              <p>{advice.reason}</p>
              {advice.alternatives?.map((alternative, index) => (
                <p key={index}>Alternative: {alternative}</p>
              ))}
              <small>
                {advice.private ? "Private answer · " : ""}
                {advice.meaning_review?.status === "model_reviewed"
                  ? "Model-reviewed guidance"
                  : advice.provider === "cached_knowledge"
                    ? "Seller-approved example · AI unavailable"
                    : "Model suggestion"}
              </small>
            </>
          )}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void run(help);
            }}
            className="input-row"
          >
            <input
              ref={questionInput}
              aria-label="Private helper question"
              placeholder="Optional question — never sent to the prospect"
              maxLength={2000}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
            />
            <button
              className="secondary"
              disabled={!active || waiting}
              type="submit"
            >
              {waiting ? "Working…" : "Ask privately"}
            </button>
          </form>
          <small>
            F1 requests help without typing. Advice and raw context expire
            within five minutes.
          </small>
        </div>
      )}
    </section>
  );
}
