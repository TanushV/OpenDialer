import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { native } from "../api/client";
import { label, useWorkspace } from "../context";
import { useLoad } from "../state/useLoad";

interface Report {
  answered: number;
  placed: number;
  window_days: number;
  stages: { stage: string; calls: number; outcomes: Record<string, number> }[];
}

/** D31, R11.6: where answered calls end in the script, by outcome; the one learning view kept after StatML. */
export function DropOff() {
  const { data } = useLoad<Report>("/v1/review/drop-off");
  const { api, run, notice, snapshot } = useWorkspace();
  const [exporting, setExporting] = useState(false);
  const [includeTest, setIncludeTest] = useState(false);
  async function download() {
    setExporting(true);
    try {
      const { archive } = await api<{ archive: string }>(
        `/v1/review/call-statistics/export?include_test=${includeTest || snapshot.simulation}`,
      );
      const bytes = Uint8Array.from(atob(archive), (character) =>
        character.charCodeAt(0),
      );
      if (native()) {
        const path = await invoke<string>("save_call_statistics", bytes);
        notice(`Call statistics saved to ${path}`);
        return;
      }
      const url = URL.createObjectURL(
        new Blob([bytes], { type: "application/zip" }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = "opendialer-call-statistics.zip";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } finally {
      setExporting(false);
    }
  }
  if (!data) return null;
  return (
    <section className="card padded" aria-label="Where calls end">
      <h2>Where calls end</h2>
      <p className="fine">
        Answered calls in the last {data.window_days} days: {data.answered}.{" "}
        {data.placed === data.answered
          ? "Each shows the script part it ended in."
          : `${data.placed} show the script part they ended in. The rest could not be placed.`}
      </p>
      {data.placed ? (
        <table>
          <thead>
            <tr>
              <th>Script part</th>
              <th>Calls ended here</th>
              <th>Outcomes</th>
            </tr>
          </thead>
          <tbody>
            {data.stages.map((row) => (
              <tr key={row.stage}>
                <td>{row.stage}</td>
                <td>{row.calls}</td>
                <td>
                  {Object.entries(row.outcomes)
                    .sort((a, b) => b[1] - a[1])
                    .map(([outcome, n]) => `${n} ${label(outcome)}`)
                    .join(" · ") || "None"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">Shows once answered calls have a transcript.</p>
      )}
      <div className="button-row">
        <button
          className="secondary"
          disabled={exporting}
          onClick={() => void run(download)}
        >
          {exporting ? "Exporting statistics…" : "Export call statistics"}
        </button>
        {!snapshot.simulation && (
          <label>
            <input
              type="checkbox"
              checked={includeTest}
              onChange={(event) => setIncludeTest(event.target.checked)}
            />{" "}
            Include test calls
          </label>
        )}
      </div>
    </section>
  );
}
