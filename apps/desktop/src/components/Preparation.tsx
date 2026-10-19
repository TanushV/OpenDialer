import { useEffect, useState } from "react";
import { useWorkspace } from "../context";
import { localEdit } from "../api/client";

type Template = {
  topic: string;
  opener: string;
  gatekeeper: string;
  screening_intro: string;
  discovery_questions: string[];
  proof: string;
  closing_question: string;
  final_voicemail_asset: string | null;
};
type PreparationPolicy = {
  enabled: boolean;
  ahead_count: number;
  workspace_ahead_cap: number;
  stable_fact_days: number;
  time_sensitive_fact_hours: number;
  batch_size: number;
  off_hours_start: number;
  off_hours_end: number;
  timezone: string;
};
type Configuration = {
  version: number;
  script_template: Template | null;
  template_defaults: Template;
  template_version: string;
  preparation: PreparationPolicy;
};
type Readiness = {
  ready: number;
  fallback: number;
  stale: number;
  failed: number;
  queued: number;
  target: number;
  runway_attempts: number;
  runway_note: string;
  runway_minutes: number | null;
  available_reps: number;
  inspected_rows: number;
  omitted_rows: number;
  within_schedule: boolean;
  enabled: boolean;
};

export function PreparationRunway() {
  const { api, run, subscribe } = useWorkspace();
  const [status, setStatus] = useState<Readiness | null>(null);
  useEffect(() => {
    const refresh = () =>
      void run(async () => setStatus(await api<Readiness>("/v1/preparation")));
    refresh();
    return subscribe((events) => {
      if (
        events.some(
          (event) =>
            event.type.startsWith("lead.") || event.type === "call.disposition",
        )
      )
        refresh();
    });
  }, []);
  return (
    <section
      className="card padded"
      data-testid="preparation-runway"
      aria-live="polite"
    >
      <h3>Prepared queue</h3>
      {status ? (
        <>
          <p>
            <strong>{status.runway_attempts} prepared attempts</strong> · Target{" "}
            {status.target} · {status.queued} queued
          </p>
          <p>
            {status.ready} personalized · {status.fallback} generic ·{" "}
            {status.stale} stale · {status.failed} failed
          </p>
          <p>
            {status.runway_minutes === null
              ? "No declared rep work"
              : `${Math.round(status.runway_minutes)} minutes at the declared schedule`}{" "}
            · {status.available_reps} rep(s). {status.inspected_rows} candidates
            inspected · {status.omitted_rows} beyond this page. Time is an
            estimate from declared availability and attempt work.
          </p>
          <p className="fine">
            {status.enabled
              ? status.within_schedule
                ? "Background preparation window is open"
                : "Outside preparation window"
              : "Automatic preparation paused"}
            . {status.runway_note} This machine or the selected server must be
            awake.
          </p>
          <button
            className="text-button"
            onClick={() =>
              void run(async () =>
                setStatus(await api<Readiness>("/v1/preparation")),
              )
            }
          >
            Refresh preparation status
          </button>
        </>
      ) : (
        <p>Loading preparation status…</p>
      )}
    </section>
  );
}

