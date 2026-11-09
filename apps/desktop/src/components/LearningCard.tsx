import { useEffect, useState } from "react";
import { date, label, useWorkspace } from "../context";
import { Panel } from "../ui/kit";

interface Ranked {
  value: string | number;
  calls: number;
  rate: number;
}
interface Summary {
  calls_used: number;
  required_calls: number;
  learning: boolean;
  best_hours: Ranked[];
  best_triggers: Ranked[];
  wrong_party_by_number_source: Ranked[];
  wrong_party_by_number_status: Ranked[];
  model: { live: boolean; promoted_at: string | null };
  last_change: { at: string; kind: string } | null;
}

const percent = (rate: number) => `${Math.round(rate * 100)}%`;
const hour = (value: string | number) => `${value}:00`;

/** What StatML has learned so far (R8.8): from real calls only, with small groups left out. */
export function LearningCard() {
  const { api } = useWorkspace();
  const [data, setData] = useState<Summary | null>(null);
  useEffect(() => {
    void api<Summary>("/v1/learning/summary")
      .then(setData)
      .catch(() => setData(null));
  }, [api]);
  if (!data) return null;
  const list = (rows: Ranked[], show: (r: Ranked) => string) =>
    rows.length ? rows.map(show).join(", ") : "Not enough calls yet";
  return (
    <Panel title="What it has learned" aria-label="What it has learned">
      <p>
        {data.calls_used} {data.calls_used === 1 ? "call" : "calls"} so far.{" "}
        {data.learning
          ? "It is training on them and switches to the learned model only once it beats the starting estimates."
          : `Best hours show from 3 calls in an hour; a learned model is tried from ${data.required_calls} calls (${Math.max(0, data.required_calls - data.calls_used)} to go).`}
      </p>
      <dl className="learning-facts">
        <dt>Best hours to reach someone</dt>
        <dd>
          {list(
            data.best_hours,
            (r) => `${hour(r.value)} (${percent(r.rate)} of ${r.calls})`,
          )}
        </dd>
        <dt>Triggers that lead to your goal</dt>
        <dd>
          {list(
            data.best_triggers,
            (r) => `${label(String(r.value))} (${percent(r.rate)})`,
          )}
        </dd>
        <dt>Wrong person by number source</dt>
        <dd>
          {list(
            data.wrong_party_by_number_source,
            (r) => `${r.value}: ${percent(r.rate)} of ${r.calls}`,
          )}
        </dd>
        <dt>Wrong person by number check</dt>
        <dd>
          {list(
            data.wrong_party_by_number_status,
            (r) =>
              `${label(String(r.value))}: ${percent(r.rate)} of ${r.calls}`,
          )}
        </dd>
        <dt>Model in use</dt>
        <dd>
          {data.model.live && data.model.promoted_at
            ? `Learned model, live since ${date(data.model.promoted_at)}`
            : "Starting estimates; no learned model yet"}
          {data.last_change &&
            ` · last change: ${data.last_change.kind} ${date(data.last_change.at)}`}
        </dd>
      </dl>
    </Panel>
  );
}
