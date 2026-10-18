import { useState, useEffect } from "react";
import { Sparkles, Pin, MicOff, X } from "lucide-react";
import { useWorkspace } from "../context";
import type { Call } from "../types";
import { terminal } from "../types";

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
  const [dismissed, setDismissed] = useState<string | null>(null);
  const hasQuestion = !!question;
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
  const active = !!call && !terminal(call) && call.state !== "ENDING";
  async function preference(fields: { muted?: boolean; pinned?: boolean }) {
    if (!call) return;
    await api(`/v1/calls/${call.id}/coach`, "PATCH", fields);
    await onChange();
  }
  async function help() {
    if (!call || waiting) return;
    setWaiting(true);
    const submitted = question;
    setQuestion("");
    try {
      await api(`/v1/calls/${call.id}/coach`, "POST", {
        question: submitted || null,
        urgent: true,
      });
      setDismissed(null);
      await onChange();
    } finally {
      setWaiting(false);
    }
  }
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
            ? "Thinking with your call context…"
            : call?.coach_preferences?.muted
              ? "Automatic tips muted"
              : "Quiet guidance alongside your script"}
        </span>
        <div className="helper-actions">
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
              <small>
                {advice.private
                  ? "Private answer"
                  : advice.provider === "cached_knowledge"
                    ? "Cached knowledge example · AI unavailable"
                    : "Contextual suggestion · verify material claims"}
              </small>
              <div className="guidance-feedback" aria-label="Guidance feedback">
                {[
                  ["Guidance was useful", { useful: true }],
                  ["Guidance was not useful", { useful: false }],
                  ["Guidance was distracting", { distracting: true }],
                  ["Guidance arrived too late", { timely: false }],
                  ["Guidance was wrong", { grounded: false }],
                ].map(([title, fields]) => (
                  <button
                    key={String(title)}
                    className="text-button"
                    onClick={() =>
                      void run(async () => {
                        await api(
                          `/v1/calls/${call!.id}/coach/feedback`,
                          "POST",
                          {
                            coach_event_id: advice.id,
                            ...(fields as Record<string, boolean>),
                          },
                        );
                        notice("Guidance feedback saved");
                      })
                    }
                  >
                    {String(title)}
                  </button>
                ))}
              </div>
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
