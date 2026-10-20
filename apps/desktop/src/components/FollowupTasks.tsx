import { useState } from "react";
import { useHistory } from "../state/useHistory";
import { HistoryMore } from "./HistoryMore";
import { date, label, useWorkspace } from "../context";
import type { FollowupTask } from "../types";
import { localEdit } from "../api/client";
import {
  chooseZone,
  zoneCaption,
  zonedInput,
  zonedInstant,
} from "../state/zones";

const CLOSED = ["completed", "cancelled"];
/** A task keeps its own zone; otherwise the lead's explicit zone applies. */
function taskZone(task: FollowupTask, leadZone?: string | null) {
  return chooseZone(task.timezone, task.time_window?.timezone, leadZone);
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
  const lead = w.leads.find((l) => l.id === task.lead_id);
  const zone = taskZone(task, lead?.timezone);
  const [when, setWhen] = useState(zonedInput(task.due_at, zone.zone));
  const [owner, setOwner] = useState(task.owner_id || "");
  const [reminder, setReminder] = useState(
    zonedInput(task.snoozed_until, zone.zone),
  );
  const [reason, setReason] = useState("");
  const routing = task.inbound && task.inbound_state !== "ended";
  const closed = CLOSED.includes(task.status) || routing;
  const name = task.lead_name || lead?.name || "Incoming callback";
  const confirmation = task.review_required
    ? { confirmation_reason: reason }
    : {};
  /** Edits are built inside run() so an invalid local time reports an error. */
  function change(edits: () => Record<string, unknown>) {
    return run(async () => {
      const values = edits();
      try {
        const saved = await localEdit<FollowupTask>(
          api,
          `/v1/tasks/${task.id}`,
          "PATCH",
          {
            request_id: crypto.randomUUID(),
            expected_version: task.version,
            ...values,
          },
        );
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
        <span>{label(task.status)}</span> · {date(task.due_at, zone.zone)}
      </p>
      {task.snoozed_until && (
        <p>
          Reminder snoozed until {date(task.snoozed_until, zone.zone)}. The
          promised callback time is unchanged.
        </p>
      )}
      {task.source_conflicts?.length ? (
        <p role="status">
          Conflicting follow-ups need a decision. Review both records before
          confirming or cancelling.
        </p>
      ) : null}
      {task.retained_notes?.map((note) => (
        <p key={note.task_id}>Retained rep note: {note.text}</p>
      ))}
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
      {task.review_required && (
        <label>
          Confirmation reason
          <input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
      )}
      <label>
        Callback time ({zoneCaption(zone)})
        <input
          type="datetime-local"
          value={when}
          onChange={(e) => setWhen(e.target.value)}
          disabled={closed}
        />
      </label>
      <label>
        Reminder until ({zoneCaption(zone)})
        <input
          type="datetime-local"
          value={reminder}
          onChange={(event) => setReminder(event.target.value)}
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
          disabled={
            closed ||
            !when ||
            !owner ||
            (!!task.review_required && reason.trim().length < 3)
          }
          onClick={() =>
            void change(() => ({
              status: "open",
              confirmation_state: "confirmed",
              ...confirmation,
              due_at: zonedInstant(when, zone.zone),
              owner_id: owner,
              timezone: zone.zone,
            }))
          }
        >
          Save follow-up
        </button>
        <button
          className="small secondary"
          disabled={closed || !task.due_at || !reminder}
          onClick={() =>
            void change(() => ({
              status: "snoozed",
              snoozed_until: zonedInstant(reminder, zone.zone),
            }))
          }
        >
          Snooze reminder
        </button>
        <button
          className="small text-button"
          disabled={
            closed || (!!task.review_required && reason.trim().length < 3)
          }
          onClick={() =>
            void change(() => ({ status: "completed", ...confirmation }))
          }
        >
          Complete
        </button>
        <button
          className="small text-button"
          disabled={
            closed || (!!task.review_required && reason.trim().length < 3)
          }
          onClick={() =>
            void change(() => ({ status: "cancelled", ...confirmation }))
          }
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
      Number(CLOSED.includes(a.status)) - Number(CLOSED.includes(b.status)) ||
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
          // Keyed by identity: a refreshed version must not discard an open draft.
          <Task
            key={t.id}
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

/** Due and overdue callbacks, shown as the rep's next work on the dialer. */
export function DueTasks() {
  const { snapshot: w } = useWorkspace();
  const now = Date.now();
  const due = w.tasks
    .filter(
      (t) =>
        !t.inbound &&
        !CLOSED.includes(t.status) &&
        (t.owner_id ?? w.user.id) === w.user.id &&
        !!t.due_at &&
        Date.parse(t.due_at) <= now + 24 * 3600_000 &&
        !(t.snoozed_until && Date.parse(t.snoozed_until) > now),
    )
    .sort((a, b) => a.due_at!.localeCompare(b.due_at!));
  if (!due.length) return null;
  return (
    <section className="card padded next-work" aria-label="Next work">
      <h3>Next work</h3>
      <ul>
        {due.slice(0, 5).map((t) => {
          const lead = w.leads.find((l) => l.id === t.lead_id);
          const zone = taskZone(t, lead?.timezone);
          return (
            <li key={t.id}>
              <strong>{t.lead_name || lead?.name || "Lead"}</strong> ·{" "}
              {t.action} · {date(t.due_at, zone.zone)} (
              {zone.zone.replaceAll("_", " ")})
              {Date.parse(t.due_at!) <= now && (
                <span className="pill subtle">Overdue</span>
              )}
            </li>
          );
        })}
      </ul>
      <p className="fine">
        Due callbacks come first when you get the next lead and calling policy
        permits.
        {due.length > 5 ? ` ${due.length - 5} more in Intelligence.` : ""}
      </p>
    </section>
  );
}
