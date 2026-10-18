import { useState } from "react";
import { date, useWorkspace } from "../context";

type Interval = {
  id: string;
  version: number;
  rep_id: string;
  started_at: string;
  ended_at: string | null;
  lease_until: string;
  correction_reason?: string;
};
const localInput = (value: string) => {
  const instant = new Date(value);
  return new Date(instant.getTime() - instant.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 19);
};

export function WorkTime() {
  const { api, run, refresh, notice, snapshot: w } = useWorkspace();
  const [rows, setRows] = useState<Interval[]>([]);
  const [selected, setSelected] = useState<Interval | null>(null);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const load = async () =>
    setRows((await api<{ intervals: Interval[] }>("/v1/rep/work")).intervals);
  return (
    <details
      className="card padded"
      onToggle={(event) => {
        if (event.currentTarget.open) void run(load);
      }}
    >
      <summary>Working time &amp; corrections</summary>
      <p>
        Working time combines declared ready time, calls and recent wrap-up.
        Breaks and disconnected gaps are excluded. Heartbeats expire after 90
        seconds; 15 minutes without interaction pauses forgotten readiness.
        Active calls continue to count while you listen.
      </p>
      <p className="fine">
        Only an activity timestamp is used in memory; typed content and
        interaction history are not collected. Overlapping devices count once.
        Review your most recent 100 intervals below; corrections are audited.
        Times use {Intl.DateTimeFormat().resolvedOptions().timeZone}.
      </p>
      <button onClick={() => void run(load)}>Refresh working intervals</button>
      {rows.length === 0 && <p>No observed working intervals yet.</p>}
      {rows.map((row) => {
        const active =
          !row.ended_at && Date.parse(row.lease_until) >= Date.now();
        return (
          <div className="task-item" key={row.id}>
            <p>
              {w.task_owners?.find((owner) => owner.id === row.rep_id)?.name ||
                row.rep_id}{" "}
              · {date(row.started_at)} →{" "}
              {row.ended_at
                ? date(row.ended_at)
                : active
                  ? "Active"
                  : date(row.lease_until)}
            </p>
            {row.correction_reason && <p>{row.correction_reason}</p>}
            <button
              disabled={active}
              onClick={() => {
                setSelected(row);
                setStart(localInput(row.started_at));
                setEnd(localInput(row.ended_at || row.lease_until));
                setReason("");
              }}
            >
              {active ? "Pause before correcting" : "Review interval"}
            </button>
          </div>
        );
      })}
      {selected && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            setBusy(true);
            void run(async () => {
              try {
                await api("/v1/rep/work/" + selected.id, "PATCH", {
                  expected_version: selected.version,
                  started_at: new Date(start).toISOString(),
                  ended_at: new Date(end).toISOString(),
                  reason,
                });
                setSelected(null);
                await load();
                await refresh();
                notice("Working time corrected");
              } finally {
                setBusy(false);
              }
            });
          }}
        >
          <div className="form-grid">
            <label>
              Work started
              <input
                type="datetime-local"
                step="1"
                required
                value={start}
                onChange={(event) => setStart(event.target.value)}
              />
            </label>
            <label>
              Work ended
              <input
                type="datetime-local"
                step="1"
                required
                value={end}
                onChange={(event) => setEnd(event.target.value)}
              />
            </label>
            <label>
              Correction reason
              <input
                required
                minLength={3}
                maxLength={500}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
          </div>
          <button type="submit" disabled={busy}>
            Save working-time correction
          </button>
          <button type="button" onClick={() => setSelected(null)}>
            Cancel correction
          </button>
        </form>
      )}
    </details>
  );
}

export function CallingHealth() {
  const {
    snapshot: { metrics: m },
  } = useWorkspace();
  const health = m.health?.connect_rate;
  const rate = (value: number | null | undefined) =>
    value == null ? "Unknown" : `${(value * 100).toFixed(1)}%`;
  return (
    <section className="card padded" aria-label="Calling health">
      <h3>Calling health</h3>
      {m.warnings?.map((warning) => (
        <p role="status" key={warning}>
          {warning}
        </p>
      ))}
      <p>
        {health?.status === "insufficient_data"
          ? "Not enough classified calls for a comparison."
          : `Human pickup ${rate(health?.rate)} · Previous period ${rate(health?.reference_rate)}.`}{" "}
        {health?.samples ?? 0} classified · {health?.unknown ?? 0} unclassified
        in the last 24 hours.
      </p>
      <p>
        {m.rep_work_seconds == null
          ? "Working time not observed"
          : `${(m.rep_work_seconds / 60).toFixed(1)} min working time`}{" "}
        ·{" "}
        {m.conversions_per_rep_hour == null
          ? "Conversions per working hour unavailable"
          : `${m.conversions_per_rep_hour.toFixed(2)} conversions per working hour`}
        .
      </p>
      <details>
        <summary>How these measurements work</summary>
        <p>{health?.comparison}</p>
        <p>
          Recent human pickup interval: {rate(health?.confidence_low)}–
          {rate(health?.confidence_high)}. All-attempt pickup:{" "}
          {rate(health?.all_attempt_rate)}. Unclassified calls remain visible.
        </p>
        <p>
          {m.work_time?.note} Review and correct working intervals in Settings.
        </p>
        <p>
          Goal: {m.outcome_goal?.replaceAll("_", " ")} · {m.outcome_window_days}
          -day outcome window.
        </p>
      </details>
    </section>
  );
}
