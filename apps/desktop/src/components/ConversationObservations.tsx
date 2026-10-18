import { useState } from "react";
import { date, label, useWorkspace } from "../context";
import type { Analysis } from "../types";

export function ConversationObservations({
  analysis,
  reload,
}: {
  analysis: Analysis;
  reload: () => Promise<void>;
}) {
  const { api, run } = useWorkspace();
  const [reasons, setReasons] = useState<Record<string, string>>({});
  return (
    <section className="card padded" aria-label="Conversation observations">
      <h2>Conversation observations</h2>
      <p className="small muted">
        Paraphrased concepts and responses, separate from confirmed outcomes.
        Original speech expires within five minutes. Correct an interpretation
        before relying on it for learning.
      </p>
      {!analysis.observations?.length && (
        <p>No reviewed observations are available.</p>
      )}
      {analysis.observations?.map((item) => (
        <div key={item.id}>
          <p>
            <strong>{item.concept}</strong>
            {item.approach && ` · ${item.approach}`}
            <br />
            {label(item.status)} · {label(item.review_status)} ·{" "}
            {date(item.observed_at)}
          </p>
          <label>
            Observation review reason
            <input
              aria-label="Observation review reason"
              value={reasons[item.id] || ""}
              maxLength={500}
              onChange={(e) =>
                setReasons({ ...reasons, [item.id]: e.target.value })
              }
            />
          </label>
          {(["confirm", "retract"] as const).map((decision) => (
            <button
              key={decision}
              className="small secondary"
              disabled={(reasons[item.id] || "").trim().length < 3}
              onClick={() =>
                void run(async () => {
                  try {
                    await api(
                      `/v1/calls/${analysis.call.id}/observations/${item.id}/review`,
                      "POST",
                      {
                        expected_version: item.version,
                        decision,
                        reason: reasons[item.id],
                      },
                    );
                  } finally {
                    await reload();
                  }
                })
              }
            >
              {decision === "confirm"
                ? "Confirm observation"
                : "Retract observation"}
            </button>
          ))}
        </div>
      ))}
    </section>
  );
}
