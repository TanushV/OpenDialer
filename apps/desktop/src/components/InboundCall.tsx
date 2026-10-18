import { useState } from "react";
import { label, useWorkspace } from "../context";
import type { FollowupTask } from "../types";

export function InboundCall({
  task,
  mute,
  localHangup,
}: {
  task: FollowupTask;
  mute: () => void;
  localHangup: () => Promise<void>;
}) {
  const { api, run, refresh } = useWorkspace();
  const [muted, setMuted] = useState(false);
  return (
    <section className="card padded" aria-label="Active return call">
      <h2>Return call · {label(task.inbound_state || "routing")}</h2>
      <p>
        {task.callback_phone || "Unidentified caller"} ·{" "}
        {label(task.route || "")}
      </p>
      <div className="button-row">
        <button
          className="secondary"
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
      {task.review_required && (
        <p role="status">Routing needs review. The follow-up remains saved.</p>
      )}
    </section>
  );
}
