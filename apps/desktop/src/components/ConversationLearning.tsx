import { useCallback, useEffect, useState } from "react";
import { date, label, percent, useWorkspace } from "../context";

interface Alias {
  field: "concept" | "approach";
  source: string;
  target: string;
}
interface ConversationReport {
  vocabulary: { version: number; aliases: Alias[] };
  report: {
    status: string;
    as_of?: string;
    samples?: number;
    limitations?: string;
    patterns: {
      concept: string;
      approach: string | null;
      samples: number;
      conversion_low: number;
      conversion_high: number;
      status: string;
      reason: string;
      validation: { exposed: number; reference: number };
    }[];
  };
}

export function ConversationLearning() {
  const { api, run, subscribe, snapshot, notice } = useWorkspace();
  const [data, setData] = useState<ConversationReport | null>(null);
  const [field, setField] = useState<Alias["field"]>("concept");
  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  const [reason, setReason] = useState("");
  const load = useCallback(async () => {
    setData(await api<ConversationReport>("/v1/optimization/conversations"));
  }, [api]);
  useEffect(() => {
    void run(load);
    return subscribe((events) => {
      if (
        events.some((event) =>
          [
            "optimization.updated",
            "analysis.ready",
            "conversion.recorded",
          ].includes(event.type),
        )
      )
        void run(load);
    });
  }, [load, run, subscribe]);
  const admin = ["owner", "admin"].includes(snapshot.user.role);
  async function save(aliases: Alias[]) {
    if (!data) return;
    try {
      await api("/v1/admin/optimization/conversation-vocabulary", "PUT", {
        expected_version: data.vocabulary.version,
        aliases,
        reason,
      });
      notice("Reviewed concept names saved. Background learning will refresh.");
      setSource("");
      setTarget("");
      setReason("");
    } finally {
      await load();
    }
  }
  const supported =
    data?.report.patterns.filter((p) => p.status === "supported_correlation") ||
    [];
  return (
    <section className="card padded" aria-label="Conversation learning">
      <h2>Conversation learning</h2>
      <p className="muted small">
        Correlations do not prove that an approach caused a conversion. Reviewed
        conversation concepts and actual rep responses are compared with later
        outcomes; feedback clicks are separate.
      </p>
      {!supported.length && (
        <p>
          No supported conversation patterns yet. Sparse evidence keeps pooled
          estimates and the current calling policy.
        </p>
      )}
      {supported.map((pattern, index) => (
        <p key={index}>
          <strong>{pattern.concept}</strong> · {pattern.approach}
          <br />
          {pattern.samples} independent contacts; later comparison{" "}
          {pattern.validation.exposed} with this approach /{" "}
          {pattern.validation.reference} with other observed approaches.{" "}
          {pattern.reason}
        </p>
      ))}
      <p className="small muted">
        {data?.report.as_of
          ? `Updated ${date(data.report.as_of)} · ${label(data.report.status)}`
          : "Awaiting background learning refresh"}
        . No automatic policy changes.
      </p>
      {!!data?.report.patterns.length && (
        <details>
          <summary>Descriptive counts and uncertainty</summary>
          {data.report.patterns.map((pattern, index) => (
            <p key={index}>
              {pattern.concept} · {pattern.approach || "Response unobserved"} ·{" "}
              {pattern.samples} contacts · observed conversion interval{" "}
              {percent(pattern.conversion_low, "Unknown", 0)}–
              {percent(pattern.conversion_high, "Unknown", 0)}. {pattern.reason}
            </p>
          ))}
          <p className="small muted">{data.report.limitations}</p>
        </details>
      )}
      {admin && data && (
        <details>
          <summary>Review concept names</summary>
          <p className="small">
            Optional synonym corrections combine observed names. No topic list
            is required. Different meanings and negation should remain separate.
          </p>
          {data.vocabulary.aliases.map((alias, index) => (
            <p key={index}>
              {alias.source} → {alias.target} ({label(alias.field)}){" "}
              <button
                className="small secondary"
                disabled={reason.trim().length < 3}
                onClick={() =>
                  void run(() =>
                    save(data.vocabulary.aliases.filter((_, i) => i !== index)),
                  )
                }
              >
                Remove normalization
              </button>
            </p>
          ))}
          <label>
            Name type
            <select
              value={field}
              onChange={(e) => setField(e.target.value as Alias["field"])}
            >
              <option value="concept">Prospect concept</option>
              <option value="approach">Rep response approach</option>
            </select>
          </label>
          <label>
            Observed wording
            <input
              value={source}
              maxLength={160}
              onChange={(e) => setSource(e.target.value)}
            />
          </label>
          <label>
            Shared concept name
            <input
              value={target}
              maxLength={160}
              onChange={(e) => setTarget(e.target.value)}
            />
          </label>
          <label>
            Normalization review reason
            <input
              value={reason}
              maxLength={500}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          <button
            className="small secondary"
            disabled={
              !source.trim() || !target.trim() || reason.trim().length < 3
            }
            onClick={() =>
              void run(() =>
                save([
                  ...data.vocabulary.aliases,
                  { field, source: source.trim(), target: target.trim() },
                ]),
              )
            }
          >
            Save reviewed normalization
          </button>
        </details>
      )}
    </section>
  );
}
