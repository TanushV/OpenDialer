import { useState } from "react";
import { label, useWorkspace } from "../context";
import type { FollowupTask } from "../types";

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
  return (
    <section className="card padded" aria-label="Active return call">
      <h2>Return call · {label(task.inbound_state || "routing")}</h2>
      <p>
        {task.callback_phone || "Unidentified caller"} ·{" "}
        {label(task.route || "")}
      </p>
      <div className="button-row">
        {task.inbound_state !== "connected" && (
          <button
            disabled={!canAccept || accepting || accepted}
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
            Accept return call
          </button>
        )}
        <button
          className="secondary"
          disabled={!audioActive}
          aria-label={muted ? "Unmute return call" : "Mute return call"}
          onClick={() => {
            mute();
            setMuted(!muted);
          }}
        >
          {muted ? "Unmute" : "Mute"}
        </button>
        <button
          className="danger-button"
          onClick={() =>
            void run(async () => {
              try {
                await api(`/v1/callbacks/${task.id}/hangup`, "POST");
              } finally {
                await localHangup();
                await refresh();
              }
            })
          }
        >
          End return call
        </button>
      </div>
      {task.inbound_state !== "connected" && !canAccept && !accepted && (
        <p role="status">
          Waiting for the matching incoming audio. You can still end this return
          call.
        </p>
      )}
      {accepted && task.inbound_state !== "connected" && (
        <p role="status">Accepted. Waiting for carrier connection.</p>
      )}
      {task.review_required && (
        <p role="status">Routing needs review. The follow-up remains saved.</p>
      )}
    </section>
  );
}