export function PreparationSettings() {
  const { api, run, notice, snapshot: workspace } = useWorkspace();
  const [config, setConfig] = useState<Configuration | null>(null);
  const [template, setTemplate] = useState<Template | null>(null);
  useEffect(() => {
    void run(async () => {
      const result = await api<Configuration>("/v1/admin/operating-policy");
      setConfig(result);
      setTemplate(result.script_template || result.template_defaults);
    });
  }, []);
  if (!config || !template) return null;
  const setPolicy = (patch: Partial<PreparationPolicy>) =>
    setConfig({ ...config, preparation: { ...config.preparation, ...patch } });
  return (
    <section className="card padded">
      <h2>Script and advance preparation</h2>
      <p>
        Your structure stays fixed. Preparation fills relevance and proof from
        supported facts; the live helper can phrase contextual advice freely.
        The displayed script stays fixed during a call.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void run(async () => {
            const saved = await localEdit<Configuration>(
              api,
              "/v1/admin/operating-policy",
              "PUT",
              {
                request_id: crypto.randomUUID(),
                expected_version: config.version,
                script_template: template,
                preparation: config.preparation,
              },
            );
            setConfig(saved);
            notice("Script and preparation saved");
          });
        }}
      >
        <button
          type="button"
          className="text-button"
          onClick={() =>
            void run(async () => {
              const current = await api<Configuration>(
                "/v1/admin/operating-policy",
              );
              setConfig(current);
              setTemplate(current.script_template || current.template_defaults);
            })
          }
        >
          Reload preparation settings
        </button>
        <label>
          Script topic
          <input
            value={template.topic}
            maxLength={150}
            onChange={(e) =>
              setTemplate({ ...template, topic: e.target.value })
            }
            required
          />
        </label>
        <p className="fine">
          Use{" "}
          {"{name}, {rep}, {seller}, {company}, {topic}, {relevance}, {proof}"}.
          Identity and fixed text are yours to approve. The screening line must
          include {"{seller}"}.
        </p>
        {(
          [
            ["opener", "Opener template"],
            ["gatekeeper", "Gatekeeper template"],
            ["screening_intro", "iPhone screening template"],
            ["proof", "Proof template"],
            ["closing_question", "Closing question template"],
          ] as const
        ).map(([field, title]) => (
          <label key={field}>
            {title}
            <textarea
              aria-label={title}
              rows={2}
              value={template[field]}
              onChange={(e) =>
                setTemplate({ ...template, [field]: e.target.value })
              }
            />
          </label>
        ))}
        <label>
          Discovery questions (one per line)
          <textarea
            aria-label="Discovery questions (one per line)"
            rows={3}
            value={template.discovery_questions.join("\n")}
            onChange={(e) =>
              setTemplate({
                ...template,
                discovery_questions: e.target.value.split("\n"),
              })
            }
          />
        </label>
        <label className="check-label">
          <input
            type="checkbox"
            checked={config.preparation.enabled}
            onChange={(e) => setPolicy({ enabled: e.target.checked })}
          />{" "}
          Prepare ahead automatically
        </label>
        <label>
          Final-attempt voicemail reference
          <select
            aria-label="Final-attempt voicemail reference"
            value={template.final_voicemail_asset || ""}
            onChange={(event) =>
              setTemplate({
                ...template,
                final_voicemail_asset: event.target.value || null,
              })
            }
          >
            <option value="">No reference</option>
            {workspace.policy.approved_assets?.map((asset) => (
              <option key={asset} value={asset}>
                {asset}
              </option>
            ))}
          </select>
        </label>
        <p className="fine">
          Choose an already reviewed message. This reference grants no playback
          permission; voicemail still requires the final permitted attempt and
          the current call's authorization.
        </p>
        <div className="two-column">
          <label>
            Prepared-ahead target
            <input
              type="number"
              min={1}
              max={100}
              value={config.preparation.ahead_count}
              onChange={(e) =>
                setPolicy({ ahead_count: Number(e.target.value) })
              }
            />
          </label>
          <label>
            Workspace preparation cap
            <input
              type="number"
              min={1}
              max={500}
              value={config.preparation.workspace_ahead_cap}
              onChange={(e) =>
                setPolicy({ workspace_ahead_cap: Number(e.target.value) })
              }
            />
          </label>
          <label>
            Stable fact cache (days)
            <input
              type="number"
              min={1}
              max={30}
              value={config.preparation.stable_fact_days}
              onChange={(e) =>
                setPolicy({ stable_fact_days: Number(e.target.value) })
              }
            />
          </label>
          <label>
            Time-sensitive fact cache (hours)
            <input
              type="number"
              min={1}
              max={24}
              value={config.preparation.time_sensitive_fact_hours}
              onChange={(e) =>
                setPolicy({ time_sensitive_fact_hours: Number(e.target.value) })
              }
            />
          </label>
          <label>
            Leads per work unit
            <input
              type="number"
              min={1}
              max={20}
              value={config.preparation.batch_size}
              onChange={(e) =>
                setPolicy({ batch_size: Number(e.target.value) })
              }
            />
          </label>
          <label>
            Preparation starts at hour
            <input
              type="number"
              min={0}
              max={23}
              value={config.preparation.off_hours_start}
              onChange={(e) =>
                setPolicy({ off_hours_start: Number(e.target.value) })
              }
            />
          </label>
          <label>
            Preparation ends at hour
            <input
              type="number"
              min={0}
              max={24}
              value={config.preparation.off_hours_end}
              onChange={(e) =>
                setPolicy({ off_hours_end: Number(e.target.value) })
              }
            />
          </label>
          <label>
            Preparation timezone
            <input
              value={config.preparation.timezone}
              onChange={(e) => setPolicy({ timezone: e.target.value })}
              required
            />
          </label>
        </div>
        <p className="fine">
          Hours use 24-hour time; 18 to 8 runs overnight. Automatic work
          respects this window and your usage budget. Manual preparation starts
          when requested. Local preparation resumes when the machine wakes.
        </p>
        <button className="primary" type="submit">
          Save script and preparation
        </button>
      </form>
    </section>
  );
}
