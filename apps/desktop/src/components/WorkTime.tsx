import { useState } from "react";
import { date, percent, useWorkspace } from "../context";

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
    refresh,
    run,
  } = useWorkspace();
  if (m.projection?.status === "pending")
    return (
      <section className="card padded" aria-label="Calling health">
        <p role="status">
          {m.projection.refresh_error ||
            "Preparing workspace totals in the background."}
        </p>
        <button onClick={() => void run(refresh)}>
          Check workspace totals
        </button>
      </section>
    );
  const health = m.health?.connect_rate;
  const rate = (value: number | null | undefined) => percent(value);
  return (
    <section className="card padded" aria-label="Calling health">
      <h3>Calling health</h3>
      {m.projection && (
        <p role="status">
          Totals as of {date(m.projection.as_of)}
          {m.projection.status === "stale"
            ? " · Updating in the background; these are the previous totals."
            : ""}
        </p>
      )}
      {m.projection?.refresh_error && (
        <p role="status">{m.projection.refresh_error}</p>
      )}
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

const DELIVERY_COLUMNS = [
  ["attempts", "Attempts"],
  ["network_answers", "Network answers"],
  ["human_pickups", "Human pickups"],
  ["connects", "Two-way connects"],
  ["intended_prospect", "Intended prospect"],
  ["screened", "Screened"],
  ["screened_to_human", "Screened to human"],
  ["voicemail", "Voicemail"],
  ["ivr", "IVR"],
  ["carrier_blocks", "Carrier blocks"],
  ["failures", "Failed"],
  ["no_answer", "No answer"],
  ["ring_timeouts", "Ring timeouts"],
  ["conversations", "Conversations"],
  ["inbound_callbacks", "Callbacks"],
  ["audio_problems", "Audio problems"],
  ["audio_unconfirmed", "Audio unconfirmed"],
] as const;
type Delivery = Partial<
  Record<(typeof DELIVERY_COLUMNS)[number][0], number>
> & {
  key?: string;
  connect_rate: number | null;
  short_answered_rate: number | null;
  pre_answer_drop_rate: number | null;
  answer_to_bridge_p95_seconds: number | null;
  answer_to_bridge_samples: number;
  warnings: string[];
};
const GROUPS = [
  ["daily", "Company by day"],
  ["weekly", "Company by week"],
  ["by_number", "28 days by caller number"],
  ["by_carrier", "28 days by receiving carrier"],
  ["by_number_and_carrier", "28 days by number and carrier"],
] as const;
type DeliveryReport = {
  simulation: boolean;
  overall: Delivery;
  rolling_28_days: Delivery;
} & Record<(typeof GROUPS)[number][0], Delivery[]>;

function DeliveryTable({ name, rows }: { name: string; rows: Delivery[] }) {
  return (
    <div className="table-scroll">
      <table aria-label={name}>
        <thead>
          <tr>
            <th>Group</th>
            <th>Connect rate</th>
            {DELIVERY_COLUMNS.map(([key, title]) => (
              <th key={key}>{title}</th>
            ))}
            <th>Short calls</th>
            <th>Pre-answer drops</th>
            <th>Answer-to-bridge p95</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key}>
              <th scope="row">{row.key}</th>
              <td>{percent(row.connect_rate)}</td>
              {DELIVERY_COLUMNS.map(([key]) => (
                <td key={key}>{row[key] ?? 0}</td>
              ))}
              <td>{percent(row.short_answered_rate)}</td>
              <td>{percent(row.pre_answer_drop_rate)}</td>
              <td>
                {row.answer_to_bridge_p95_seconds == null
                  ? "Unmeasured"
                  : `${Math.round(row.answer_to_bridge_p95_seconds * 1000)} ms (${row.answer_to_bridge_samples})`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ConnectDelivery() {
  const { api, run } = useWorkspace();
  const [report, setReport] = useState<DeliveryReport | null>(null);
  const load = async () =>
    setReport(await api<DeliveryReport>("/v1/admin/connect-report?days=56"));
  return (
    <details
      className="card padded"
      onToggle={(event) => {
        if (event.currentTarget.open && !report) void run(load);
      }}
    >
      <summary>Live connect report</summary>
      <p>
        Two-way connects divide rep-confirmed two-way live pickups by all
        initiated attempts. Test calls marked in wrap-up are excluded. Bridge
        time is a carrier timing proxy. Days and weeks use Pacific time.
      </p>
      <button onClick={() => void run(load)}>Refresh connect report</button>
      {report && (
        <>
          {report.simulation && <p>Simulation data</p>}
          {report.rolling_28_days.warnings.map((warning) => (
            <p role="status" key={warning}>
              {warning}
            </p>
          ))}
          <DeliveryTable
            name="Connect totals"
            rows={[
              { ...report.overall, key: "Last 56 days" },
              { ...report.rolling_28_days, key: "Rolling 28 days" },
            ]}
          />
          {GROUPS.map(([key, title]) => (
            <details key={key}>
              <summary>{title}</summary>
              {report[key].length ? (
                <DeliveryTable name={title} rows={report[key]} />
              ) : (
                <p>No attempts yet.</p>
              )}
            </details>
          ))}
        </>
      )}
    </details>
  );
}
