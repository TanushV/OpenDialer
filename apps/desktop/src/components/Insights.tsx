import { invoke } from "@tauri-apps/api/core";
import { useEffect, useState } from "react";
import { native } from "../api/client";
import { useWorkspace } from "../context";

interface Rate {
  count: number;
  of: number;
  rate: number | null;
  range: [number, number] | null;
}
interface Group {
  group: string;
  dials: number;
  conversations: Rate;
  past_opener: Rate;
  reached_pitch: Rate;
  meetings: Rate;
  enough: boolean;
}
interface Comparison {
  by: string;
  groups: Group[];
  enough_at: number;
}
interface ScriptTest extends Comparison {
  test: { id: string; when: string[] } | null;
  arms?: {
    variant: string;
    share: number;
    hypothesis: string;
    results: Group | null;
  }[];
}

const DIMENSIONS: [string, string][] = [
  ["variant", "Script variant"],
  ["path", "Path"],
  ["band", "Local time band"],
  ["list", "List"],
  ["trigger", "Trigger"],
  ["talk_share", "Your talk share"],
  ["longest_monologue", "Your longest monologue"],
  ["response_gap", "Your response gap"],
];

function share(value: Rate) {
  if (!value.of) return "–";
  const main = `${Math.round((value.rate ?? 0) * 100)}%`;
  return value.range
    ? `${main} (${Math.round(value.range[0] * 100)}–${Math.round(value.range[1] * 100)}%)`
    : main;
}

function Row({ name, group }: { name: string; group: Group | null }) {
  if (!group)
    return (
      <tr>
        <td>{name}</td>
        <td colSpan={5} className="muted">
          No calls yet
        </td>
      </tr>
    );
  return (
    <tr>
      <td>
        {name}
        {!group.enough && <span className="fine"> · not enough data yet</span>}
      </td>
      <td>{group.dials}</td>
      <td>{share(group.conversations)}</td>
      <td>{share(group.past_opener)}</td>
      <td>{share(group.reached_pitch)}</td>
      <td>{share(group.meetings)}</td>
    </tr>
  );
}

function Table({ children }: { children: React.ReactNode }) {
  return (
    <table className="insights-table">
      <thead>
        <tr>
          <th>Group</th>
          <th>Dials</th>
          <th>Conversations</th>
          <th>Past the opener</th>
          <th>Reached the pitch</th>
          <th>Meetings</th>
        </tr>
      </thead>
      <tbody>{children}</tbody>
    </table>
  );
}

/**
 * Call insights plan phase 7: the script test with each variant's hypothesis, results by one dimension at a time
 * with 95% ranges, and the export of calls with their audio and transcripts.
 */
export function Insights() {
  const { api, run, notice } = useWorkspace();
  const [test, setTest] = useState<ScriptTest | null>(null);
  const [by, setBy] = useState("band");
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [filters, setFilters] = useState({
    since: "",
    list_name: "",
    conversations_only: false,
    include_audio: true,
  });
  const [exporting, setExporting] = useState(false);
  useEffect(() => {
    void api<ScriptTest>("/v1/review/variants")
      .then(setTest)
      .catch(() => undefined);
  }, [api]);
  useEffect(() => {
    void api<Comparison>(`/v1/review/compare?by=${by}`)
      .then(setComparison)
      .catch(() => undefined);
  }, [api, by]);
  async function exportCalls() {
    setExporting(true);
    try {
      const result = await api<{ path: string; calls: number; audio: number }>(
        "/v1/review/export",
        "POST",
        {
          ...filters,
          since: filters.since ? `${filters.since}T00:00:00` : null,
          list_name: filters.list_name || null,
        },
        { timeoutMs: 300_000 },
      );
      notice(
        `Exported ${result.calls} calls (${result.audio} with audio) to ${result.path}`,
      );
      if (native())
        await invoke("reveal_export", { path: result.path }).catch(
          () => undefined,
        );
    } finally {
      setExporting(false);
    }
  }
  return (
    <section className="card padded" aria-label="Insights">
      <h2>Insights</h2>
      {test?.test && test.arms && (
        <div aria-label="Script test">
          <h3>Script test</h3>
          {test.test.when.length > 0 && (
            <p className="fine">
              Counts only leads with these triggers: {test.test.when.join(", ")}
              .
            </p>
          )}
          <Table>
            {test.arms.map((arm) => (
              <Row
                key={arm.variant}
                name={`${arm.variant} (${arm.share}%)`}
                group={arm.results}
              />
            ))}
          </Table>
          <ul className="fine">
            {test.arms.map((arm) => (
              <li key={arm.variant}>
                <strong>{arm.variant}:</strong> {arm.hypothesis}
              </li>
            ))}
          </ul>
        </div>
      )}
      <label className="inline-label">
        Compare by
        <select
          aria-label="Compare by"
          value={by}
          onChange={(event) => setBy(event.target.value)}
        >
          {DIMENSIONS.map(([value, name]) => (
            <option key={value} value={value}>
              {name}
            </option>
          ))}
        </select>
      </label>
      {comparison && comparison.groups.length > 0 ? (
        <Table>
          {comparison.groups.map((group) => (
            <Row key={group.group} name={group.group} group={group} />
          ))}
        </Table>
      ) : (
        <p className="fine">No finished calls to compare yet.</p>
      )}
      <p className="fine">
        Ranges are 95%. A difference needs at least{" "}
        {comparison?.enough_at ?? 30} conversations in each group before it
        means much.
      </p>
      <details>
        <summary>Export calls with audio and transcripts</summary>
        <div className="row">
          <label>
            From
            <input
              type="date"
              value={filters.since}
              onChange={(event) =>
                setFilters({ ...filters, since: event.target.value })
              }
            />
          </label>
          <label>
            List
            <input
              aria-label="List"
              placeholder="All lists"
              value={filters.list_name}
              onChange={(event) =>
                setFilters({ ...filters, list_name: event.target.value })
              }
            />
          </label>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={filters.conversations_only}
              onChange={(event) =>
                setFilters({
                  ...filters,
                  conversations_only: event.target.checked,
                })
              }
            />{" "}
            Conversations only
          </label>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={filters.include_audio}
              onChange={(event) =>
                setFilters({ ...filters, include_audio: event.target.checked })
              }
            />{" "}
            Include audio
          </label>
        </div>
        <p className="fine">
          A ZIP with a calls spreadsheet and, per call, its audio (left: you,
          right: the prospect) and transcript timed to the audio. It holds
          prospects' personal data.
        </p>
        <button
          className="secondary"
          disabled={exporting}
          onClick={() => void run(exportCalls)}
        >
          {exporting ? "Exporting…" : "Export calls"}
        </button>
      </details>
    </section>
  );
}
