import { useState, useEffect, useRef } from "react";
import { Sparkles, MicOff, X } from "lucide-react";
import { useWorkspace } from "../context";
import { isActiveCall, type Call } from "../types";

/** R10.1: why the helper is quiet, in the rep's words. */
const PAUSED: Record<string, string> = {
  not_configured: "add an OpenRouter key in Settings",
  budget: "this list's AI budget is used up",
  transport_fault: "connection interrupted",
  provider_unverified: "provider not verified",
  provider_mismatch: "provider route changed",
};

function HelperStatus({ call }: { call: Call | null }) {
  const helper = call?.helper_status;
  if (!helper || helper.status === "available") return null;
  return (
    <span
      className="fine helper-status"
      role="status"
      data-testid="helper-status"
    >
      Live help paused · {PAUSED[helper.reason ?? ""] ?? "unavailable"}. Keep
      using your script.
    </span>
  );
}

/** Surface a prepared script response automatically or when the rep asks privately. */
export function LiveCoach({
  call,
  onChange,
}: {
  call: Call | null;
  onChange: () => Promise<void>;
}) {
  const { api, run, notice } = useWorkspace();
  const [expanded, setExpanded] = useState(false);
  const [question, setQuestion] = useState("");
  const [waiting, setWaiting] = useState(false);
  const pending = useRef<{ id: string; callId: string } | null>(null);
  const questionInput = useRef<HTMLInputElement>(null);
  const expandButton = useRef<HTMLButtonElement>(null);
  const [dismissed, setDismissed] = useState<string | null>(null);
  useEffect(() => {
    if (expanded) questionInput.current?.focus();
  }, [expanded]);
  useEffect(() => {
    setQuestion("");
    setDismissed(null);
    setExpanded(false);
  }, [call?.id]);
  const advice = call?.coach && call.coach.id !== dismissed ? call.coach : null;
  const active = isActiveCall(call) && call?.state !== "ENDING";
  useEffect(() => {
    setWaiting(false);
    return () => {
      const old = pending.current;
      pending.current = null;
      if (old)
        void api(`/v1/calls/${old.callId}/coach/cancel`, "POST", {
          request_id: old.id,
        }).catch(() => {
          /* The call's end stops the helper on the server too. */
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
  async function mute(muted: boolean) {
    if (!call) return;
    await api(`/v1/calls/${call.id}/coach`, "PATCH", { muted });
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
        { question: submitted || null, request_id: current.id },
      );
      if (pending.current !== current) return;
      if (response.status === "unavailable")
        notice("Live help is paused. Keep using your script.");
      else if (response.status === "quiet")
        notice("Nothing to add right now. Keep using your script.");
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
    if (!current) return;
    pending.current = null;
    setWaiting(false);
    await api(`/v1/calls/${current.callId}/coach/cancel`, "POST", {
      request_id: current.id,
    });
    await onChange();
  }
  const muted = !!call?.coach_preferences?.muted;
  return (
    <section
      className="live-coaching-strip"
      data-testid="live-coaching-strip"
      aria-label="Live coaching"
    >
      <div className="helper-topline">
        <strong>
          <Sparkles size={15} /> Live helper
        </strong>
        <span className="fine">
          {waiting
            ? "Choosing a script line…"
            : muted
              ? "Automatic tips muted"
              : "Tips come from your script"}
        </span>
        <HelperStatus call={call} />
        <div className="helper-actions">
          {waiting && (
            <button
              className="text-button"
              onClick={() => void run(cancelHelp)}
            >
              Cancel help
            </button>
          )}
          <button
            className="text-button"
            disabled={!active}
            onClick={() => void run(() => mute(!muted))}
          >
            <MicOff size={13} />
            {muted ? "Enable tips" : "Mute tips"}
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
        <div className="helper-advice">
          {advice?.script_part ? (
            <div
              className="helper-script-part"
              data-testid="helper-script-part"
            >
              <small>From your script · {advice.script_part.label}</small>
              <p>{advice.script_part.text}</p>
            </div>
          ) : (
            <p>
              {advice?.say_or_ask ||
                (active
                  ? "Listening. Keep following your script."
                  : "Tips appear here during a call.")}
            </p>
          )}
        </div>
        {advice && (
          <div className="helper-actions">
            {advice.private && <small>Private answer</small>}
            <button
              className="icon-button"
              aria-label="Dismiss guidance"
              onClick={() => {
                setDismissed(advice.id);
                if (call)
                  void api(`/v1/calls/${call.id}/coach`, "PATCH", {
                    dismissed: advice.id,
                  }).catch(() => {});
              }}
            >
              <X size={15} />
            </button>
          </div>
        )}
      </div>
      {expanded && (
        <div className="helper-detail">
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
              placeholder="Optional question, never sent to the prospect"
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
          <small>F1 asks for help without typing.</small>
        </div>
      )}
    </section>
  );
}
