import { useEffect, useState } from "react";
import { date, useWorkspace } from "../context";

interface Entry {
  at: string;
  kind: "call" | "carrier" | "command";
  label: string;
  detail: string;
}

/** What happened on one call, in order (R4.12). */
export function CallTimeline({ callId }: { callId: string }) {
  const { api } = useWorkspace();
  const [entries, setEntries] = useState<Entry[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let current = true;
    setEntries(null);
    setFailed(false);
    api<{ entries: Entry[] }>(
      `/v1/calls/${encodeURIComponent(callId)}/timeline`,
    )
      .then((result) => current && setEntries(result.entries))
      .catch(() => current && setFailed(true));
    return () => {
      current = false;
    };
  }, [api, callId]);
  return (
    <section className="card padded" aria-label="Call timeline">
      <h2>Timeline</h2>
      {failed && <p className="small muted">The timeline could not be read.</p>}
      {entries && (
        <ol className="timeline">
          {entries.map((entry, index) => (
            <li key={`${entry.at}-${index}`} className={entry.kind}>
              <time dateTime={entry.at}>{date(entry.at)}</time>{" "}
              <strong>{entry.label}</strong>
              {entry.detail && <span className="muted"> · {entry.detail}</span>}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
