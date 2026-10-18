import { useState } from "react";
import { useHistory } from "../state/useHistory";
import { HistoryMore } from "./HistoryMore";
import { date, label, useWorkspace } from "../context";
import type { FollowupTask } from "../types";

export function localDateInput(value?: string | null) {
  if (!value) return "";
  const d = new Date(value);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 16);
}

function Task({
  task,
  owners,
  onSaved,
}: {
  task: FollowupTask;
  owners: { id: string; name: string }[];
  onSaved: (task: FollowupTask) => void;
}) {
  const { snapshot: w, api, run, refresh, notice } = useWorkspace();
  const [when, setWhen] = useState(localDateInput(task.due_at));
  const [owner, setOwner] = useState(task.owner_id || "");
  const routing = task.inbound && task.inbound_state !== "ended";
  const closed = ["completed", "cancelled"].includes(task.status) || routing;
  const name =
    task.lead_name ||
    w.leads.find((l) => l.id === task.lead_id)?.name ||
    "Incoming callback";
  function change(edits: Record<string, unknown>) {
    return run(async () => {
      try {
        const saved = await api<FollowupTask>(`/v1/tasks/${task.id}`, "PATCH", {
          expected_version: task.version,
          ...edits,
        });
        onSaved(saved);
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
      {task.inbound && (
        <p>
          Return call: {label(task.inbound_state || "unknown")} ·{" "}
          {label(task.route || "unrouted")}.{" "}
          {task.callback_phone || "Caller identity unavailable"}
        </p>
      )}
      {routing && (
        <p>
          Routing is still active. Call controls are in the calling workspace.
        </p>
      )}
      {task.recovery_reason && <p role="status">{task.recovery_reason}</p>}
      {task.time_window && !task.due_at && (
        <p>
          Requested window:{" "}
          {[
            task.time_window.date,
            task.time_window.period || task.time_window.local_time,
            task.time_window.timezone,
          ]
            .filter(Boolean)
            .join(" · ")}
        </p>
      )}
      {task.source_stale && task.review_required && (
        <p role="status">
          Source corrected. Confirm this follow-up; your edits are preserved.
        </p>
      )}
      {task.source_retracted && task.review_required && (
        <p role="status">
          The latest analysis no longer supports this promise. Review your next
          action.
        </p>
      )}
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
          {owners.map((o) => (
            <option key={o.id} value={o.id}>
              {o.name}
            </option>
          ))}
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
  const { snapshot: w, run } = useWorkspace();
  const history = useHistory("tasks", w.tasks);
  const owners = useHistory(
    "task_owners",
    w.task_owners || [{ id: w.user.id, name: w.user.name }],
  );
  const sorted = [...history.items].sort(
    (a, b) =>
      Number(["completed", "cancelled"].includes(a.status)) -
        Number(["completed", "cancelled"].includes(b.status)) ||
      (a.due_at || "").localeCompare(b.due_at || ""),
  );
  return (
    <>
      <label>
        Follow-up history
        <select
          value={history.filter}
          onChange={(event) =>
            void run(() => history.load(false, "", event.target.value))
          }
        >
          <option value="all">All follow-ups</option>
          <option value="open">Open follow-ups</option>
          <option value="overdue">Overdue follow-ups</option>
          <option value="closed">Completed or cancelled</option>
        </select>
      </label>
      {sorted.length ? (
        sorted.map((t) => (
          <Task
            key={`${t.id}:${t.version}`}
            task={t}
            owners={owners.items}
            onSaved={history.update}
          />
        ))
      ) : (
        <p className="muted">
          Agreed callbacks will appear here, ready to confirm or reschedule.
        </p>
      )}
      <HistoryMore history={history} label="Load more follow-ups" />
      <HistoryMore history={owners} label="Load more task owners" />
    </>
  );
}
