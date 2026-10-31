import { useCallback, useEffect, useState } from "react";
import { Bot, Pause, Play } from "lucide-react";
import { date, isAdmin, useWorkspace } from "../context";

interface ListProgress {
  list_name: string;
  total: number;
  ready: number;
  skipped: number;
  failed: number;
  working: number;
  waiting: number;
  spent_usd: number;
  budget_usd: number;
  skipped_leads: { name: string; reason: string }[];
}
export interface PreparationStatus {
  lists: ListProgress[];
  threads: { id: string; status: string; leads: number }[];
  paused_until: string | null;
  pause_reason: string | null;
  settings: { leads_per_agent: number; agents_at_once: number };
  has_script: boolean;
  ai_connected: boolean;
  callable: number;
}

const money = (value: number) => `$${value.toFixed(2)}`;

/** Research and per-lead scripts run before calling; this shows how far each list has got. */
export function PreparationPanel({
  onNavigate,
}: {
  onNavigate: (view: "Script" | "Settings") => void;
}) {
  const { api, run, notice, subscribe, snapshot } = useWorkspace();
  const admin = isAdmin(snapshot.user.role);
  const [status, setStatus] = useState<PreparationStatus | null>(null);
  const [draft, setDraft] = useState<PreparationStatus["settings"] | null>(
    null,
  );
  const load = useCallback(async () => {
    const next = await api<PreparationStatus>("/v1/preparation");
    setStatus(next);
    setDraft((old) => old ?? next.settings);
  }, [api]);
  // An import changes the lead count before any agent reports back, which can take minutes.
  const leadCount = snapshot.pages?.leads?.total ?? snapshot.leads.length;
  useEffect(() => {
    void load().catch(() => {});
  }, [load, leadCount]);
  useEffect(
    () =>
      subscribe((events) => {
        if (events.some((e) => e.type.startsWith("lead.")))
          void load().catch(() => {});
      }),
    [load, subscribe],
  );
  const busy = status?.lists.some((l) => l.working || l.waiting);
  useEffect(() => {
    if (!busy) return;
    const timer = window.setInterval(() => void load().catch(() => {}), 8000);
    return () => window.clearInterval(timer);
  }, [busy, load]);
  if (!status) return null;
  const lists = status.lists;
  return (
    <section className="card padded prep-panel" aria-label="Lead preparation">
      <div className="section-heading">
        <div>
          <h2>Research and scripts</h2>
          <p className="muted small">
            Before a lead can be called, an AI agent researches it and writes
            its script from yours.
          </p>
        </div>
        {status.threads.length > 0 && (
          <span className="pill green">
            <Bot size={13} /> {status.threads.length} agent
            {status.threads.length === 1 ? "" : "s"} working
          </span>
        )}
      </div>
      {!status.has_script && (
        <div className="info-note">
          <p>
            Add your script first. Agents write each lead's version from it.
          </p>
          <button className="secondary" onClick={() => onNavigate("Script")}>
            Open Script
          </button>
        </div>
      )}
      {status.has_script && !status.ai_connected && (
        <div className="info-note">
          <p>
            Connect ChatGPT in Settings so agents can research your leads. Until
            then, leads are called with your script as written.
          </p>
          <button className="secondary" onClick={() => onNavigate("Settings")}>
            Open Settings
          </button>
        </div>
      )}
      {status.paused_until && (
        <div className="alert" role="status">
          <Pause size={15} />
          <span>
            Paused: {status.pause_reason} Resumes {date(status.paused_until)}.
          </span>
          {admin && (
            <button
              className="secondary"
              onClick={() =>
                void run(async () => {
                  await api("/v1/preparation/resume", "POST");
                  await load();
                  notice("Preparation resumed.");
                })
              }
            >
              <Play size={14} /> Resume now
            </button>
          )}
        </div>
      )}
      {lists.length === 0 ? (
        <p className="muted">
          Import a list below. Its leads are researched and scripted first.
        </p>
      ) : (
        <ul className="prep-lists">
          {lists.map((list) => {
            const done = list.ready + list.skipped + list.failed;
            const percent = list.total
              ? Math.round((done / list.total) * 100)
              : 0;
            return (
              <li key={list.list_name}>
                <div className="prep-row">
                  <strong>{list.list_name || "Unnamed list"}</strong>
                  <span className="fine">
                    {done} of {list.total} ready to call
                  </span>
                </div>
                <div
                  className="progress"
                  role="progressbar"
                  aria-valuenow={percent}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-label={`${list.list_name || "Unnamed list"} preparation`}
                >
                  <span style={{ width: `${percent}%` }} />
                </div>
                <p className="fine">
                  {list.ready} with their own script
                  {list.skipped
                    ? ` · ${list.skipped} skipped by the agent`
                    : ""}
                  {list.failed
                    ? ` · ${list.failed} using your base script`
                    : ""}
                  {list.working ? ` · ${list.working} in progress` : ""}
                  {list.waiting ? ` · ${list.waiting} waiting` : ""}
                  {list.spent_usd > 0
                    ? ` · ${money(list.spent_usd)} of ${money(list.budget_usd)} AI budget`
                    : ""}
                </p>
                {list.skipped_leads?.length > 0 && (
                  <details className="prep-skipped">
                    <summary>
                      Why{" "}
                      {list.skipped === 1
                        ? "one lead was"
                        : `${list.skipped} leads were`}{" "}
                      skipped
                    </summary>
                    <p className="fine">
                      Skipped leads are called with your script as written.
                    </p>
                    <ul>
                      {list.skipped_leads.map((lead, index) => (
                        <li key={index}>
                          <strong>{lead.name || "Unnamed lead"}</strong>{" "}
                          {lead.reason}
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
                {admin && list.waiting > 0 && (
                  <button
                    className="text-button"
                    onClick={() =>
                      void run(async () => {
                        const result = await api<{ released: number }>(
                          "/v1/preparation/lists/release",
                          "POST",
                          { list_name: list.list_name },
                        );
                        await load();
                        notice(
                          `${result.released} leads can be called now with your script as written.`,
                        );
                      })
                    }
                  >
                    Call the waiting leads now with the base script
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {admin && draft && (
        <details className="prep-settings">
          <summary>Agent settings</summary>
          <div className="two-fields">
            <label>
              Leads per agent
              <input
                type="number"
                min={1}
                max={20}
                value={draft.leads_per_agent}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    leads_per_agent: Number(e.target.value),
                  })
                }
              />
            </label>
            <label>
              Agents at once
              <input
                type="number"
                min={1}
                max={4}
                value={draft.agents_at_once}
                onChange={(e) =>
                  setDraft({ ...draft, agents_at_once: Number(e.target.value) })
                }
              />
            </label>
          </div>
          <p className="fine">
            Contacts at the same company go to the same agent so research is
            shared.
          </p>
          <button
            className="secondary"
            onClick={() =>
              void run(async () => {
                await api("/v1/admin/preparation", "PUT", draft);
                await load();
                notice("Agent settings saved.");
              })
            }
          >
            Save agent settings
          </button>
        </details>
      )}
    </section>
  );
}
