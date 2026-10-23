import { useEffect, useState } from "react";
import { label, useWorkspace } from "../context";
import { useLoad } from "../state/useLoad";
import type { Analysis } from "../types";

const callTypes = [
  "cold_introduction",
  "qualification",
  "discovery",
  "follow_up",
] as const;
type CallType = (typeof callTypes)[number];
interface Dimension {
  id: string;
  title: string;
  weight: number;
  anchors: Record<"0" | "1" | "2", string>;
}
interface Rubric {
  title: string;
  purpose: string;
  dimensions: Dimension[];
}
interface Configuration {
  campaign_id: string;
  expected_version: number;
  default_call_type: CallType;
  rubrics: Record<CallType, Rubric>;
}

export function RubricSettings() {
  const { api, snapshot, run } = useWorkspace();
  const {
    data: config,
    setData: setConfig,
    error,
  } = useLoad<Configuration>(
    `/v1/admin/rubrics?campaign_id=${encodeURIComponent(snapshot.campaign.id)}`,
  );
  const [selected, setSelected] = useState<CallType>("cold_introduction");
  const [saved, setStatus] = useState("");
  const status = saved || error;
  const [busy, setBusy] = useState(false);
  const rubric = config?.rubrics[selected];
  function change(patch: Partial<Rubric>) {
    if (config && rubric)
      setConfig({
        ...config,
        rubrics: { ...config.rubrics, [selected]: { ...rubric, ...patch } },
      });
    setStatus("");
  }
  function changeDimension(index: number, patch: Partial<Dimension>) {
    if (rubric)
      change({
        dimensions: rubric.dimensions.map((item, i) =>
          i === index ? { ...item, ...patch } : item,
        ),
      });
  }
  return (
    <section className="card padded" aria-label="Call scoring rubrics">
      <h2>Call scoring rubrics</h2>
      <p className="muted small">
        Define observable opportunities for each call purpose. These anchors
        guide review; the live helper still writes freely. Existing calls retain
        their original version until a rep explicitly corrects the purpose.
      </p>
      {status && <p role="status">{status}</p>}
      {!config || !rubric ? (
        <p>Scoring settings are unavailable until loaded.</p>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            setBusy(true);
            void run(async () => {
              try {
                setConfig(
                  await api<Configuration>("/v1/admin/rubrics", "PUT", config),
                );
                setStatus("Rubrics saved");
              } finally {
                setBusy(false);
              }
            });
          }}
        >
          <label>
            Default call purpose
            <select
              value={config.default_call_type}
              onChange={(e) =>
                setConfig({
                  ...config,
                  default_call_type: e.target.value as CallType,
                })
              }
            >
              {callTypes.map((kind) => (
                <option key={kind} value={kind}>
                  {label(kind)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Rubric to edit
            <select
              aria-label="Rubric to edit"
              value={selected}
              onChange={(e) => setSelected(e.target.value as CallType)}
            >
              {callTypes.map((kind) => (
                <option key={kind} value={kind}>
                  {label(kind)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Rubric title
            <input
              value={rubric.title}
              maxLength={100}
              required
              onChange={(e) => change({ title: e.target.value })}
            />
          </label>
          <label>
            Scoring purpose
            <textarea
              value={rubric.purpose}
              maxLength={600}
              required
              onChange={(e) => change({ purpose: e.target.value })}
            />
          </label>
          {rubric.dimensions.map((item, index) => (
            <fieldset key={index}>
              <legend>{item.title || `Dimension ${index + 1}`}</legend>
              <label>
                Dimension ID
                <input
                  aria-label="Dimension ID"
                  value={item.id}
                  pattern="[a-z][a-z0-9_]{0,63}"
                  required
                  onChange={(e) =>
                    changeDimension(index, { id: e.target.value })
                  }
                />
              </label>
              <label>
                Dimension name
                <input
                  aria-label="Dimension name"
                  value={item.title}
                  maxLength={100}
                  required
                  onChange={(e) =>
                    changeDimension(index, { title: e.target.value })
                  }
                />
              </label>
              <label>
                Dimension weight
                <input
                  aria-label="Dimension weight"
                  type="number"
                  min={0.1}
                  max={10}
                  step={0.1}
                  value={item.weight}
                  required
                  onChange={(e) =>
                    changeDimension(index, { weight: Number(e.target.value) })
                  }
                />
              </label>
              {(["0", "1", "2"] as const).map((score, i) => (
                <label key={score}>
                  {
                    [
                      "Missed opportunity anchor",
                      "Partial demonstration anchor",
                      "Clearly demonstrated anchor",
                    ][i]
                  }
                  <textarea
                    aria-label={
                      [
                        "Missed opportunity anchor",
                        "Partial demonstration anchor",
                        "Clearly demonstrated anchor",
                      ][i]
                    }
                    rows={2}
                    value={item.anchors[score]}
                    maxLength={600}
                    required
                    onChange={(e) =>
                      changeDimension(index, {
                        anchors: { ...item.anchors, [score]: e.target.value },
                      })
                    }
                  />
                </label>
              ))}
              <button
                type="button"
                className="text-button"
                disabled={rubric.dimensions.length <= 1}
                onClick={() =>
                  change({
                    dimensions: rubric.dimensions.filter((_, i) => i !== index),
                  })
                }
              >
                Remove dimension
              </button>
            </fieldset>
          ))}
          <button
            type="button"
            className="secondary"
            disabled={rubric.dimensions.length >= 12}
            onClick={() =>
              change({
                dimensions: [
                  ...rubric.dimensions,
                  {
                    id: `criterion_${rubric.dimensions.length + 1}`,
                    title: "New observable criterion",
                    weight: 1,
                    anchors: { "0": "", "1": "", "2": "" },
                  },
                ],
              })
            }
          >
            Add dimension
          </button>{" "}
          <button className="primary" disabled={busy}>
            Save scoring rubrics
          </button>
        </form>
      )}
    </section>
  );
}

export function CallPurpose({
  analysis,
  reload,
}: {
  analysis: Analysis;
  reload: () => Promise<void>;
}) {
  const { api, run } = useWorkspace();
  const current =
    analysis.call.rubric_snapshot?.call_type || "cold_introduction";
  const [purpose, setPurpose] = useState(current);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setPurpose(current);
  }, [current, analysis.call.id]);
  return (
    <section aria-label="Call purpose">
      <p>
        <strong>
          {analysis.scorecard.title ||
            label(analysis.scorecard.call_type || current)}
        </strong>
      </p>
      <p className="small muted">
        {analysis.scorecard.purpose} No opportunity or insufficient evidence is
        unscored. Original speech expires; the retained assessment identifies
        its rubric version.
      </p>
      <label>
        Actual call purpose
        <select
          aria-label="Actual call purpose"
          value={purpose}
          onChange={(e) => setPurpose(e.target.value)}
        >
          {callTypes.map((kind) => (
            <option key={kind} value={kind}>
              {label(kind)}
            </option>
          ))}
          <option value="unknown">Uncertain / unassessed</option>
        </select>
      </label>
      <label>
        Reason for purpose correction
        <input
          aria-label="Reason for purpose correction"
          maxLength={500}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </label>
      <button
        className="secondary"
        disabled={busy || !reason.trim()}
        onClick={() => {
          setBusy(true);
          void run(async () => {
            try {
              await api(`/v1/calls/${analysis.call.id}/rubric`, "PUT", {
                expected_version: analysis.call.version,
                call_type: purpose,
                reason,
              });
              setReason("");
            } finally {
              try {
                await reload();
              } finally {
                setBusy(false);
              }
            }
          });
        }}
      >
        Update call purpose
      </button>
    </section>
  );
}
