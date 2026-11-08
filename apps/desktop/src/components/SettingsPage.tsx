import { ManagedConnection, type ConnectionStatus } from "./ManagedConnection";
import { countLabel } from "../state/useHistory";
import { OperatingPolicySettings } from "./OperatingPolicySettings";
import { CallbackRouting } from "./CallbackRouting";
import { RecoveryPanel } from "./RecoveryPanel";
import { AccessAndRecovery } from "./AdminWorkflows";
import {
  LearningAdvanced,
  type OptimizationSettings,
} from "./OptimizationPanel";
import { CredentialField } from "./CredentialField";
import { CodexLogin, type LoginStatus as CodexLoginStatus } from "./CodexLogin";
import type { Update } from "@tauri-apps/plugin-updater";
import {
  ArrowDownToLine,
  Database,
  Download,
  LoaderCircle,
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
  assertNoPendingOptouts,
  type ProviderCredential,
} from "../api/client";
import { isAdmin, label, useWorkspace } from "../context";
import { limits, options, useDefinitions } from "../state/definitions";
import type { Policy, Readiness } from "../types";

type BudgetData = {
  per_list_usd: number;
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
type View = "Dialer" | "Leads" | "Review" | "Numbers" | "Script";
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
            <h1>Settings</h1>
            <p>Your workspace and app updates.</p>
          </div>
        </div>
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

const READINESS: Record<string, string> = {
  NO_CAMPAIGN: "Create a campaign.",
  RESTORE_RECONCILIATION_REQUIRED: "Finish checking the restored backup.",
  BUDGET_NOT_CONFIGURED: "Set an AI budget.",
  TELNYX_NOT_CONFIGURED: "Connect Telnyx under Calling.",
  AUDIO_SETUP_MISSING:
    "This computer has no calling audio login. Import your calling setup under Calling.",
  WEBHOOK_SIGNATURE_KEY_MISSING: "Import your calling setup under Calling.",
  MANAGED_CONNECTION_UNAVAILABLE:
    "Call events are reconnecting. Keep OpenDialer open; reconnect under Calling if it stays off.",
  NO_CALLER_NUMBER:
    "Add a caller number in Numbers: buy one, or add one you own in Telnyx.",
  CALLER_NUMBER_ROUTE_REQUIRED:
    "In Numbers, choose Set up route on your caller number.",
  CALLER_NUMBER_CALLBACK_TEST_REQUIRED:
    "One test call left: go ready, call one of your numbers from your own phone and answer it here. Every number opens after that.",
  CALLER_NUMBER_PAUSED:
    "Your caller numbers are paused. Review them in Numbers.",
  NO_LEADS: "Import a lead list.",
  TIMEZONE_UNCERTAIN:
    "Some leads have no usable state or time zone. Add a state column to the CSV.",
  INTERNAL_SUPPRESSION: "Every remaining lead is on your do-not-call list.",
  ATTEMPT_LIMIT: "Every remaining lead has used all its attempts.",
  OUTREACH_HOLD:
    "The remaining leads are on hold (booked, waiting or held by you).",
  REGISTRY_NOT_CLEARED_FOR_ENDPOINT:
    "The do-not-call check is on, but no current list covers these leads. Load one or turn the check off.",
  SCREENING_PENDING:
    "The do-not-call check is on, but no current list covers these leads. Load one or turn the check off.",
};

function AdminSettings({
  onDisconnect,
  onNavigate,
}: {
  onDisconnect: () => void;
  onNavigate: (view: View) => void;
}) {
  const { api, run, snapshot: w } = useWorkspace();
  const settings = useAdminSettings();
  // Null until the AI card has read the ChatGPT login.
  const [chatGpt, setChatGpt] = useState<boolean | null>(null);
  const [preflight, setPreflight] = useState<Readiness | null>(null);
  useEffect(() => setPreflight(null), [settings.data?.version]);
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Settings</h1>
          <p>Set these once. Everything else has sensible defaults.</p>
        </div>
        <button
          className="secondary"
          onClick={() =>
            void run(async () => setPreflight(await api("/v1/admin/preflight")))
          }
        >
          <ShieldCheck size={16} />
          Check readiness
        </button>
      </div>
      {preflight && (
        <section
          className={`card padded readiness ${preflight.ready_to_call ? "passed" : ""}`}
          aria-label="Readiness"
        >
          <h3>
            {preflight.ready_to_call
              ? "Ready to call"
              : `${preflight.blockers.length} thing${preflight.blockers.length === 1 ? "" : "s"} left before live calls`}
          </h3>
          <ul>
            {preflight.blockers.map((b) => (
              <li key={b}>{READINESS[b] ?? label(b)}</li>
            ))}
          </ul>
          {preflight.warnings.length > 0 && (
            <details>
              <summary>Notes</summary>
              {preflight.warnings.map((warning) => (
                <p className="small" key={warning}>
                  {warning}
                </p>
              ))}
            </details>
          )}
        </section>
      )}
      {!settings.data ? (
        <section className="card padded">
          <p role="status">
            {settings.error
              ? `Settings could not be loaded: ${settings.error}`
              : "Loading settings…"}
          </p>
          {settings.error && (
            <button
              className="secondary"
              onClick={() => void settings.load().catch(() => {})}
            >
              Try again
            </button>
          )}
        </section>
      ) : (
        <div className="settings-grid">
          <SettingCard
            title="Calling"
            done={!!settings.data.configured.telnyx_api_key}
            summary={
              settings.data.configured.telnyx_api_key
                ? "Telnyx is connected."
                : "Add your Telnyx API key."
            }
          >
            <TelnyxStep settings={settings} />
          </SettingCard>
          <SettingCard
            title="AI"
            done={aiDone(settings.data, chatGpt)}
            summary="ChatGPT researches leads and writes scripts. A Cerebras key powers live help during calls."
            open
          >
            <AiCard settings={settings} onConnected={setChatGpt} />
          </SettingCard>
          <SettingCard
            title="Script"
            summary="Paste your script once; every lead gets its own version."
            open
          >
            <button className="secondary" onClick={() => onNavigate("Script")}>
              Open Script
            </button>
          </SettingCard>
          <SettingCard
            title="Leads"
            done={(w.pages?.leads?.total ?? w.leads.length) > 0}
            summary={`${countLabel(w.pages?.leads?.total ?? w.leads.length, w.pages?.leads?.total_capped)} leads imported.`}
            open
          >
            <button className="secondary" onClick={() => onNavigate("Leads")}>
              Open Leads
            </button>
          </SettingCard>
          <SettingCard
            title="Calling hours"
            summary={`${w.policy.local_start_hour}:00 to ${w.policy.local_end_hour}:00 in each lead's time zone · up to ${w.policy.max_attempts} attempts · retries at least a day apart`}
          >
            <CallingWindow />
          </SettingCard>
          <SettingCard
            title="Do-not-call check"
            summary={
              w.policy.dnc_screening
                ? "On: leads are checked against the national do-not-call list before calls. Your own opt-outs always apply."
                : "Optional and off: leads are not looked up. Your own opt-outs always apply."
            }
          >
            <ScreeningChoice />
          </SettingCard>
        </div>
      )}
      <MoreSettings settings={settings} onDisconnect={onDisconnect} />
    </>
  );
}

