import { countLabel, useHistory } from "../state/useHistory";
import { HistoryMore } from "./HistoryMore";
import { BillingReconciliation } from "./BillingReconciliation";
import { OperatingPolicySettings } from "./OperatingPolicySettings";
import { WorkTime } from "./WorkTime";
import { CallbackRouting } from "./CallbackRouting";
import { RecoveryPanel } from "./RecoveryPanel";
import { RubricSettings } from "./Rubrics";
import { PreparationSettings } from "./Preparation";
import { AccessAndRecovery } from "./AdminWorkflows";
import { Trackers } from "./AdvancedIntelligence";
import {
  LearningAdvanced,
  type OptimizationSettings,
} from "./OptimizationPanel";
import { WeeklySchedule, type SimpleStaffing } from "./Staffing";
import type { Update } from "@tauri-apps/plugin-updater";
import {
  ArrowDownToLine,
  Database,
  Download,
  LoaderCircle,
  LockKeyhole,
  RefreshCw,
  ShieldCheck,
  Wifi,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  LONG_REQUEST_MS,
  native,
  openSource,
  tunnelStatus,
  saveTunnel,
  assertNoPendingOptouts,
  type TunnelStatus,
  type ProviderCredential,
} from "../api/client";
import { date, isAdmin, label, useWorkspace } from "../context";
import { limits, options, useDefinitions } from "../state/definitions";
import type { CapabilityStatus, Policy, Readiness } from "../types";

type BudgetData = {
  daily_limit_usd: number;
  monthly_limit_usd: number;
  number_purchase_limit_usd: number;
};
type SettingsData = {
  version: number;
  effective: Record<string, string>;
  configured: Record<string, boolean>;
  budget: BudgetData;
  optimization: OptimizationSettings;
  defaults: { optimization: OptimizationSettings };
  [key: string]: unknown;
};
type View = "Dialer" | "Leads" | "Review" | "Numbers" | "Intelligence";
const message = (reason: unknown) =>
  reason instanceof Error ? reason.message : String(reason);

export function SettingsPage({
  onDisconnect,
  onNavigate,
}: {
  onDisconnect: () => void;
  onNavigate: (view: View) => void;
}) {
  const { snapshot: w } = useWorkspace();
  if (!isAdmin(w.user.role))
    return (
      <>
        <div className="page-heading">
          <div>
            <span className="eyebrow">YOUR WORKSPACE</span>
            <h1>Settings</h1>
            <p>
              Your working time, workspace connection and app updates. An
              administrator manages providers, policy and numbers.
            </p>
          </div>
        </div>
        <WorkTime />
        <WorkspaceConnection onDisconnect={onDisconnect} />
        <UpdateSettings />
      </>
    );
  return <AdminSettings onDisconnect={onDisconnect} onNavigate={onNavigate} />;
}

/** One settings read for the admin page; failures stay visible and retryable. */
function useAdminSettings() {
  const { api, subscribe } = useWorkspace();
  const [data, setData] = useState<SettingsData | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    try {
      setData(await api<SettingsData>("/v1/admin/settings"));
      setError("");
    } catch (reason) {
      setError(message(reason));
      throw reason;
    }
  }, [api]);
  useEffect(() => {
    void load().catch(() => {});
    return subscribe((events) => {
      if (events.some((e) => e.type === "optimization.updated"))
        void load().catch(() => {});
    });
  }, [load, subscribe]);
  return { data, error, load, setData };
}
type AdminSettingsState = ReturnType<typeof useAdminSettings>;

function AdminSettings({
  onDisconnect,
  onNavigate,
}: {
  onDisconnect: () => void;
  onNavigate: (view: View) => void;
}) {
  const { api, run } = useWorkspace();
  const settings = useAdminSettings();
  const [preflight, setPreflight] = useState<Readiness | null>(null);
  const [section, setSection] = useState("Setup");
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">A WORKSPACE YOU CAN TRUST</span>
          <h1>Settings &amp; readiness</h1>
          <p>
            Work through setup once, then keep a recoverable, versioned record.
          </p>
        </div>
        <button
          className="primary"
          onClick={() =>
            void run(async () => setPreflight(await api("/v1/admin/preflight")))
          }
        >
          <ShieldCheck size={17} />
          Run preflight
        </button>
      </div>
      {preflight && <PreflightResult preflight={preflight} />}
      <div className="tabs">
        {["Setup", "Policy & DNC", "Operations", "App updates"].map((tab) => (
          <button
            className={section === tab ? "selected" : ""}
            aria-pressed={section === tab}
            onClick={() => setSection(tab)}
            key={tab}
          >
            {tab}
          </button>
        ))}
      </div>
      {section === "Setup" && (
        <SetupChecklist
          settings={settings}
          preflight={preflight}
          onNavigate={onNavigate}
          onSection={setSection}
        />
      )}
      {section === "Policy & DNC" && (
        <>
          <OperatingPolicySettings />
          <div className="two-column">
            <CallingPolicySettings />
            <RegistrySettings />
          </div>
        </>
      )}
      {section === "Operations" && (
        <OperationsSettings onDisconnect={onDisconnect} />
      )}
      {section === "App updates" && <UpdateSettings />}
      <AdvancedView settings={settings} />
    </>
  );
}

function PreflightResult({ preflight }: { preflight: Readiness }) {
  const { snapshot: w } = useWorkspace();
  return (
    <section
      className={`preflight card ${preflight.ready_to_call ? "passed" : ""}`}
    >
      <div>
        <ShieldCheck size={24} />
        <div>
          <h3>
            {preflight.ready_to_call
              ? w.simulation
                ? "Simulation checks passed"
                : "Configuration checks passed"
              : "A few things need attention"}
          </h3>
          <p>
            {preflight.integrity_ok
              ? "Database integrity verified"
              : "Database integrity check failed"}{" "}
            · {preflight.version} · {preflight.mode}
          </p>
        </div>
      </div>
      {preflight.screening && (
        <div className="stack">
          <p>
            National screening: {label(preflight.screening.mode)} ·{" "}
            {preflight.screening.cleared_count} clear ·{" "}
            {preflight.screening.pending_count} pending ·{" "}
            {preflight.screening.blocked_count} blocked.
          </p>
          <p className="fine">
            {preflight.screening.dialable_queue_count} currently eligible
            candidates for this rep in {preflight.screening.inspected_count}{" "}
            inspected leads, of {preflight.screening.total_count} total.
            {preflight.screening.omitted_count > 0 &&
              ` ${preflight.screening.omitted_count} leads remain outside this bounded check.`}{" "}
            Every submission is checked again.
          </p>
        </div>
      )}
      {preflight.blockers.map((b) => (
        <p className="preflight-blocker" key={b}>
          ● {label(b)}
        </p>
      ))}
      {preflight.warnings.map((warning) => (
        <p className="small" key={warning}>
          {warning}
        </p>
      ))}
    </section>
  );
}

type StepState = CapabilityStatus["state"];
const tone = (state: StepState) =>
  ["configured", "software_verified", "live_verified"].includes(state)
    ? "green"
    : state === "unavailable"
      ? "red"
      : "amber";

function Step({
  id,
  index,
  title,
  state,
  summary,
  children,
}: {
  id: string;
  index: number;
  title: string;
  state: StepState;
  summary: string;
  children: React.ReactNode;
}) {
  return (
    <details className="setup-step" data-testid={`setup-${id}`}>
      <summary>
        <span className="setup-index">{index}</span>
        <strong>{title}</strong>
        <span className={`pill ${tone(state)}`}>{label(state)}</span>
        <span className="fine setup-summary">{summary}</span>
      </summary>
      <div className="setup-body">{children}</div>
    </details>
  );
}

