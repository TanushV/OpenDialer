import { useState } from "react";
import { date, label, useWorkspace } from "../context";
import type { FollowupTask } from "../types";

export function localDateInput(value?: string | null) {
  if (!value) return "";
  const d = new Date(value);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 16);
}

function Task({ task }: { task: FollowupTask }) {
  const { snapshot: w, api, run, refresh, notice } = useWorkspace();
  const [when, setWhen] = useState(localDateInput(task.due_at));
  const [owner, setOwner] = useState(task.owner_id || "");
  const closed = ["completed", "cancelled"].includes(task.status);
  const name =
    w.leads.find((l) => l.id === task.lead_id)?.name || "Incoming callback";
  function change(edits: Record<string, unknown>) {
    return run(async () => {
      try {
        await api(`/v1/tasks/${task.id}`, "PATCH", {
          expected_version: task.version,
          ...edits,
        });
        notice("Follow-up saved.");
      } finally {
        await refresh();
      }
    });
  }
  return (
    <article className="followup-task" data-call-id={task.call_id}>
      <strong>
        {name} · {task.action}
      </strong>
      <p>
        <span>{label(task.status)}</span> · {date(task.due_at)}{" "}
        {task.timezone ? `(${task.timezone})` : ""}
      </p>
      <label>
        Callback time (your device timezone)
        <input
          type="datetime-local"
          value={when}
          onChange={(e) => setWhen(e.target.value)}
          disabled={closed}
        />
      </label>
      <label>
        Task owner
        <select
          value={owner}
          onChange={(e) => setOwner(e.target.value)}
          disabled={closed}
        >
          <option value="">Choose an owner</option>
          {(w.task_owners || [{ id: w.user.id, name: w.user.name }]).map(
            (o) => (
              <option key={o.id} value={o.id}>
                {o.name}
              </option>
            ),
          )}
        </select>
      </label>
      <div className="button-row">
        <button
          className="small secondary"
          disabled={closed || !when || !owner}
          onClick={() =>
            void change({
              status: "open",
              due_at: new Date(when).toISOString(),
              owner_id: owner,
              timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            })
          }
        >
          Save follow-up
        </button>
        <button
          className="small secondary"
          disabled={closed || !when}
          onClick={() =>
            void change({
              status: "snoozed",
              due_at: new Date(when).toISOString(),
              timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            })
          }
        >
          Snooze to this time
        </button>
        <button
          className="small text-button"
          disabled={closed}
          onClick={() => void change({ status: "completed" })}
        >
          Complete
        </button>
        <button
          className="small text-button"
          disabled={closed}
          onClick={() => void change({ status: "cancelled" })}
        >
          Cancel
        </button>
      </div>
    </article>
  );
}

export function FollowupTasks() {
  const { snapshot: w } = useWorkspace();
  const sorted = [...w.tasks].sort(
    (a, b) =>
      Number(["completed", "cancelled"].includes(a.status)) -
        Number(["completed", "cancelled"].includes(b.status)) ||
      (a.due_at || "").localeCompare(b.due_at || ""),
  );
  return (
    <>
      {sorted.length ? (
        sorted.map((t) => <Task key={`${t.id}:${t.version}`} task={t} />)
      ) : (
        <p className="muted">
          Agreed callbacks will appear here, ready to confirm or reschedule.
        </p>
      )}
    </>
  );
}
