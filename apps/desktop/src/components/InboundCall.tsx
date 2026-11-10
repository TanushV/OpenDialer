import { useEffect, useMemo, useState } from "react";
import { native } from "../api/client";
import { label, useWorkspace } from "../context";
import { createRinger } from "../telephony/ringer";
import type { FollowupTask } from "../types";

/** Bounce the Dock when a callback rings while another app is in front (D29). */
async function askForAttention() {
  if (!native() || document.hasFocus()) return;
  try {
    const { getCurrentWindow, UserAttentionType } =
      await import("@tauri-apps/api/window");
    await getCurrentWindow().requestUserAttention(UserAttentionType.Critical);
  } catch (error) {
    console.warn("The Dock could not be bounced for a callback:", error);
  }
}

/**
 * A callback to this app (D29): it rings with a tone on any page; Answer takes it, and Decline sends the caller to
 * the greeting and keeps a missed-call task.
 */
export function InboundCall({
  task,
  accept,
  canAccept,
  audioActive,
  mute,
  localHangup,
}: {
  task: FollowupTask;
  accept: () => Promise<void>;
  canAccept: boolean;
  audioActive: boolean;
  mute: () => void;
  localHangup: () => Promise<void>;
}) {
  const { api, run, refresh } = useWorkspace();
  const [muted, setMuted] = useState(false);
  const [accepting, setAccepting] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const connected = task.inbound_state === "connected";
  const ringing = task.inbound_state === "ringing" && !accepting && !accepted;
  const ringer = useMemo(() => createRinger(), []);
  useEffect(() => {
    if (!ringing) return;
    ringer.start();
    void askForAttention();
    return () => ringer.stop();
  }, [ringing, ringer]);
  const who = task.lead_name
    ? task.lead_name + (task.lead_company ? ` · ${task.lead_company}` : "")
    : "Not one of your leads";
  async function endOnServer() {
    try {
      await api(`/v1/callbacks/${task.id}/hangup`, "POST");
    } finally {
      await localHangup();
      await refresh();
    }
  }
  return (
    <section
      className="card padded"
      aria-label="Incoming call"
      data-ringing={ringing ? "true" : "false"}
    >
      <h2>
        {connected ? "On a callback" : "Incoming call"} ·{" "}
        {label(task.inbound_state || "routing")}
      </h2>
      <p>
        <strong>{who}</strong> · {task.callback_phone || "Unidentified caller"}
      </p>
      <div className="button-row">
        {!connected && !accepted && (
          <>
            <button
              disabled={!canAccept || accepting}
              onClick={() =>
                void run(async () => {
                  setAccepting(true);
                  try {
                    await accept();
                    setAccepted(true);
                  } finally {
                    setAccepting(false);
                  }
                })
              }
            >
              Answer
            </button>
            <button
              className="secondary"
              onClick={() =>
                void run(async () => {
                  // Dropping only this app's leg lets the caller hear the greeting; a task is kept.
                  if (canAccept) {
                    await localHangup();
                    await refresh();
                  } else await endOnServer();
                })
              }
            >
              Decline
            </button>
          </>
        )}
        {(connected || accepted) && (
          <>
            <button
              className="secondary"
              disabled={!audioActive}
              onClick={() => {
                mute();
                setMuted(!muted);
              }}
            >
              {muted ? "Unmute" : "Mute"}
            </button>
            <button
              className="danger-button"
              onClick={() => void run(endOnServer)}
            >
              Hang up
            </button>
          </>
        )}
      </div>
      {!connected && !canAccept && !accepted && (
        <p role="status">Connecting the call to this app…</p>
      )}
      {accepted && !connected && (
        <p role="status">Answered. Connecting the audio…</p>
      )}
      {task.review_required && (
        <p role="status">Routing needs review. The follow-up remains saved.</p>
      )}
    </section>
  );
}
