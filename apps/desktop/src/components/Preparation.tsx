import { useEffect, useState } from "react";
import { useWorkspace } from "../context";
import { localEdit } from "../api/client";
import { limits, useDefinitions } from "../state/definitions";
import { ZonePicker } from "./Staffing";

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
  const bounds = useDefinitions();
  const b = bounds?.preparation;
  const [config, setConfig] = useState<Configuration | null>(null);
  const [template, setTemplate] = useState<Template | null>(null);
  // Questions are edited as text; blank lines are dropped only when saving.
  const [questions, setQuestions] = useState("");
  const [error, setError] = useState("");
  const apply = (result: Configuration) => {
    const next = result.script_template || result.template_defaults;
    setConfig(result);
    setTemplate(next);
    setQuestions(next.discovery_questions.join("\n"));
  };
  const load = async () => {
    try {
      apply(await api<Configuration>("/v1/admin/operating-policy"));
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };
  useEffect(() => {
    let current = true;
    void api<Configuration>("/v1/admin/operating-policy").then(
      (result) => current && apply(result),
      (reason: unknown) =>
        current &&
        setError(reason instanceof Error ? reason.message : String(reason)),
    );
    return () => {
      current = false;
    };
  }, []);
  if (!config || !template)
    return (
      <section className="card padded" aria-label="Script and preparation">
        <h2>Script and advance preparation</h2>
        <p role="status">
          {error
            ? `Script and preparation settings could not be loaded: ${error}`
            : "Loading script and preparation settings…"}
        </p>
        {error && (
          <button className="secondary" onClick={() => void load()}>
            Retry loading preparation settings
          </button>
        )}
      </section>
    );
  const setPolicy = (patch: Partial<PreparationPolicy>) =>
    setConfig({ ...config, preparation: { ...config.preparation, ...patch } });
  const number = (key: keyof PreparationPolicy, title: string) => (
    <label key={key}>
      {title}
      <input
        type="number"
        {...limits(b?.[key])}
        value={config.preparation[key] as number}
        onChange={(e) => setPolicy({ [key]: Number(e.target.value) })}
      />
    </label>
  );
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
                script_template: {
                  ...template,
                  discovery_questions: questions
                    .split("\n")
                    .map((q) => q.trim())
                    .filter(Boolean),
                },
                preparation: config.preparation,
              },
            );
            apply(saved);
            notice("Script and preparation saved");
          });
        }}
      >
        <button
          type="button"
          className="text-button"
          onClick={() =>
            void run(async () =>
              apply(await api<Configuration>("/v1/admin/operating-policy")),
            )
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
            value={questions}
            onChange={(e) => setQuestions(e.target.value)}
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
          {number("ahead_count", "Prepared-ahead target")}
          {number("workspace_ahead_cap", "Workspace preparation cap")}
          {number("stable_fact_days", "Stable fact cache (days)")}
          {number(
            "time_sensitive_fact_hours",
            "Time-sensitive fact cache (hours)",
          )}
          {number("batch_size", "Leads per work unit")}
          {number("off_hours_start", "Preparation starts at hour")}
          {number("off_hours_end", "Preparation ends at hour")}
          <ZonePicker
            title="Preparation timezone"
            value={config.preparation.timezone}
            onChange={(timezone) => setPolicy({ timezone })}
          />
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
