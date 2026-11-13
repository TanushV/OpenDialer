import { ManagedConnection, type ConnectionStatus } from "./ManagedConnection";
import { OperatingPolicySettings } from "./OperatingPolicySettings";
import { CallbackRouting } from "./CallbackRouting";
import { RecoveryPanel } from "./RecoveryPanel";
import { AccessAndRecovery } from "./AdminWorkflows";
import {
  LearningAdvanced,
  type OptimizationSettings,
} from "./OptimizationPanel";
import { CodexLogin, type LoginStatus as CodexLoginStatus } from "./CodexLogin";
import type { Update } from "@tauri-apps/plugin-updater";
import {
  ArrowDownToLine,
  Database,
  Download,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  LONG_REQUEST_MS,
  native,
  assertNoPendingOptouts,
} from "../api/client";
import { label, useWorkspace } from "../context";
import { useNewestAnswer } from "../state/newest";
import { limits, options, useDefinitions } from "../state/definitions";
import type { Policy } from "../types";
import { SetupRows } from "../settings/SetupRows";

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
  onNavigate,
}: {
  onNavigate: (view: View) => void;
}) {
  return <AdminSettings onNavigate={onNavigate} />;
}

/** One settings read for the admin page; failures stay visible and retryable. */
function useAdminSettings() {
  const { api, subscribe } = useWorkspace();
  const [data, setData] = useState<SettingsData | null>(null);
  const [error, setError] = useState("");
  // A slow older read (failed or not) never undoes a newer answer, e.g. after Try again.
  const newest = useNewestAnswer();
  const load = useCallback(async () => {
    const ticket = newest.begin();
    try {
      const found = await api<SettingsData>("/v1/admin/settings");
      if (!newest.accept(ticket)) return;
      setData(found);
      setError("");
    } catch (reason) {
      if (newest.accept(ticket)) setError(message(reason));
      throw reason;
    }
  }, [api, newest]);
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

function AdminSettings({ onNavigate }: { onNavigate: (view: View) => void }) {
  const settings = useAdminSettings();
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Settings</h1>
          <p>Set these once. Everything else has sensible defaults.</p>
        </div>
      </div>
      <SetupRows
        version={settings.data?.version}
        testSession={Boolean(settings.data?.test_session)}
        onTestCall={() => onNavigate("Dialer")}
        reload={settings.load}
        extras={{
          connection: <ConnectionRow settings={settings} />,
          chatgpt: <ChatGptLogin settings={settings} />,
          hours: <CallingWindow />,
        }}
      />
      {!settings.data && (
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
      )}
      <MoreSettings settings={settings} />
    </>
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
/** The ChatGPT login, in its Settings row: research and scripts use the plan of the login on this computer. */
function ChatGptLogin({ settings }: { settings: AdminSettingsState }) {
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
              : "Connect your own ChatGPT login or use OpenRouter for script generation."}
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
      {!login?.connected && (
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
    </div>
  );
}

/** The call connection row: it reconnects by itself; the setup-file import shows only while disconnected (R7.4). */
function ConnectionRow({ settings }: { settings: AdminSettingsState }) {
  const { runtime } = useWorkspace();
  const changed = useCallback(
    (value: ConnectionStatus) => {
      if (value.configured) void settings.load();
    },
    [settings.load],
  );
  if (!runtime?.local_token || runtime.simulation) return null;
  return <ManagedConnection changed={changed} />;
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
function MoreSettings({ settings }: { settings: AdminSettingsState }) {
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
      <summary>Advanced</summary>
      {open && (
        <div className="more-list">
          <details>
            {/* R7.6: only what software could not settle by itself. */}
            <summary>Needs attention</summary>
            <RecoveryPanel />
          </details>
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
            <ScreeningChoice />
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
            <summary>Number limits</summary>
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
          </details>
          <details>
            <summary>App updates</summary>
            <UpdateSettings />
          </details>
        </div>
      )}
    </details>
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
                }>("/v1/quiescence");
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
