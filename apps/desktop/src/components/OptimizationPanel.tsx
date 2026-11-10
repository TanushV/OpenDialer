import { useCallback, useEffect, useRef, useState } from "react";
import { useWorkspace } from "../context";
import { limits, useDefinitions } from "../state/definitions";
import type { EstimatePolicy } from "../types";
import { readNewest, useNewestAnswer } from "../state/newest";

export interface HealthPolicy {
  min_comparable: number;
  material_drop: number;
  confidence: number;
  window_days: number;
}
export interface OptimizationSettings {
  conversion_goal: string;
  window_days: number;
  exploration: number;
  auto_train: boolean;
  horizon_hours: number;
  rep_minutes: number;
  handling_seconds: number;
  daily_capacity_per_number: number;
  training_min_rows: number;
  account_channel_limit: number | null;
  minimum_expansion_age_hours: number;
  estimate_policy?: EstimatePolicy;
  health_policy?: HealthPolicy;
}
/** R8.4: the report is computed when read; Advanced only needs the saved settings from it. */
function useLearningSettings() {
  const { api, subscribe } = useWorkspace();
  const [settings, setSettings] = useState<OptimizationSettings | null>(null);
  const newest = useNewestAnswer();
  const load = useCallback(
    () =>
      readNewest(
        newest,
        () => api<{ settings: OptimizationSettings }>("/v1/optimization"),
        (report) => setSettings(report.settings),
      ),
    [api, newest],
  );
  useEffect(() => {
    void load().catch(() => {});
    return subscribe((events) => {
      if (events.some((e) => e.type === "optimization.updated"))
        void load().catch(() => {});
    });
  }, [load, subscribe]);
  return { settings, load };
}

/** Settings → Advanced: planning assumptions. What StatML has learned is the card in Review. */
export function LearningAdvanced() {
  const { settings, load } = useLearningSettings();
  return settings ? (
    <PlanningAssumptions settings={settings} onSaved={load} />
  ) : null;
}

const ADVANCED_KEYS = [
  "exploration",
  "horizon_hours",
  "rep_minutes",
  "handling_seconds",
  "daily_capacity_per_number",
  "training_min_rows",
  "auto_train",
  "account_channel_limit",
  "minimum_expansion_age_hours",
  "health_policy",
] as const;
type AdvancedKey = (typeof ADVANCED_KEYS)[number];
type Assumptions = Pick<OptimizationSettings, AdvancedKey>;
const pick = (settings: OptimizationSettings) =>
  Object.fromEntries(
    ADVANCED_KEYS.map((key) => [key, settings[key]]),
  ) as Assumptions;

function PlanningAssumptions({
  settings,
  onSaved,
}: {
  settings: OptimizationSettings;
  onSaved: () => Promise<void>;
}) {
  const { api, run, notice, refresh } = useWorkspace();
  const bounds = useDefinitions();
  const b = bounds?.optimization;
  const h = bounds?.health_policy;
  const [draft, setDraft] = useState<Assumptions>(() => pick(settings));
  const dirty = useRef(false);
  const editRevision = useRef(0);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!dirty.current) setDraft(pick(settings));
  }, [settings]);
  const change = <K extends AdvancedKey>(key: K, value: Assumptions[K]) => {
    dirty.current = true;
    editRevision.current++;
    setDraft((old) => ({ ...old, [key]: value }));
  };
  const number = (key: AdvancedKey, title: string, step?: string) => (
    <label key={key}>
      {title}
      <input
        type="number"
        step={step}
        {...limits(b?.[key])}
        value={(draft[key] as number | null) ?? ""}
        onChange={(e) =>
          change(
            key,
            (key === "account_channel_limit" && !e.target.value
              ? null
              : Number(e.target.value)) as never,
          )
        }
      />
    </label>
  );
  const estimate = settings.estimate_policy;
  return (
    <section
      className="card padded optimization-panel"
      aria-label="Planning assumptions"
    >
      <h2>Planning assumptions</h2>
      <p className="fine">
        Engineering starting values for forecasts and learning. Calling
        permissions, callbacks, stable caller identity and coverage stay
        mandatory; nothing here authorizes a number purchase.
      </p>
      <div className="optimization-fields">
        {number("exploration", "Exploration share", "0.01")}
        {number("horizon_hours", "Forecast horizon (hours)")}
        {number("rep_minutes", "Available rep minutes")}
        {number("handling_seconds", "Average handling time (seconds)")}
        {number(
          "daily_capacity_per_number",
          "Reviewed daily capacity per number",
        )}
        {number("training_min_rows", "Minimum training rows")}
        {number(
          "account_channel_limit",
          "Account concurrent-call limit (blank if unknown)",
        )}
        {number(
          "minimum_expansion_age_hours",
          "Shortfall review interval (hours)",
        )}
      </div>
      <label className="check-row">
        <input
          type="checkbox"
          checked={draft.auto_train}
          onChange={(e) => change("auto_train", e.target.checked)}
        />
        Automatic training from call history
      </label>
      {draft.health_policy && (
        <details>
          <summary>Advanced number evidence policy</summary>
          <p className="fine">
            Review assumptions. A statistical warning is not proof of a carrier
            spam label. Changes require fresh health evaluation; calling
            identity is preserved.
          </p>
          <div className="optimization-fields">
            {(
              [
                ["min_comparable", "Minimum comparable attempts"],
                ["material_drop", "Material pickup drop (fraction)"],
                ["confidence", "Health interval confidence (fraction)"],
                ["window_days", "Health observation window (days)"],
              ] as const
            ).map(([key, title]) => (
              <label key={key}>
                {title}
                <input
                  type="number"
                  step="any"
                  {...limits(h?.[key])}
                  value={draft.health_policy![key]}
                  onChange={(e) =>
                    change("health_policy", {
                      ...draft.health_policy!,
                      [key]: Number(e.target.value),
                    })
                  }
                />
              </label>
            ))}
          </div>
        </details>
      )}
      {estimate && (
        <details>
          <summary>Sparse estimate assumptions</summary>
          <p className="fine" data-testid="estimate-assumptions">
            Human pickup prior {estimate.human_prior[0]} successes /{" "}
            {estimate.human_prior[1]} failures · Right party prior{" "}
            {estimate.right_prior[0]} / {estimate.right_prior[1]} · Selected
            goal prior {estimate.goal_prior[0]} / {estimate.goal_prior[1]} ·
            Pooled shrinkage weight {estimate.shrinkage} · Work prior{" "}
            {estimate.work_prior} attempt equivalents.
          </p>
          <p className="fine">
            Read-only starting weights. They apply until observed outcomes
            provide support; changing them requires renewed model evaluation.
          </p>
        </details>
      )}
      <button
        className="secondary"
        disabled={saving}
        onClick={() =>
          void run(async () => {
            const revision = editRevision.current;
            setSaving(true);
            try {
              const original = pick(settings);
              const changed = Object.fromEntries(
                ADVANCED_KEYS.filter(
                  (key) =>
                    JSON.stringify(draft[key]) !==
                    JSON.stringify(original[key]),
                ).map((key) => [key, draft[key]]),
              );
              await api("/v1/admin/optimization", "PUT", changed);
              if (editRevision.current === revision) dirty.current = false;
              await onSaved();
              await refresh();
              notice("Planning assumptions saved");
            } finally {
              setSaving(false);
            }
          })
        }
      >
        Save planning assumptions
      </button>
    </section>
  );
}