/** Research has its ChatGPT login and live help has its key. */
function aiDone(data: SettingsData, chatGpt: boolean | null) {
  const route = chatGpt;
  return route === null
    ? undefined
    : route && !!data.configured.openrouter_api_key;
}

function SettingCard({
  title,
  done,
  summary,
  open = false,
  children,
}: {
  title: string;
  done?: boolean;
  summary: string;
  open?: boolean;
  children: React.ReactNode;
}) {
  return (
    <details
      className="card setting-card"
      open={open}
      data-testid={`setting-${title}`}
    >
      <summary>
        <span>
          <strong>{title}</strong>
          <small>{summary}</small>
        </span>
        {done !== undefined && (
          <span className={`pill ${done ? "green" : "amber"}`}>
            {done ? "Done" : "To do"}
          </span>
        )}
      </summary>
      <div className="setting-body">{children}</div>
    </details>
  );
}

const screeningChoice = (policy: Policy) => ({
  dnc_screening: !!policy.dnc_screening,
});

function ScreeningChoice() {
  const { run } = useWorkspace();
  const editor = usePolicyEditor(screeningChoice);
  return (
    <div className="setup-form">
      <label className="check-label">
        <input
          type="checkbox"
          checked={editor.draft.dnc_screening}
          onChange={(e) => editor.change({ dnc_screening: e.target.checked })}
        />
        Check leads against the national do-not-call list before calling
      </label>
      <button className="secondary" onClick={() => void run(editor.publish)}>
        Save
      </button>
      {editor.conflict && <PolicyConflict onReload={editor.reload} />}
    </div>
  );
}

type LoginStatus = CodexLoginStatus & { computer_login?: boolean };