/** Admin setup in order: each step shows saved or checked state and its remedy. */
function SetupChecklist({
  settings,
  preflight,
  onNavigate,
  onSection,
}: {
  settings: AdminSettingsState;
  preflight: Readiness | null;
  onNavigate: (view: View) => void;
  onSection: (section: string) => void;
}) {
  const { snapshot: w, api } = useWorkspace();
  const bounds = useDefinitions();
  const readiness = preflight ?? w.readiness;
  const capabilities = readiness.capabilities;
  const capability = (id: string) => capabilities?.find((c) => c.id === id);
  const [numbers, setNumbers] = useState<{
    total: number;
    ready: number;
  } | null>(null);
  useEffect(() => {
    let live = true;
    void api<{ inventory: { total: number; ready: number } }>(
      "/v1/admin/numbers/summary",
    )
      .then((value) => live && setNumbers(value.inventory))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [api, w.numbers]);
  const data = settings.data;
  if (!data)
    return (
      <section className="card padded" aria-label="Setup checklist">
        <p role="status">
          {settings.error
            ? `Saved settings could not be loaded: ${settings.error}`
            : "Loading saved settings…"}
        </p>
        {settings.error && (
          <button
            className="secondary"
            onClick={() => void settings.load().catch(() => {})}
          >
            Retry loading settings
          </button>
        )}
      </section>
    );
  const configured = data.configured;
  const checked = (id: string, saved: boolean): StepState =>
    capability(id)?.state ?? (saved ? "configured" : "unconfigured");
  const remedy = (id: string, fallback: string) =>
    capability(id)?.remedy ?? fallback;
  const telnyxSaved =
    ["telnyx_api_key"].every((k) => configured[k]) &&
    ["telnyx_connection_id", "telnyx_public_key"].every((k) => !!data[k]);
  const knowledge = !!w.active_playbook?.product_description?.trim();
  const registry = w.registries.some(
    (r) => r.kind === "national" && r.published,
  );
  const leads = w.pages?.leads?.total ?? w.leads.length;
  const optimization = data.optimization;
  const defaults = data.defaults.optimization;
  const scheduleDefault = (
    [
      "staffing_timezone",
      "staffing_start_hour",
      "staffing_end_hour",
      "staffing_reps",
    ] as const
  ).every((k) => optimization[k] === defaults[k]);
  const budget = data.budget;
  return (
    <section className="setup-checklist" aria-label="Setup checklist">
      <p className="fine">
        {capabilities
          ? `Capability evidence checked ${date(readiness.capabilities_as_of)}${readiness.capabilities_stale ? "; it may be out of date" : ""}. `
          : "Capabilities have not been checked since this authority started; states below come from saved settings. "}
        Run preflight for a fresh no-charge check. Simulation never claims live
        verification.
      </p>
      <Step
        id="identity"
        index={1}
        title="Business identity & knowledge"
        state={w.seller.approved && knowledge ? "configured" : "unconfigured"}
        summary={`${w.seller.approved ? "Identity reviewed" : "Record your business identity evidence"} · ${knowledge ? "business knowledge published" : "describe what you sell and which claims are valid"}`}
      >
        <IdentityStep onKnowledge={() => onNavigate("Intelligence")} />
      </Step>
      <Step
        id="telnyx"
        index={2}
        title="Calling (Telnyx)"
        state={checked("telephony", telnyxSaved)}
        summary={remedy(
          "telephony",
          telnyxSaved
            ? "Saved; run preflight and test an owned destination."
            : "Add your Telnyx key, connection and webhook signing key.",
        )}
      >
        <TelnyxStep settings={settings} />
      </Step>
      <Step
        id="helper"
        index={3}
        title="Live helper (OpenRouter)"
        state={checked("live_helper", !!configured.openrouter_api_key)}
        summary={remedy(
          "live_helper",
          configured.openrouter_api_key
            ? "Key saved; funded Cerebras acceptance still required."
            : "Add an OpenRouter key; reps keep the prepared script without it.",
        )}
      >
        <CredentialButton
          field="openrouter_api_key"
          title="OpenRouter API key"
          settings={settings}
        />
        <p className="fine">
          Cerebras only, zero data retention, no other-provider fallback. A
          refused or unsupported endpoint disables help instead of switching
          models.
          {data.effective.coach_provider === "static" &&
            " Approved static guidance is selected under Advanced."}
        </p>
      </Step>
      <Step
        id="dnc"
        index={4}
        title="Do-not-call sources"
        state={checked("national_dnc", registry || !!configured.dnc_api_key)}
        summary={remedy(
          "national_dnc",
          registry
            ? "A national registry snapshot is published."
            : "Import seller-authorized national coverage or configure prepaid screening.",
        )}
      >
        <p>
          National screening and your internal opt-outs are checked before every
          US call. Missing, stale or uncovered data blocks dialing.
        </p>
        {capability("national_dnc")?.reason_codes.length ? (
          <p className="fine">
            {capability("national_dnc")!.reason_codes.map(label).join(" · ")}
          </p>
        ) : null}
        <button className="secondary" onClick={() => onSection("Policy & DNC")}>
          Open DNC sources
        </button>
      </Step>
      <Step
        id="numbers"
        index={5}
        title="Existing numbers"
        state={
          !numbers
            ? "unconfigured"
            : numbers.total === 0
              ? "unconfigured"
              : numbers.ready
                ? "configured"
                : "degraded"
        }
        summary={
          numbers
            ? `${numbers.total} owned · ${numbers.ready} ready. New rentals are blocked; reuse owned numbers.`
            : "Inventory totals are loading."
        }
      >
        <p>
          Sync numbers you already own at Telnyx, then complete identity and
          callback evidence for each. OpenDialer never rents new numbers.
        </p>
        <button className="secondary" onClick={() => onNavigate("Numbers")}>
          Open Numbers
        </button>
      </Step>
      <Step
        id="leads"
        index={6}
        title="Lead list"
        state={leads ? "configured" : "unconfigured"}
        summary={
          leads
            ? `${countLabel(leads, w.pages?.leads?.total_capped)} leads imported; suppressions are preserved.`
            : "Import a CSV of the people you are allowed to call."
        }
      >
        <button className="secondary" onClick={() => onNavigate("Leads")}>
          Open Leads
        </button>
      </Step>
      <Step
        id="campaign"
        index={7}
        title="Campaign goal, hours & attempts"
        state={w.policy.approved ? "configured" : "unconfigured"}
        summary={`${label(optimization.conversion_goal)} within ${optimization.window_days} days · calls ${w.policy.local_start_hour}:00–${w.policy.local_end_hour}:00 prospect time · up to ${w.policy.max_attempts} attempts${w.policy.approved ? "" : " · not yet approved for live calling"}`}
      >
        <CampaignGoal
          current={optimization}
          bounds={bounds}
          onSaved={settings.load}
        />
        <CallingWindow bounds={bounds} />
      </Step>
      <Step
        id="schedule"
        index={8}
        title="Rep schedule (weekly)"
        state={scheduleDefault ? "unconfigured" : "configured"}
        summary={`${scheduleDefault ? "Default schedule: " : ""}${optimization.staffing_start_hour}:00–${optimization.staffing_end_hour}:00 ${optimization.staffing_timezone.replaceAll("_", " ")} · ${optimization.staffing_reps} rep(s) · ${optimization.staffing_weekdays.length} day(s) a week`}
      >
        <ScheduleStep
          current={optimization}
          bounds={bounds}
          onSaved={settings.load}
        />
      </Step>
      <Step
        id="budget"
        index={9}
        title="Budgets"
        state={
          budget.daily_limit_usd > 0 && budget.monthly_limit_usd > 0
            ? "configured"
            : "unconfigured"
        }
        summary={`Optional AI work: $${budget.daily_limit_usd.toFixed(2)} per day, $${budget.monthly_limit_usd.toFixed(2)} per month. Calling controls are never budget-gated.`}
      >
        <BudgetStep settings={settings} bounds={bounds} />
      </Step>
      {capabilities && (
        <section className="card padded" aria-label="Capability evidence">
          <h3>Capability evidence</h3>
          {capabilities.map((c) => (
            <p className="small" key={c.id} data-testid={`capability-${c.id}`}>
              <strong>{label(c.id)}</strong> · {label(c.state)} ·{" "}
              {c.evidence_at
                ? `Evidence ${date(c.evidence_at)}`
                : "No live evidence"}
              {c.reason_codes.length
                ? ` · ${c.reason_codes.map(label).join(", ")}`
                : ""}
            </p>
          ))}
        </section>
      )}
    </section>
  );
}

function IdentityStep({ onKnowledge }: { onKnowledge: () => void }) {
  const { snapshot: w, api, run, refresh, notice } = useWorkspace();
  const [evidence, setEvidence] = useState("");
  return (
    <>
      <label>
        Business identity evidence
        <input
          value={evidence}
          onChange={(e) => setEvidence(e.target.value)}
          placeholder="Verified business registration / identity reference"
        />
      </label>
      <button
        className="secondary"
        disabled={!evidence.trim()}
        onClick={() =>
          void run(async () => {
            await api("/v1/admin/seller/approve", "POST", {
              identity_reference: evidence,
            });
            await refresh();
            notice(
              "Business identity review recorded. Campaign permission remains separately controlled.",
            );
          })
        }
      >
        Record business identity approval
      </button>
      {w.seller.approved && (
        <p className="fine">Identity approval is recorded for this seller.</p>
      )}
      <p>
        Business knowledge tells the helper what you sell, which prices and
        claims are valid, and what to avoid.
      </p>
      <button className="secondary" onClick={onKnowledge}>
        Edit business knowledge
      </button>
    </>
  );
}

function CredentialButton({
  field,
  title,
  settings,
}: {
  field: ProviderCredential;
  title: string;
  settings: AdminSettingsState;
}) {
  const { run, notice, refresh, configureCredential } = useWorkspace();
  const [busy, setBusy] = useState(false);
  const data = settings.data;
  return (
    <div data-testid={`credential-${field}`}>
      <button
        className="secondary"
        aria-label={title}
        disabled={!data || busy || !native()}
        onClick={() =>
          void run(async () => {
            if (!data) return;
            setBusy(true);
            try {
              const result = await configureCredential(field, data.version);
              settings.setData({
                ...data,
                version: result.version,
                configured: { ...data.configured, ...result.configured },
              });
              notice("Credential saved in the selected authority.");
              await refresh();
            } finally {
              setBusy(false);
            }
          })
        }
      >
        {title}
      </button>
      <p className="fine">
        {data?.configured[field] ? "Configured" : "Not configured"} ·{" "}
        {native()
          ? "Enter in a protected system dialog"
          : "Use the installed desktop to enter keys"}
      </p>
    </div>
  );
}

/** A draft of non-secret provider fields with a stable request identity per edit. */
function useProviderDraft(settings: AdminSettingsState, keys: string[]) {
  const { api, notice, refresh } = useWorkspace();
  const data = settings.data;
  const read = () =>
    Object.fromEntries(
      keys.map((key) => {
        const value = data?.[key] ?? data?.effective?.[key];
        return [key, typeof value === "string" ? value : ""];
      }),
    );
  const [draft, setDraft] = useState<Record<string, string>>(read);
  const [busy, setBusy] = useState(false);
  const edit = useRef<{ body: string; request_id: string } | null>(null);
  useEffect(() => {
    setDraft(read());
    edit.current = null;
  }, [data?.version]);
  const save = async (done: string) => {
    if (!data) return;
    const values = Object.fromEntries(
      Object.entries(draft).filter(([, v]) => v !== ""),
    );
    const body = JSON.stringify({ values, version: data.version });
    if (edit.current?.body !== body)
      edit.current = { body, request_id: crypto.randomUUID() };
    setBusy(true);
    try {
      settings.setData(
        await api<SettingsData>("/v1/admin/settings", "PUT", {
          ...values,
          request_id: edit.current.request_id,
          expected_version: data.version,
        }),
      );
      edit.current = null;
      notice(done);
      await refresh();
    } finally {
      setBusy(false);
    }
  };
  const field = (key: string, title: string, placeholder = "") => (
    <label key={key}>
      {title}
      <input
        disabled={!data || busy}
        autoComplete="off"
        spellCheck={false}
        value={draft[key] || ""}
        placeholder={placeholder}
        onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
      />
    </label>
  );
  return { draft, setDraft, busy, save, field };
}

function TelnyxStep({ settings }: { settings: AdminSettingsState }) {
  const { api, run } = useWorkspace();
  const form = useProviderDraft(settings, [
    "telnyx_connection_id",
    "telnyx_public_key",
    "webhook_url",
    "telephony_credential_id",
    "sip_username",
    "traffic_approval_reference",
  ]);
  const [ingress, setIngress] = useState<Record<string, unknown> | null>(null);
  return (
    <>
      <p className="muted small">
        Account credentials stay with this authority. Desktop clients receive
        scoped telephony tokens only.
      </p>
      <CredentialButton
        field="telnyx_api_key"
        title="Telnyx API key"
        settings={settings}
      />
      {form.field("telnyx_connection_id", "Call Control connection ID")}
      {form.field("telnyx_public_key", "Ed25519 webhook public key")}
      {form.field(
        "webhook_url",
        "Public HTTPS webhook URL",
        "https://your-endpoint/webhooks/telnyx",
      )}
      <details>
        <summary>Agent credential and carrier approval</summary>
        {form.field(
          "telephony_credential_id",
          "Scoped telephony credential ID",
        )}
        {form.field("sip_username", "Agent SIP username")}
        {form.field(
          "traffic_approval_reference",
          "Carrier traffic approval reference",
        )}
      </details>
      <div className="button-row">
        <button
          className="primary"
          disabled={!settings.data || form.busy}
          onClick={() =>
            void run(() => form.save("Telnyx settings saved securely."))
          }
        >
          Save Telnyx settings
        </button>
        <button
          className="secondary"
          disabled={!settings.data || form.busy}
          onClick={() =>
            void run(async () => {
              if (!settings.data) return;
              setIngress(
                await api("/v1/admin/ingress-check", "POST", {
                  request_id: crypto.randomUUID(),
                  expected_version: settings.data.version,
                }),
              );
            })
          }
        >
          Check saved ingress
        </button>
      </div>
      <p className="fine">
        Signed carrier callbacks need a public HTTPS route to this authority's
        webhook endpoint; keep administrative routes private. The ingress check
        verifies routing and signature handling without a carrier call.
      </p>
      {ingress && (
        <pre className="small" aria-label="Ingress check result">
          {JSON.stringify(ingress, null, 2)}
        </pre>
      )}
    </>
  );
}

interface GoalPreview {
  campaign_version: number;
  from_goal: string;
  to_goal: string;
  completed_before: number;
  completed_after: number;
  requires_next_action: number;
  preserved_holds: number;
  preserved_callbacks: number;
}

function CampaignGoal({
  current,
  bounds,
  onSaved,
}: {
  current: OptimizationSettings;
  bounds: ReturnType<typeof useDefinitions>;
  onSaved: () => Promise<void>;
}) {
  const { api, run, notice, refresh } = useWorkspace();
  const [goal, setGoal] = useState(current.conversion_goal);
  const [days, setDays] = useState(current.window_days);
  const [preview, setPreview] = useState<GoalPreview | null>(null);
  useEffect(() => {
    setGoal(current.conversion_goal);
    setDays(current.window_days);
  }, [current.conversion_goal, current.window_days]);
  const changed =
    goal !== current.conversion_goal || days !== current.window_days;
  const apply = async (version?: number) => {
    await api("/v1/admin/optimization", "PUT", {
      conversion_goal: goal,
      window_days: days,
      ...(version ? { expected_campaign_version: version } : {}),
    });
    await onSaved();
    await refresh();
    notice("Campaign goal saved");
  };
  return (
    <div className="setup-form">
      <div className="optimization-fields">
        <label>
          Conversion goal
          <select
            value={goal}
            onChange={(e) => {
              setGoal(e.target.value);
              setPreview(null);
            }}
          >
            {options(bounds?.optimization?.conversion_goal).map((value) => (
              <option key={value} value={value}>
                {label(value)}
              </option>
            ))}
            {!bounds && <option value={goal}>{label(goal)}</option>}
          </select>
        </label>
        <label>
          Outcome window (days)
          <input
            type="number"
            {...limits(bounds?.optimization?.window_days)}
            value={days}
            onChange={(e) => {
              setDays(Number(e.target.value));
              setPreview(null);
            }}
          />
        </label>
      </div>
      <button
        className="secondary"
        disabled={!changed}
        onClick={() =>
          void run(async () =>
            setPreview(
              await api<GoalPreview>("/v1/admin/optimization/preview", "POST", {
                conversion_goal: goal,
                window_days: days,
              }),
            ),
          )
        }
      >
        Save campaign goal
      </button>
      {!changed && (
        <p className="fine">Change the goal or window to review the effect.</p>
      )}
      {preview && (
        <section aria-label="Review goal change" className="alert">
          <div>
            <strong>
              {label(preview.from_goal)} → {label(preview.to_goal)}
            </strong>
            <p>
              {preview.completed_before} completed before;{" "}
              {preview.completed_after} completed under the new goal.
            </p>
            <p>
              Previously completed prospects require next-action review before
              new outreach: {preview.requires_next_action}. Existing holds:{" "}
              {preview.preserved_holds}; callbacks:{" "}
              {preview.preserved_callbacks}. Historical trials retain their
              original definition.
            </p>
            <div className="button-row">
              <button
                className="primary"
                onClick={() =>
                  void run(async () => {
                    try {
                      await apply(preview.campaign_version);
                      setPreview(null);
                    } catch (error) {
                      setPreview(null);
                      if (!(error instanceof ApiError && error.status === 409))
                        throw error;
                      // The campaign moved on while this preview was open: review again.
                      setPreview(
                        await api<GoalPreview>(
                          "/v1/admin/optimization/preview",
                          "POST",
                          { conversion_goal: goal, window_days: days },
                        ),
                      );
                      notice(
                        "The campaign changed while you reviewed. Review the updated effect, then apply again.",
                      );
                    }
                  })
                }
              >
                Apply reviewed goal change
              </button>
              <button
                className="secondary"
                onClick={() => {
                  setPreview(null);
                  setGoal(current.conversion_goal);
                  setDays(current.window_days);
                }}
              >
                Cancel change
              </button>
            </div>
          </div>
        </section>
      )}
    </div>
  );
}

/** A draft of one part of the current calling policy, fenced to its version. */
function usePolicyEditor<T extends object>(pick: (policy: Policy) => T) {
  const { snapshot: w, api, refresh, notice } = useWorkspace();
  const [draft, setDraft] = useState<T>(() => pick(w.policy));
  const [reason, setReason] = useState("");
  const [conflict, setConflict] = useState(false);
  const [reloading, setReloading] = useState(false);
  const dirty = useRef(false);
  useEffect(() => {
    if (reloading || !dirty.current) {
      setDraft(pick(w.policy));
      dirty.current = false;
      setReloading(false);
    }
  }, [w.policy.id, w.policy.version, reloading]);
  const change = (patch: Partial<T>) => {
    dirty.current = true;
    setDraft((old) => ({ ...old, ...patch }));
  };
  const publish = async () => {
    try {
      await api(`/v1/admin/policies/${w.policy.id}/publish`, "POST", {
        ...draft,
        reason,
        expected_version: w.policy.version,
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) setConflict(true);
      throw error;
    }
    dirty.current = false;
    setConflict(false);
    setReason("");
    await refresh();
    notice("New policy version published. Queued calls will recheck it.");
  };
  const reload = async () => {
    setConflict(false);
    setReloading(true);
    await refresh();
  };
  return { draft, change, reason, setReason, conflict, publish, reload };
}

function PolicyConflict({ onReload }: { onReload: () => Promise<void> }) {
  const { run } = useWorkspace();
  return (
    <div className="alert" role="alert">
      A newer calling policy is current. Reload it, review your change, then
      publish again.
      <button onClick={() => void run(onReload)}>Reload calling policy</button>
    </div>
  );
}

const callingWindow = (policy: Policy) => ({
  local_start_hour: policy.local_start_hour,
  local_end_hour: policy.local_end_hour,
  max_attempts: policy.max_attempts,
  approved: policy.approved,
  review_reference: policy.review_reference || "",
});

function CallingWindow({
  bounds,
}: {
  bounds: ReturnType<typeof useDefinitions>;
}) {
  const { run } = useWorkspace();
  const editor = usePolicyEditor(callingWindow);
  const p = bounds?.policy;
  const { draft, change } = editor;
  return (
    <div className="setup-form">
      <div className="three-fields">
        <label>
          Local start hour
          <input
            type="number"
            {...limits(p?.local_start_hour)}
            value={draft.local_start_hour}
            onChange={(e) =>
              change({ local_start_hour: Number(e.target.value) })
            }
          />
        </label>
        <label>
          Local end hour
          <input
            type="number"
            {...limits(p?.local_end_hour)}
            value={draft.local_end_hour}
            onChange={(e) => change({ local_end_hour: Number(e.target.value) })}
          />
        </label>
        <label>
          Attempt limit
          <input
            type="number"
            {...limits(p?.max_attempts)}
            value={draft.max_attempts}
            onChange={(e) => change({ max_attempts: Number(e.target.value) })}
          />
        </label>
      </div>
      <p className="fine">
        Hours are the prospect's local time. National screening and internal
        opt-outs are always required; voicemail stays limited to the final
        permitted attempt.
      </p>
      <label className="check-label">
        <input
          type="checkbox"
          checked={draft.approved}
          onChange={(e) => change({ approved: e.target.checked })}
        />
        Campaign approved for live use
      </label>
      <label>
        Reviewed policy reference
        <input
          value={draft.review_reference}
          onChange={(e) => change({ review_reference: e.target.value })}
        />
      </label>
      <label>
        Publication reason
        <input
          value={editor.reason}
          onChange={(e) => editor.setReason(e.target.value)}
        />
      </label>
      <button
        className="primary"
        disabled={!editor.reason.trim()}
        onClick={() => void run(editor.publish)}
      >
        Publish calling window
      </button>
      {!editor.reason.trim() && (
        <p className="fine">A publication reason is required.</p>
      )}
      {editor.conflict && <PolicyConflict onReload={editor.reload} />}
    </div>
  );
}

function ScheduleStep({
  current,
  bounds,
  onSaved,
}: {
  current: OptimizationSettings;
  bounds: ReturnType<typeof useDefinitions>;
  onSaved: () => Promise<void>;
}) {
  const { api, run, notice, refresh } = useWorkspace();
  const pick = (value: OptimizationSettings): SimpleStaffing => ({
    staffing_timezone: value.staffing_timezone,
    staffing_start_hour: value.staffing_start_hour,
    staffing_end_hour: value.staffing_end_hour,
    staffing_reps: value.staffing_reps,
    staffing_weekdays: value.staffing_weekdays,
  });
  const [draft, setDraft] = useState(() => pick(current));
  const dirty = useRef(false);
  useEffect(() => {
    if (!dirty.current) setDraft(pick(current));
  }, [current]);
  return (
    <div className="setup-form">
      <p className="fine">
        When reps call, in one IANA time zone. Dated exceptions and named roster
        pools are under Advanced. Total available rep minutes still caps the
        plan.
      </p>
      <WeeklySchedule
        value={draft}
        bounds={bounds}
        onChange={(patch) => {
          dirty.current = true;
          setDraft((old) => ({ ...old, ...patch }));
        }}
      />
      <button
        className="primary"
        disabled={!draft.staffing_weekdays.length}
        onClick={() =>
          void run(async () => {
            await api("/v1/admin/optimization", "PUT", draft);
            dirty.current = false;
            await onSaved();
            await refresh();
            notice("Rep schedule saved");
          })
        }
      >
        Save rep schedule
      </button>
      {!draft.staffing_weekdays.length && (
        <p className="fine">Choose at least one staffed weekday.</p>
      )}
    </div>
  );
}

function BudgetStep({
  settings,
  bounds,
}: {
  settings: AdminSettingsState;
  bounds: ReturnType<typeof useDefinitions>;
}) {
  const { snapshot: w, api, run, notice, refresh } = useWorkspace();
  const [budget, setBudget] = useState<BudgetData | null>(
    settings.data?.budget ?? null,
  );
  const saved = JSON.stringify(settings.data?.budget ?? null);
  useEffect(() => {
    if (settings.data) setBudget(settings.data.budget);
  }, [saved]);
  if (!budget) return null;
  const b = bounds?.budget;
  return (
    <div className="setup-form">
      <div className="two-column">
        <label>
          Daily optional-work limit (USD)
          <input
            type="number"
            {...limits(b?.daily_limit_usd)}
            value={budget.daily_limit_usd}
            onChange={(e) =>
              setBudget({ ...budget, daily_limit_usd: Number(e.target.value) })
            }
          />
        </label>
        <label>
          Monthly limit (USD)
          <input
            type="number"
            {...limits(b?.monthly_limit_usd)}
            value={budget.monthly_limit_usd}
            onChange={(e) =>
              setBudget({
                ...budget,
                monthly_limit_usd: Number(e.target.value),
              })
            }
          />
        </label>
      </div>
      <p className="fine">
        Limits optional research, preparation and model requests. Hangup,
        opt-out, recovery and owed bookkeeping are never blocked. New number
        rentals are blocked by the no-subscription policy.
      </p>
      <button
        className="primary"
        onClick={() =>
          void run(async () => {
            await api("/v1/admin/budget", "PUT", budget);
            await settings.load();
            notice("Budget limits saved");
            await refresh();
          })
        }
      >
        Save budget limits
      </button>
      <p className="fine">
        Reserved usage: $
        {w.metrics.reserved_cost_usd == null
          ? "Unknown"
          : w.metrics.reserved_cost_usd.toFixed(2)}{" "}
        · Verified provider billing:{" "}
        {w.metrics.billing_verified ? "Reconciled" : "Not yet reconciled"}.
        Reserved amounts are internal controls; provider receipts establish
        billed costs.
      </p>
    </div>
  );
}

const treatments = (policy: Policy) => ({
  state_registry_required: policy.state_registry_required,
  permissions: policy.permissions,
  voicemail_enabled: policy.voicemail_enabled || false,
  voicemail_optout_supported: policy.voicemail_optout_supported || false,
  approved_assets: policy.approved_assets || [],
  detached_topology_verified: policy.detached_topology_verified || false,
  topology_test_reference: policy.topology_test_reference || "",
  late_human_recovery_destination: policy.late_human_recovery_destination || "",
});

function CallingPolicySettings() {
  const { run } = useWorkspace();
  const editor = usePolicyEditor(treatments);
  const { draft: policy, change } = editor;
  return (
    <section className="card padded">
      <span className="eyebrow">VERSIONED CAMPAIGN POLICY</span>
      <h2>Permission before every dial</h2>
      <p className="fine">
        National screening is always required. Calling hours, attempts and live
        approval are in Setup.
      </p>
      <label className="check-label">
        <input
          type="checkbox"
          checked={policy.state_registry_required}
          onChange={(e) =>
            change({ state_registry_required: e.target.checked })
          }
        />
        Require applicable state registry data
      </label>
      <h3>Separate treatment permissions</h3>
      {["live", "transcription", "prerecorded"].map((permission) => (
        <label className="check-label" key={permission}>
          <input
            type="checkbox"
            checked={policy.permissions.includes(permission)}
            onChange={(e) =>
              change({
                permissions: e.target.checked
                  ? [...policy.permissions, permission]
                  : policy.permissions.filter((p) => p !== permission),
              })
            }
          />
          {label(permission)}
        </label>
      ))}
      <h3>Approved voicemail assets</h3>
      <label className="check-label">
        <input
          type="checkbox"
          checked={policy.voicemail_enabled}
          onChange={(e) => change({ voicemail_enabled: e.target.checked })}
        />
        Enable permissioned voicemail
      </label>
      <label className="check-label">
        <input
          type="checkbox"
          checked={policy.voicemail_optout_supported}
          onChange={(e) =>
            change({ voicemail_optout_supported: e.target.checked })
          }
        />
        Reviewed message explains the digit 9 opt-out
      </label>
      <label>
        Approved Telnyx media names
        <input
          value={policy.approved_assets.join(", ")}
          onChange={(e) =>
            change({
              approved_assets: e.target.value
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
            })
          }
        />
      </label>
      <label className="check-label">
        <input
          type="checkbox"
          checked={policy.detached_topology_verified}
          onChange={(e) =>
            change({ detached_topology_verified: e.target.checked })
          }
        />
        Detached topology verified on the actual account
      </label>
      <label>
        Topology test reference
        <input
          value={policy.topology_test_reference}
          onChange={(e) => change({ topology_test_reference: e.target.value })}
        />
      </label>
      <label>
        Late human recovery destination
        <input
          value={policy.late_human_recovery_destination}
          onChange={(e) =>
            change({ late_human_recovery_destination: e.target.value })
          }
        />
      </label>
      <label>
        Publication reason
        <input
          value={editor.reason}
          onChange={(e) => editor.setReason(e.target.value)}
        />
      </label>
      <button
        className="primary"
        disabled={!editor.reason.trim()}
        onClick={() => void run(editor.publish)}
      >
        Publish policy version
        <ShieldCheck size={15} />
      </button>
      {!editor.reason.trim() && (
        <p className="fine">A publication reason is required.</p>
      )}
      {editor.conflict && <PolicyConflict onReload={editor.reload} />}
    </section>
  );
}
function RegistrySettings() {
  const { snapshot: w, api, run, refresh, notice } = useWorkspace();
  const history = useHistory("registries", w.registries);
  const [registry, setRegistry] = useState({
    numbers: "",
    npas: "",
    reference: "",
    refreshed_at: new Date().toISOString().slice(0, 10),
    kind: "national",
    jurisdiction: "",
    mode: "full",
    base_snapshot_id: "",
    authorization_expires_at: "",
    add: "",
    remove: "",
  });
  const [registryBusy, setRegistryBusy] = useState(false);
  const missing = !registry.npas
    ? "Enter the authorized area codes."
    : !registry.reference
      ? "Enter the authorization reference."
      : registry.mode === "changes" && !registry.base_snapshot_id
        ? "Choose the published snapshot these changes apply to."
        : "";
  return (
    <section className="card padded">
      <span className="eyebrow">AUTHORITATIVE DATA</span>
      <h2>Registry snapshots</h2>
      {history.items.map((r) => (
        <div className="registry-row" key={r.id}>
          <strong>
            {label(r.kind)}{" "}
            {r.synthetic && <span className="pill subtle">Synthetic</span>}
          </strong>
          <small>
            Updated {date(r.refreshed_at)} · {r.npas.join(", ")}
          </small>
          <small>SHA-256 {r.checksum.slice(0, 18)}…</small>
        </div>
      ))}
      <HistoryMore history={history} label="Load older registry snapshots" />
      <p className="muted small">
        Import seller-authorized data only. Missing or stale coverage blocks
        calling. Registry membership never enters scheduling features.
      </p>
      <label>
        Dataset import mode
        <select
          value={registry.mode}
          onChange={(e) => setRegistry({ ...registry, mode: e.target.value })}
          disabled={registryBusy}
        >
          <option value="full">Complete authorized snapshot</option>
          <option value="changes">Changes to a published snapshot</option>
        </select>
      </label>
      {registry.mode === "changes" && (
        <label>
          Base publication
          <select
            value={registry.base_snapshot_id}
            disabled={registryBusy}
            onChange={(e) => {
              const base = history.items.find((r) => r.id === e.target.value);
              setRegistry({
                ...registry,
                base_snapshot_id: e.target.value,
                ...(base
                  ? {
                      kind: base.kind,
                      jurisdiction: base.jurisdiction || "",
                      npas: base.npas.join(", "),
                      reference:
                        base.subscription_reference ||
                        base.subscription_id ||
                        "",
                      authorization_expires_at:
                        base.authorization_expires_at?.slice(0, 16) || "",
                    }
                  : {}),
              });
            }}
          >
            <option value="">Choose the current source</option>
            {history.items
              .filter((r) => r.published)
              .map((r) => (
                <option key={r.id} value={r.id}>
                  {label(r.kind)} · {r.npas.join(", ")} · version {r.version}
                </option>
              ))}
          </select>
        </label>
      )}
      <fieldset disabled={registryBusy} style={{ border: 0, padding: 0 }}>
        <label>
          Dataset type
          <select
            value={registry.kind}
            onChange={(e) => setRegistry({ ...registry, kind: e.target.value })}
          >
            <option value="national">National</option>
            <option value="state">State</option>
          </select>
        </label>
        {registry.kind === "state" && (
          <label>
            State jurisdiction
            <input
              value={registry.jurisdiction}
              onChange={(e) =>
                setRegistry({ ...registry, jurisdiction: e.target.value })
              }
            />
          </label>
        )}
        <label>
          Authorized area codes (comma separated)
          <input
            value={registry.npas}
            onChange={(e) => setRegistry({ ...registry, npas: e.target.value })}
          />
        </label>
        <label>
          Authorization reference
          <input
            value={registry.reference}
            onChange={(e) =>
              setRegistry({ ...registry, reference: e.target.value })
            }
          />
        </label>
        <label>
          Dataset refreshed on
          <input
            type="date"
            value={registry.refreshed_at}
            onChange={(e) =>
              setRegistry({ ...registry, refreshed_at: e.target.value })
            }
          />
        </label>
        <label>
          Source access expires at
          <input
            type="datetime-local"
            value={registry.authorization_expires_at}
            onChange={(e) =>
              setRegistry({
                ...registry,
                authorization_expires_at: e.target.value,
              })
            }
          />
        </label>
        <p className="fine">
          Source access time is UTC. An earlier access expiry always shortens
          the screening lifetime.
        </p>
        {registry.mode === "full" ? (
          <label>
            Listed E.164 numbers (one per line)
            <textarea
              rows={4}
              value={registry.numbers}
              onChange={(e) =>
                setRegistry({ ...registry, numbers: e.target.value })
              }
            />
          </label>
        ) : (
          <>
            <label>
              Added listed E.164 numbers
              <textarea
                value={registry.add}
                onChange={(e) =>
                  setRegistry({ ...registry, add: e.target.value })
                }
              />
            </label>
            <label>
              Removed listed E.164 numbers
              <textarea
                value={registry.remove}
                onChange={(e) =>
                  setRegistry({ ...registry, remove: e.target.value })
                }
              />
            </label>
          </>
        )}
        <button
          className="secondary"
          disabled={registryBusy || !!missing}
          onClick={() =>
            void run(async () => {
              setRegistryBusy(true);
              try {
                const base = history.items.find(
                  (r) => r.id === registry.base_snapshot_id,
                );
                const body = {
                  kind: registry.kind,
                  jurisdiction: registry.jurisdiction,
                  npas: registry.npas.split(",").map((s) => s.trim()),
                  subscription_reference: registry.reference,
                  refreshed_at: new Date(
                    registry.refreshed_at + "T00:00:00Z",
                  ).toISOString(),
                  ...(registry.authorization_expires_at
                    ? {
                        authorization_expires_at: new Date(
                          registry.authorization_expires_at + "Z",
                        ).toISOString(),
                      }
                    : {}),
                  ...(registry.mode === "full"
                    ? {
                        numbers: registry.numbers.split(/\s+/).filter(Boolean),
                      }
                    : {
                        base_snapshot_id: base?.id,
                        expected_version: base?.version,
                        add: registry.add.split(/\s+/).filter(Boolean),
                        remove: registry.remove.split(/\s+/).filter(Boolean),
                      }),
                };
                const digest = Array.from(
                  new Uint8Array(
                    await crypto.subtle.digest(
                      "SHA-256",
                      new TextEncoder().encode(JSON.stringify(body)),
                    ),
                  ),
                )
                  .map((b) => b.toString(16).padStart(2, "0"))
                  .join("");
                const key = "opendialer.registry-import." + w.seller.id;
                let previous: { digest?: string; request_id?: string } = {};
                try {
                  previous = JSON.parse(sessionStorage.getItem(key) || "{}");
                } catch {
                  /* Invalid metadata cannot authorize anything. */
                }
                const request_id =
                  previous.digest === digest && previous.request_id
                    ? previous.request_id
                    : crypto.randomUUID();
                // Retain only the request fingerprint, never registry membership, across reload.
                sessionStorage.setItem(
                  key,
                  JSON.stringify({ digest, request_id }),
                );
                const result = await api<{ published: boolean }>(
                  "/v1/admin/registry/import",
                  "POST",
                  { ...body, request_id },
                  { timeoutMs: LONG_REQUEST_MS },
                );
                await refresh();
                notice(
                  result.published
                    ? "Validated registry snapshot published atomically."
                    : "Original import recovered; a newer publication is current.",
                );
              } finally {
                setRegistryBusy(false);
              }
            })
          }
        >
          Validate &amp; publish dataset
        </button>
        {missing && <p className="fine">{missing}</p>}
      </fieldset>
      <p className="fine">
        If interrupted, resubmit the same source and fields to resume the
        original import. The current source remains active until all chunks
        validate. No source download or purchase runs here.
      </p>
    </section>
  );
}
function OperationsSettings({ onDisconnect }: { onDisconnect: () => void }) {
  const { api, run, refresh, notice } = useWorkspace();
  const [backupId, setBackupId] = useState("");
  const [restoreId, setRestoreId] = useState("");
  return (
    <>
      <WorkTime />
      <PreparationSettings />
      <CallbackRouting />
      <RubricSettings />
      <BillingReconciliation />
      <section className="card padded">
        <span className="eyebrow">RECOVERABLE BY DESIGN</span>
        <h2>Encrypted backups</h2>
        <p className="muted small">
          Restore recovers missing records and keeps current records and live
          obligations. It reconciles denial journals and pauses outbound work
          for review. Backups contain business records, never unencrypted
          provider credentials.
        </p>
        <button
          className="primary"
          onClick={() =>
            void run(async () => {
              const r = await api<{ backup_id: string }>(
                "/v1/admin/backup",
                "POST",
                undefined,
                { timeoutMs: LONG_REQUEST_MS },
              );
              setBackupId(r.backup_id);
              notice("Encrypted backup created");
            })
          }
        >
          <Database size={15} />
          Create encrypted backup
        </button>
        {backupId && <p className="mono small">Backup ID: {backupId}</p>}
        <label>
          Backup ID to restore
          <input
            value={restoreId}
            onChange={(e) => setRestoreId(e.target.value)}
          />
        </label>
        <button
          className="secondary"
          disabled={!restoreId}
          onClick={() =>
            void run(async () => {
              const restored = await api<{ recovered_records: number }>(
                "/v1/admin/restore",
                "POST",
                { backup_id: restoreId },
                { timeoutMs: LONG_REQUEST_MS },
              );
              await refresh();
              notice(
                `Recovered ${restored.recovered_records} missing records. Current records were kept. Outbound calling is paused.`,
              );
            })
          }
        >
          Restore selected backup
        </button>
        <div className="section-rule" />
        <button
          className="secondary"
          onClick={() =>
            void run(async () => {
              await api("/v1/admin/recovery", "POST");
              await refresh();
              notice("Recovery sweep completed");
            })
          }
        >
          <RefreshCw size={15} />
          Run recovery sweep
        </button>
      </section>
      <WorkspaceConnection onDisconnect={onDisconnect} />
      <AccessAndRecovery />
      <RecoveryPanel />
    </>
  );
}

function WorkspaceConnection({ onDisconnect }: { onDisconnect: () => void }) {
  const { snapshot: w } = useWorkspace();
  const reasons = w.quiescence?.reasons ?? [];
  return (
    <section className="card padded">
      <h2>Workspace connection</h2>
      <p className="muted">
        Current authority:{" "}
        {w.mode === "connected"
          ? "Authenticated shared server"
          : "Private authority on this computer"}
        .
      </p>
      <button
        className="secondary"
        disabled={w.quiescence?.can_disconnect === false}
        onClick={onDisconnect}
      >
        <Wifi size={15} />
        Switch workspace or server
      </button>
      {w.quiescence?.can_disconnect === false && (
        <p className="fine" role="status">
          Finish first:{" "}
          {reasons.length
            ? reasons.map(label).join(" · ")
            : "pending work on this authority"}
          .
        </p>
      )}
    </section>
  );
}

/** One collapsed view for model, experiment, routing and diagnostic detail. */
function AdvancedView({ settings }: { settings: AdminSettingsState }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="card padded advanced-view"
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>Advanced</summary>
      <p className="fine">
        Optional model routes, planning assumptions, experiments, trackers and
        diagnostics. The authority's defaults apply until you change them;
        changes are versioned and audited.
      </p>
      {open && (
        <>
          <AdvancedProviders settings={settings} />
          <LearningAdvanced />
          <Trackers />
        </>
      )}
    </details>
  );
}

function AdvancedProviders({ settings }: { settings: AdminSettingsState }) {
  const { snapshot: w, api, run, notice } = useWorkspace();
  const form = useProviderDraft(settings, [
    "coach_provider",
    "coach_model",
    "inference_mode",
    "inference_base_url",
    "model",
    "research_search",
    "script_write_model",
    "scorecard_review_model",
  ]);
  const { draft, setDraft } = form;
  const [tunnel, setTunnel] = useState<TunnelStatus | null>(null);
  const [busy, setBusy] = useState(false);
  // A hosted authority cannot receive the browser's loopback redirect: the user pastes it.
  const [planPaste, setPlanPaste] = useState(false);
  const [planAddress, setPlanAddress] = useState("");
  useEffect(() => {
    if (native() && w.mode === "local")
      void tunnelStatus()
        .then(setTunnel)
        .catch(() => {});
  }, [w.mode]);
  return (
    <section className="card padded" aria-label="Optional model routes">
      <h2>Optional model routes</h2>
      <label>
        Live coach provider
        <select
          value={draft.coach_provider || ""}
          onChange={(e) =>
            setDraft({ ...draft, coach_provider: e.target.value })
          }
        >
          <option value="openrouter">OpenRouter · Cerebras</option>
          <option value="static">Approved static guidance</option>
        </select>
      </label>
      {draft.coach_provider === "openrouter" && (
        <>
          {form.field("coach_model", "Cerebras coach model")}
          <p className="fine">
            Cerebras only · other-provider fallbacks disabled · strict
            structured responses. Endpoint support and live timing must pass
            your account checks.
          </p>
        </>
      )}
      <label>
        Preparation and analysis route
        <select
          value={draft.inference_mode || ""}
          onChange={(e) =>
            setDraft({ ...draft, inference_mode: e.target.value })
          }
        >
          <option value="api">OpenAI Responses API</option>
          <option value="litellm">
            Explicit OpenAI-compatible LiteLLM endpoint
          </option>
          <option value="chatgpt_plan">
            Official eligible ChatGPT plan usage
          </option>
        </select>
      </label>
      <CredentialButton
        field="openai_api_key"
        title={
          draft.inference_mode === "litellm"
            ? "LiteLLM API key"
            : "OpenAI API key"
        }
        settings={settings}
      />
      {draft.inference_mode === "litellm" &&
        form.field(
          "inference_base_url",
          "Inference base URL",
          "http://127.0.0.1:4000/v1",
        )}
      {draft.inference_mode === "chatgpt_plan" && (
        <>
          <p className="fine">
            Research and script writing use your own ChatGPT plan. Each user
            signs in for the work they queue; the live helper stays on Cerebras.
          </p>
          <button
            className="secondary"
            onClick={() =>
              void run(async () => {
                const flow = await api<{
                  authorization_url: string;
                  completion: "loopback" | "paste";
                }>("/v1/inference/chatgpt/start", "POST");
                await openSource(flow.authorization_url);
                setPlanPaste(flow.completion === "paste");
                notice(
                  flow.completion === "paste"
                    ? "Sign in in your browser, then paste the address it ends on here."
                    : "Complete the official authorization in your browser, then return here.",
                );
              })
            }
          >
            Continue with ChatGPT
          </button>
          {planPaste && (
            <>
              <label>
                Address shown after signing in
                <input
                  value={planAddress}
                  placeholder="http://127.0.0.1:1455/auth/callback?code=…"
                  onChange={(e) => setPlanAddress(e.target.value)}
                />
              </label>
              <button
                className="primary"
                disabled={!planAddress.trim()}
                onClick={() =>
                  void run(async () => {
                    await api("/v1/inference/chatgpt/complete", "POST", {
                      redirect_url: planAddress.trim(),
                    });
                    setPlanAddress("");
                    setPlanPaste(false);
                    notice("ChatGPT is connected to your OpenDialer user.");
                  })
                }
              >
                Finish ChatGPT sign-in
              </button>
            </>
          )}
        </>
      )}
      {form.field("model", "Default evaluated model")}
      <label>
        Optional research search
        <select
          aria-label="Optional research search"
          value={draft.research_search || "disabled"}
          onChange={(e) =>
            setDraft({ ...draft, research_search: e.target.value })
          }
        >
          <option value="disabled">Disabled</option>
          <option value="openai_web_search">Metered OpenAI web search</option>
        </select>
      </label>
      <p className="fine">
        At most two metered searches per company, within the preparation budget,
        when official pages lack useful context. Saving this choice makes no
        paid request.
      </p>
      {form.field("script_write_model", "Script-writing model")}
      {form.field("scorecard_review_model", "Optional stronger review model")}
      <div className="settings-save">
        <LockKeyhole size={16} />
        <span>
          Write-only secrets · encrypted at rest · no credentials in workspace
          snapshots
        </span>
        <button
          className="primary"
          disabled={!settings.data || form.busy}
          onClick={() =>
            void run(() => form.save("Provider configuration saved securely."))
          }
        >
          Save provider settings
        </button>
        <button
          className="secondary"
          disabled={form.busy}
          onClick={() =>
            void run(async () => {
              await settings.load();
              notice("Current provider settings loaded.");
            })
          }
        >
          Reload provider settings
        </button>
      </div>
      {tunnel && (
        <details>
          <summary>Existing SSH forwarding host</summary>
          <p>
            Use an owned host with the webhook-only Caddy recipe, a pinned SSH
            host key, and an existing SSH key or agent. Your local computer must
            stay awake. No service is installed.
          </p>
          <p role="status">Forward: {label(tunnel.state)}</p>
          <label className="check-label">
            <input
              type="checkbox"
              checked={tunnel.config.enabled}
              disabled={w.simulation || busy}
              onChange={(e) =>
                setTunnel({
                  ...tunnel,
                  config: { ...tunnel.config, enabled: e.target.checked },
                })
              }
            />
            Enable owned SSH forward
          </label>
          {(["host", "user"] as const).map((key) => (
            <label key={key}>
              SSH {key}
              <input
                value={tunnel.config[key]}
                disabled={busy}
                onChange={(e) =>
                  setTunnel({
                    ...tunnel,
                    config: { ...tunnel.config, [key]: e.target.value },
                  })
                }
              />
            </label>
          ))}
          <label>
            Remote loopback port
            <input
              type="number"
              min="1024"
              max="65535"
              value={tunnel.config.remote_port}
              disabled={busy}
              onChange={(e) =>
                setTunnel({
                  ...tunnel,
                  config: {
                    ...tunnel.config,
                    remote_port: Number(e.target.value),
                  },
                })
              }
            />
          </label>
          <button
            className="secondary"
            disabled={busy || w.simulation}
            onClick={() =>
              void run(async () => {
                setBusy(true);
                try {
                  await saveTunnel(tunnel.config);
                  setTunnel(await tunnelStatus());
                } finally {
                  setBusy(false);
                }
              })
            }
          >
            Save SSH forward
          </button>
          <button
            className="secondary"
            onClick={() =>
              void run(async () => setTunnel(await tunnelStatus()))
            }
          >
            Refresh forward status
          </button>
          {w.simulation && <p>Forwarding is unavailable in simulation.</p>}
        </details>
      )}
    </section>
  );
}

function UpdateSettings() {
  const { snapshot: w, session, api, run, notice } = useWorkspace();
  const [updateInfo, setUpdateInfo] = useState<{
    version: string;
    body: string;
  } | null>(null);
  const [updateBusy, setUpdateBusy] = useState(false);
  const update = useRef<Update | null>(null);
  return (
    <section className="card padded update-card">
      <span className="eyebrow">SAME APP. SAME DATA.</span>
      <h2>Updates without starting over</h2>
      <p className="muted">
        OpenDialer verifies signed update artifacts before replacing the
        installed app. Your workspace database and protected credentials stay in
        the application's data directory.
      </p>
      <div className="update-version">
        <span className="brand-symbol">
          <Download size={22} />
        </span>
        <div>
          <strong>OpenDialer {w.readiness.version}</strong>
          <small>
            {native()
              ? "Native desktop application"
              : "Browser development session"}
          </small>
        </div>
      </div>
      <button
        className="primary"
        disabled={updateBusy || !native()}
        onClick={() =>
          void run(async () => {
            setUpdateBusy(true);
            try {
              const { check } = await import("@tauri-apps/plugin-updater");
              update.current = await check();
              if (update.current)
                setUpdateInfo({
                  version: update.current.version,
                  body:
                    update.current.body ||
                    "A signed OpenDialer update is available.",
                });
              else notice("You are running the current release.");
            } finally {
              setUpdateBusy(false);
            }
          })
        }
      >
        {updateBusy ? (
          <LoaderCircle className="spin" size={16} />
        ) : (
          <RefreshCw size={16} />
        )}
        Check for updates
      </button>
      {!native() && (
        <p className="fine">
          Signed updates install only in the desktop application.
        </p>
      )}
      {updateInfo && (
        <div className="info-note">
          <h3>Version {updateInfo.version}</h3>
          <p>{updateInfo.body}</p>
          <button
            className="primary"
            disabled={updateBusy}
            onClick={() =>
              void run(async () => {
                await assertNoPendingOptouts(session.tenant_id, w.simulation);
                const status = await api<{
                  can_disconnect: boolean;
                  reasons: string[];
                }>("/v1/auth/quiescence");
                if (!status.can_disconnect)
                  throw new Error(
                    `Finish pending work before updating: ${status.reasons.join(", ")}`,
                  );
                setUpdateBusy(true);
                try {
                  await update.current?.downloadAndInstall();
                  notice(
                    "Update installed. Quit and reopen OpenDialer to use the new version.",
                  );
                } finally {
                  setUpdateBusy(false);
                }
              })
            }
          >
            <ArrowDownToLine size={16} />
            Install signed update
          </button>
        </div>
      )}
      <p className="fine">
        Local builds are validated on this Mac. Distribution signing,
        notarization, and other operating-system audio certification have
        separate release gates.
      </p>
    </section>
  );
}
