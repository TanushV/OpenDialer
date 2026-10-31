import { ManagedConnection, type ConnectionStatus } from "./ManagedConnection";
import { countLabel, useHistory } from "../state/useHistory";
import { HistoryMore } from "./HistoryMore";
import { BillingReconciliation } from "./BillingReconciliation";
import { OperatingPolicySettings } from "./OperatingPolicySettings";
import { WorkTime } from "./WorkTime";
import { CallbackRouting } from "./CallbackRouting";
import { RecoveryPanel } from "./RecoveryPanel";
import { RubricSettings } from "./Rubrics";
import { AccessAndRecovery } from "./AdminWorkflows";
import { Trackers } from "./AdvancedIntelligence";
import {
  LearningAdvanced,
  type OptimizationSettings,
} from "./OptimizationPanel";
import { WeeklySchedule, type SimpleStaffing } from "./Staffing";
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
  tunnelStatus,
  saveTunnel,
  assertNoPendingOptouts,
  type TunnelStatus,
  type ProviderCredential,
} from "../api/client";
import { date, isAdmin, label, useWorkspace } from "../context";
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
            <p>Your working time, workspace and app updates.</p>
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

const READINESS: Record<string, string> = {
  POLICY_NOT_PUBLISHED: "Approve your calling hours for live calls.",
  SELLER_NOT_APPROVED: "Confirm your business is allowed to make these calls.",
  NO_CAMPAIGN: "Create a campaign.",
  RESTORE_RECONCILIATION_REQUIRED: "Finish checking the restored backup.",
  BUDGET_NOT_CONFIGURED: "Set an AI budget.",
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
            done={w.policy.approved}
            summary={`${w.policy.local_start_hour}:00 to ${w.policy.local_end_hour}:00 in each lead's time zone · up to ${w.policy.max_attempts} attempts${w.policy.approved ? "" : " · not approved for live calls yet"}`}
          >
            <CallingWindow />
            <BusinessConfirmation />
          </SettingCard>
          <SettingCard
            title="Do-not-call list"
            done={w.registries.some(
              (r) => r.kind === "national" && r.published,
            )}
            summary={
              w.registries.some((r) => r.kind === "national" && r.published)
                ? "A national do-not-call list is loaded. Your own opt-outs always apply."
                : "Load the national do-not-call list for your area codes before live calls."
            }
          >
            <RegistrySettings />
          </SettingCard>
        </div>
      )}
      <MoreSettings settings={settings} onDisconnect={onDisconnect} />
    </>
  );
}

/** Research has a route (ChatGPT or an API key) and live help has its key. */
function aiDone(data: SettingsData, chatGpt: boolean | null) {
  const route =
    data.effective.inference_mode === "codex"
      ? chatGpt
      : !!data.configured.openai_api_key;
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

function BusinessConfirmation() {
  const { snapshot: w, api, run, refresh, notice } = useWorkspace();
  if (w.seller.approved)
    return <p className="fine">Business confirmed for calling.</p>;
  return (
    <button
      className="secondary"
      onClick={() =>
        void run(async () => {
          await api("/v1/admin/seller/approve", "POST", {
            identity_reference: `Confirmed in Settings by ${w.user.name}`,
          });
          await refresh();
          notice("Business confirmed.");
        })
      }
    >
      Confirm our business is allowed to call these leads
    </button>
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
  const mode = settings.data?.effective.inference_mode;
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
  const useChatGpt = async () => {
    if (mode !== "codex" && settings.data)
      settings.setData(
        await api<SettingsData>("/v1/admin/settings", "PUT", {
          inference_mode: "codex",
          request_id: crypto.randomUUID(),
          expected_version: settings.data.version,
        }),
      );
  };
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
                  await useChatGpt();
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
            await useChatGpt();
          }}
        />
      )}
      {login?.connected && mode !== "codex" && (
        <button className="secondary" onClick={() => void run(useChatGpt)}>
          Use ChatGPT for research and scripts
        </button>
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
      <details>
        <summary>Use an OpenAI API key instead of ChatGPT</summary>
        <AdvancedProviders settings={settings} />
      </details>
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
    "traffic_approval_reference",
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
          <LocalWebhookForward />
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
          {form.field(
            "traffic_approval_reference",
            "Carrier traffic approval reference",
          )}
        </details>
      )}
      {configured &&
        form.field(
          "traffic_approval_reference",
          "Carrier traffic approval reference",
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
  approved: policy.approved,
  review_reference: policy.review_reference || "",
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
      <label className="check-label">
        <input
          type="checkbox"
          checked={draft.approved}
          onChange={(e) => change({ approved: e.target.checked })}
        />
        Approved for live calls
      </label>
      <button className="primary" onClick={() => void run(editor.publish)}>
        Save calling hours
      </button>
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
      {["live", "transcription"].map((permission) => (
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
      <button className="secondary" onClick={() => void run(editor.publish)}>
        Save checks
      </button>
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
      <h3>Loaded lists</h3>
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
        Paste the numbers from your do-not-call download for the area codes you
        call. Leads in area codes without a current list are not called.
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
            <summary>Campaign goal and team schedule</summary>
            {settings.data && (
              <>
                <CampaignGoal
                  current={settings.data.optimization}
                  bounds={bounds}
                  onSaved={settings.load}
                />
                <ScheduleStep
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
            <summary>Call scoring</summary>
            <RubricSettings />
          </details>
          <details>
            <summary>Work time</summary>
            <WorkTime />
          </details>
          <details>
            <summary>Billing records</summary>
            <BillingReconciliation />
          </details>
          <details>
            <summary>Learning experiments and trackers</summary>
            <LearningAdvanced />
            <Trackers />
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

function AdvancedProviders({ settings }: { settings: AdminSettingsState }) {
  const { run } = useWorkspace();
  const form = useProviderDraft(settings, [
    "inference_mode",
    "inference_base_url",
    "model",
  ]);
  const { draft, setDraft } = form;
  return (
    <div className="stack" aria-label="AI route">
      <label>
        Research and scripts use
        <select
          value={draft.inference_mode || "codex"}
          onChange={(e) =>
            setDraft({ ...draft, inference_mode: e.target.value })
          }
        >
          <option value="codex">ChatGPT (included in your plan)</option>
          <option value="api">
            OpenAI API key (billed per use, with web search)
          </option>
          <option value="litellm">OpenAI-compatible endpoint</option>
        </select>
      </label>
      {draft.inference_mode && draft.inference_mode !== "codex" && (
        <>
          <CredentialButton
            field="openai_api_key"
            title={
              draft.inference_mode === "litellm"
                ? "Endpoint API key"
                : "OpenAI API key"
            }
            settings={settings}
          />
          {draft.inference_mode === "litellm" &&
            form.field(
              "inference_base_url",
              "Endpoint address",
              "http://127.0.0.1:4000/v1",
            )}
          {form.field("model", "Model")}
        </>
      )}
      <button
        className="secondary"
        disabled={!settings.data || form.busy}
        onClick={() => void run(() => form.save("AI route saved."))}
      >
        Save AI route
      </button>
    </div>
  );
}

function LocalWebhookForward() {
  const { snapshot: w, run } = useWorkspace();
  const [tunnel, setTunnel] = useState<TunnelStatus | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (native() && w.mode === "local")
      void tunnelStatus()
        .then(setTunnel)
        .catch(() => {});
  }, [w.mode]);
  return (
    <>
      {" "}
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
    </>
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