/** ChatGPT for research and scripts; a Cerebras key for live help. */
function AiCard({
  settings,
  onConnected,
}: {
  settings: AdminSettingsState;
  onConnected: (connected: boolean) => void;
}) {
  const { api, run, notice } = useWorkspace();
  const [login, setLogin] = useState<LoginStatus | null>(null);
  const [busy, setBusy] = useState(false);
  // A status read that started before an import cannot overwrite its result.
  const generation = useRef(0);
  const load = useCallback(async () => {
    const current = ++generation.current;
    const value = await api<LoginStatus>("/v1/inference/codex");
    if (current === generation.current) setLogin(value);
  }, [api]);
  const accept = (value: LoginStatus) => {
    generation.current++;
    setLogin(value);
  };
  useEffect(() => {
    if (login) onConnected(login.connected);
  }, [login, onConnected]);
  useEffect(() => {
    void load().catch(() => {});
    // Coming back from the browser or Codex refreshes both the login and the saved route.
    const returned = () => {
      void load().catch(() => {});
      void settings.load().catch(() => {});
    };
    window.addEventListener("focus", returned);
    return () => window.removeEventListener("focus", returned);
  }, [load, settings.load]);
  return (
    <div className="stack">
      <div className="ai-row">
        <div>
          <strong>ChatGPT</strong>
          <small>
            {login?.connected
              ? `Connected${login.account?.email ? ` as ${login.account.email}` : ""}${login.account?.planType ? ` · ${login.account.planType} plan` : ""}. Research and scripts use GPT-6.1 Terra at maximum effort, included in your plan.`
              : "Not connected. Leads are called with your script as written until it is."}
          </small>
        </div>
        {login?.connected ? (
          <span className="pill green">Connected</span>
        ) : login?.computer_login ? (
          <button
            className="primary"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                setBusy(true);
                try {
                  accept(
                    await api<LoginStatus>(
                      "/v1/inference/codex/import-computer",
                      "POST",
                      undefined,
                      { timeoutMs: LONG_REQUEST_MS },
                    ),
                  );
                  notice("ChatGPT connected.");
                } finally {
                  setBusy(false);
                }
              })
            }
          >
            {busy ? "Connecting…" : "Use the ChatGPT login on this computer"}
          </button>
        ) : null}
      </div>
      {!login?.connected && !login?.computer_login && (
        <CodexLogin
          compact
          onImported={async (value) => {
            accept(value);
          }}
        />
      )}
      {login?.connected && (
        <button
          className="text-button"
          onClick={() =>
            void run(async () => {
              setLogin(await api<LoginStatus>("/v1/inference/codex", "DELETE"));
              notice("ChatGPT disconnected.");
            })
          }
        >
          Disconnect ChatGPT
        </button>
      )}
      <CredentialButton
        field="openrouter_api_key"
        title="Live help key (OpenRouter, Cerebras)"
        settings={settings}
      />
    </div>
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
  const { notice, refresh } = useWorkspace();
  const data = settings.data;
  return (
    <CredentialField
      field={field}
      title={title}
      version={data?.version}
      configured={data?.configured[field]}
      onConflict={settings.load}
      onSaved={(result) => {
        if (data)
          settings.setData({
            ...data,
            version: result.version,
            configured: { ...data.configured, ...result.configured },
          });
        notice("Credential saved in the selected authority.");
        void refresh();
      }}
    />
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
  const server = useRef<Record<string, string>>(draft);
  useEffect(() => {
    // Newly read settings refresh only the fields the person has not changed;
    // a choice made while the first read was in flight is kept, not reset.
    const previous = server.current;
    const next = read();
    server.current = next;
    setDraft((current) =>
      Object.fromEntries(
        keys.map((key) => [
          key,
          current[key] !== previous[key] ? current[key] : next[key],
        ]),
      ),
    );
    edit.current = null;
  }, [data?.version]);
  const save = async (done: string, extra: Record<string, string> = {}) => {
    if (!data) return;
    const values = {
      ...Object.fromEntries(
        Object.entries(draft).filter(
          ([key, value]) => value !== server.current[key],
        ),
      ),
      ...extra,
    };
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
      server.current = { ...draft };
      edit.current = null;
      notice(done);
      await refresh();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409)
        await settings.load();
      throw error;
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
  const { api, run, runtime } = useWorkspace();
  const [managed, setManaged] = useState<ConnectionStatus | null>(null);
  const configured = managed?.configured;
  const managedChanged = useCallback(
    (value: ConnectionStatus) => {
      setManaged((previous) => {
        if (JSON.stringify(previous) === JSON.stringify(value)) return previous;
        return value;
      });
      if (value.configured) void settings.load();
    },
    [settings.load],
  );
  const [key, setKey] = useState("");
  const form = useProviderDraft(settings, [
    "telnyx_connection_id",
    "telnyx_public_key",
    "webhook_url",
    "telephony_credential_id",
    "sip_username",
  ]);
  const [ingress, setIngress] = useState<Record<string, unknown> | null>(null);
  return (
    <>
      {runtime?.local_token && !runtime.simulation && (
        <ManagedConnection changed={managedChanged} />
      )}
      <p className="fine">
        In the{" "}
        <button
          className="text-button"
          onClick={() =>
            void run(() => openSource("https://portal.telnyx.com/"))
          }
        >
          Telnyx portal
        </button>
        : Account settings → API Keys → Create API Key.
      </p>
      <label>
        Telnyx API key
        <input
          type="password"
          autoComplete="off"
          value={key}
          disabled={form.busy}
          placeholder={
            settings.data?.configured.telnyx_api_key
              ? "Saved; enter only to replace"
              : "Paste your API v2 key"
          }
          onChange={(e) => setKey(e.target.value)}
        />
      </label>

      {!configured && (
        <>
          {form.field("telnyx_connection_id", "Voice API connection ID")}
          {form.field("telnyx_public_key", "Webhook public key")}
          {form.field(
            "webhook_url",
            "Webhook address",
            "https://your-endpoint/webhooks/telnyx",
          )}
        </>
      )}
      {!configured && (
        <details>
          <summary>Browser calling credential</summary>
          {form.field(
            "telephony_credential_id",
            "Scoped telephony credential ID",
          )}
          {form.field("sip_username", "Agent SIP username")}
        </details>
      )}
      <div className="button-row">
        <button
          className="primary"
          disabled={!settings.data || form.busy}
          onClick={() =>
            void run(async () => {
              await form.save(
                "Telnyx settings saved.",
                key.trim() ? { telnyx_api_key: key.trim() } : {},
              );
              setKey("");
            })
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
          Check connection
        </button>
      </div>
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
        reason: reason.trim() || "Edited in Settings",
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
    notice("Saved.");
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
      Someone else changed this. Reload, then make your change again.
      <button onClick={() => void run(onReload)}>Reload</button>
    </div>
  );
}

const callingWindow = (policy: Policy) => ({
  local_start_hour: policy.local_start_hour,
  local_end_hour: policy.local_end_hour,
  max_attempts: policy.max_attempts,
});

function CallingWindow() {
  const { run } = useWorkspace();
  const bounds = useDefinitions();
  const editor = usePolicyEditor(callingWindow);
  const p = bounds?.policy;
  const { draft, change } = editor;
  return (
    <div className="setup-form">
      <div className="three-fields">
        <label>
          Start (hour)
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
          End (hour)
          <input
            type="number"
            {...limits(p?.local_end_hour)}
            max={17}
            value={draft.local_end_hour}
            onChange={(e) => change({ local_end_hour: Number(e.target.value) })}
          />
        </label>
        <label>
          Attempts per lead
          <input
            type="number"
            {...limits(p?.max_attempts)}
            value={draft.max_attempts}
            onChange={(e) => change({ max_attempts: Number(e.target.value) })}
          />
        </label>
      </div>
      <button className="primary" onClick={() => void run(editor.publish)}>
        Save calling hours
      </button>
      {editor.conflict && <PolicyConflict onReload={editor.reload} />}
    </div>
  );
}

function BudgetSettings({ settings }: { settings: AdminSettingsState }) {
  const { api, run, notice } = useWorkspace();
  const [value, setValue] = useState(settings.data?.budget.per_list_usd ?? 25);
  useEffect(() => {
    if (settings.data) setValue(settings.data.budget.per_list_usd);
  }, [settings.data?.budget.per_list_usd]);
  if (!settings.data) return null;
  const budget = settings.data.budget;
  return (
    <div className="setup-form">
      <label>
        AI budget per lead list (USD)
        <input
          type="number"
          min={0}
          step="1"
          value={value}
          onChange={(e) => setValue(Number(e.target.value))}
        />
      </label>
      <p className="fine">
        Covers research, scripts, live help and call notes for each list. On
        ChatGPT, research and scripts are included in your plan and cost $0
        here. Calling is never blocked by this budget.
      </p>
      <button
        className="secondary"
        onClick={() =>
          void run(async () => {
            await api("/v1/admin/budget", "PUT", {
              ...budget,
              per_list_usd: value,
            });
            await settings.load();
            notice("Budget saved.");
          })
        }
      >
        Save budget
      </button>
    </div>
  );
}

const treatments = (policy: Policy) => ({
  state_registry_required: policy.state_registry_required,
  permissions: policy.permissions,
  voicemail_enabled: policy.voicemail_enabled || false,
  voicemail_optout_supported: policy.voicemail_optout_supported || false,
  approved_assets: policy.approved_assets || [],
});

function CallingPolicySettings() {
  const { run } = useWorkspace();
  const editor = usePolicyEditor(treatments);
  const { draft: policy, change } = editor;
  return (
    <section className="card padded">
      <h3>Extra checks before each call</h3>
      <label className="check-label">
        <input
          type="checkbox"
          checked={policy.state_registry_required}
          onChange={(e) =>
            change({ state_registry_required: e.target.checked })
          }
        />
        Also check state do-not-call lists
      </label>
      <p className="fine">Allowed during calls:</p>
      <label className="check-label">
        <input
          type="checkbox"
          checked={policy.permissions.includes("live")}
          onChange={(e) =>
            change({
              permissions: [
                ...policy.permissions.filter(
                  (p) => p !== "live" && p !== "transcription",
                ),
                ...(e.target.checked ? ["live", "transcription"] : []),
              ],
            })
          }
        />
        Live transcription and help
      </label>
      <button className="secondary" onClick={() => void run(editor.publish)}>
        Save checks
      </button>
      {editor.conflict && <PolicyConflict onReload={editor.reload} />}
    </section>
  );
}
function MoreSettings({
  settings,
  onDisconnect,
}: {
  settings: AdminSettingsState;
  onDisconnect: () => void;
}) {
  const { api, run, refresh, notice } = useWorkspace();
  const [backupId, setBackupId] = useState("");
  const [restoreId, setRestoreId] = useState("");
  const [open, setOpen] = useState(false);
  const bounds = useDefinitions();
  return (
    <details
      className="card padded more-settings"
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>More settings</summary>
      {open && (
        <div className="more-list">
          <details>
            <summary>AI budget</summary>
            <BudgetSettings settings={settings} />
          </details>
          <details>
            <summary>Campaign goal</summary>
            {settings.data && (
              <>
                <CampaignGoal
                  current={settings.data.optimization}
                  bounds={bounds}
                  onSaved={settings.load}
                />
              </>
            )}
          </details>
          <details>
            <summary>Do-not-call checks and consent</summary>
            <CallingPolicySettings />
          </details>
          <details>
            <summary>Privacy and spending rules</summary>
            <OperatingPolicySettings />
          </details>
          <details>
            <summary>Callbacks</summary>
            <CallbackRouting />
          </details>
          <details>
            <summary>Learning</summary>
            <LearningAdvanced />
          </details>
          <details>
            <summary>Users, backups and recovery</summary>
            <div className="stack">
              <h3>Backups</h3>
              <p className="muted small">
                Restore recovers missing records and keeps current records and
                live obligations, then pauses outbound work until carrier
                reconciliation. Backups contain business records, not provider
                credentials.
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
                    notice("Backup created");
                  })
                }
              >
                <Database size={15} />
                Create backup
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
            </div>
            <AccessAndRecovery />
            <RecoveryPanel />
          </details>
          <details>
            <summary>Workspace and app updates</summary>
            <WorkspaceConnection onDisconnect={onDisconnect} />
            <UpdateSettings />
          </details>
        </div>
      )}
    </details>
  );
}
function WorkspaceConnection({ onDisconnect }: { onDisconnect: () => void }) {
  const { snapshot: w } = useWorkspace();
  const reasons = w.quiescence?.reasons ?? [];
  return (
    <section className="card padded">
      <h2>Workspace connection</h2>
      <p className="muted">Current authority: private, on this computer.</p>
      <button
        className="secondary"
        disabled={w.quiescence?.can_disconnect === false}
        onClick={onDisconnect}
      >
        <Wifi size={15} />
        Switch workspace
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
      <span className="eyebrow">SIGNED APP UPDATES</span>
      <h2>Review and install updates</h2>
      <p className="muted">
        OpenDialer verifies signed update artifacts before replacing the
        installed app. Version 0.4.0 starts a fresh workspace when updating from
        0.2 or 0.3; old data stays untouched in its previous folder and is not
        migrated. Review the release notes before installing.
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
                assertNoPendingOptouts(session.tenant_id);
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
