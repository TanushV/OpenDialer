import { NumberPurchasing } from "./NumberPurchasing";
import { ProductKnowledge } from "./ProductKnowledge";
import {
  ArrowRight,
  ChevronRight,
  Layers3,
  Plus,
  Radio,
  RefreshCw,
  Search,
  ShieldCheck,
  Upload,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useHistory } from "../state/useHistory";
import { limits, useDefinitions } from "../state/definitions";
import { HistoryMore } from "./HistoryMore";
import { LONG_REQUEST_MS, openSource } from "../api/client";
import { date, isAdmin, label, percent, useWorkspace } from "../context";
import { parseCSV } from "../state/csv";
import type { Lead, NumberRecord, Packet } from "../types";
import { ProspectBrief } from "./ProspectBrief";
import { PreparationPanel } from "./PreparationPanel";
import { ConversationLearning } from "./ConversationLearning";
import { ScriptEditor } from "./ScriptEditor";
import { ScriptMemory } from "./ScriptMemory";
import { ScriptReader } from "./ScriptReader";
import { OptimizationPanel } from "./OptimizationPanel";

export function LeadsPage({
  onNavigate,
}: {
  onNavigate: (view: "Script" | "Settings") => void;
}) {
  const { snapshot: w, api, run, refresh, notice } = useWorkspace();
  const admin = isAdmin(w.user.role);
  const [csv, setCSV] = useState("");
  const [listName, setListName] = useState("");
  const [query, setQuery] = useState("");
  const [report, setReport] = useState<{
    imported: number;
    duplicates: number;
    invalid: number;
  } | null>(null);
  const [packet, setPacket] = useState<Packet | null>(null);
  const history = useHistory("leads", w.leads);
  const leads = history.items;
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Leads</h1>
          <p>
            Import a list. Each lead is researched and gets its own script
            before it is called.
          </p>
        </div>
      </div>
      <PreparationPanel onNavigate={onNavigate} />
      <section className="card table-card">
        <div className="table-toolbar">
          <h3>
            {history.totalLabel ?? w.leads.length} leads{" "}
            <span className="subtle-count">
              {w.metrics.suppressed ?? "Unknown"} suppressed
            </span>
          </h3>
          <div className="search">
            <Search size={16} />
            <input
              aria-label="Search leads"
              placeholder="Find a person or company"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <button
              disabled={history.busy}
              onClick={() => void run(() => history.load(false, query))}
            >
              Search lead history
            </button>
          </div>
        </div>
        {history.query && history.cursor && (
          <p role="status">More records remain to search.</p>
        )}
        <table>
          <thead>
            <tr>
              <th>PERSON / COMPANY</th>
              <th>ENDPOINT</th>
              <th>STATUS</th>
              <th>COVERAGE DUE</th>
              <th>ATTEMPTS</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {leads.map((lead) => (
              <tr key={lead.id}>
                <td>
                  <strong>{lead.name}</strong>
                  <small>{lead.company || "Company not provided"}</small>
                  {lead.list_name && <small>{lead.list_name}</small>}
                </td>
                <td>
                  {lead.phone}
                  <small>{lead.timezone || "Timezone needs review"}</small>
                  {lead.state && (
                    <small>
                      {lead.state}
                      {lead.state_source === "provided"
                        ? ""
                        : " · estimated from phone"}
                    </small>
                  )}
                </td>
                <td>
                  <span
                    className={`pill ${lead.suppressed ? "red" : lead.status === "preparing" ? "amber" : lead.outreach_hold ? "subtle" : "green"}`}
                  >
                    {lead.suppressed
                      ? "Suppressed"
                      : lead.status === "preparing"
                        ? "Being prepared"
                        : lead.outreach_hold === "company_booked"
                          ? "Company booked"
                          : label(lead.status)}
                  </span>
                </td>
                <td>{date(lead.coverage_due_at)}</td>
                <td>{lead.attempt_count}</td>
                <td>
                  <button
                    className="icon-button"
                    aria-label={`Open lead ${lead.name}`}
                    onClick={() =>
                      void run(async () =>
                        setPacket(await api(`/v1/leads/${lead.id}/packet`)),
                      )
                    }
                  >
                    <ChevronRight size={18} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!leads.length && !history.cursor && (
          <Empty
            text={
              history.query
                ? "No matching leads."
                : "Your next cohort starts with an import."
            }
          />
        )}
        <HistoryMore
          history={history}
          label={history.query ? "Continue lead search" : "Load more leads"}
        />
      </section>
      {admin && (
        <section className="card import-card">
          <div>
            <h2>Import a list</h2>
            <p className="muted">
              A CSV with <code>name</code> and <code>phone</code> columns.{" "}
              <code>company</code>, <code>domain</code>, <code>timezone</code>{" "}
              and <code>state</code> help, and every other column is used as
              research context. Opted-out numbers stay blocked.
            </p>
          </div>
          <div>
            <label>
              List name
              <input
                value={listName}
                maxLength={120}
                onChange={(e) => setListName(e.target.value)}
                placeholder="e.g. California agencies"
              />
            </label>
            <label>
              CSV lead data
              <textarea
                rows={5}
                value={csv}
                onChange={(e) => setCSV(e.target.value)}
                placeholder={
                  "name,phone,company,timezone\nJordan Example,+14155550101,Example Co,America/Los_Angeles"
                }
              />
            </label>
            <div className="button-row">
              <label className="secondary file-button">
                <Upload size={15} />
                Choose CSV file
                <input
                  type="file"
                  accept=".csv,text/csv"
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) {
                      setListName(
                        file.name.replace(/\.csv$/i, "").slice(0, 120),
                      );
                      void file.text().then(setCSV);
                    }
                  }}
                />
              </label>
              <button
                className="primary"
                disabled={!csv.trim()}
                onClick={() =>
                  void run(async () => {
                    const report = await api<{
                      imported: number;
                      duplicates: number;
                      invalid: number;
                      preparing: number;
                    }>(
                      "/v1/leads/import",
                      "POST",
                      {
                        campaign_id: w.campaign.id,
                        list_name: listName.trim(),
                        rows: parseCSV(csv),
                        prepare: true,
                      },
                      { timeoutMs: LONG_REQUEST_MS },
                    );
                    setReport(report);
                    setCSV("");
                    await refresh();
                    const count = `${report.imported} lead${report.imported === 1 ? "" : "s"} imported.`;
                    notice(
                      report.preparing
                        ? `${count} Agents are researching them now.`
                        : count,
                    );
                  })
                }
              >
                Import leads
                <ArrowRight size={15} />
              </button>
            </div>
            {report && (
              <p className="small">
                {report.imported} imported · {report.duplicates} already in the
                list · {report.invalid} without a valid phone number
              </p>
            )}
          </div>
        </section>
      )}
      {packet && (
        <div className="modal-shade">
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label="Lead packet"
          >
            <button className="close-modal" onClick={() => setPacket(null)}>
              Close
            </button>
            <h2>{packet.lead.name}</h2>
            <p>
              {packet.lead.company} · {packet.lead.phone}
            </p>
            <div className="section-rule" />
            <h3>Script for this lead</h3>
            <p className="fine">
              {packet.script.fallback
                ? packet.lead.status === "preparing"
                  ? "Being prepared. This is your script as written until the agent finishes."
                  : "Your script as written."
                : (packet.script.summary ?? "Written from research.")}
            </p>
            <ScriptReader script={packet.script} />
            <h3>What the agent found</h3>
            {!packet.research.facts.length && (
              <p className="muted small">No researched facts yet.</p>
            )}
            {packet.research.facts.map((f) => (
              <p key={f.id}>
                {f.statement}
                <button
                  className="source-link"
                  onClick={() => void openSource(f.source_url)}
                >
                  Source ↗
                </button>
              </p>
            ))}
            <ProspectBrief packet={packet} onChange={setPacket} />
            <h3>Previous outcomes</h3>
            {packet.previous_calls.map((c) => (
              <p key={c.id}>
                {date(c.started_at)} · {c.outcome || c.state}
              </p>
            ))}
            {admin && (
              <>
                <h3>Endpoint correction</h3>
                <p className="muted small">
                  Wrong-party endpoints stay suppressed. Attach a verified
                  replacement endpoint to continue this lead.
                </p>
                <EndpointCorrection
                  lead={packet.lead}
                  onDone={() => setPacket(null)}
                />
                {packet.lead.suppressed && (
                  <HoldRelease
                    lead={packet.lead}
                    onDone={() => setPacket(null)}
                  />
                )}
              </>
            )}
          </section>
        </div>
      )}
    </>
  );
}
/** Release a temporary safety hold after review; permanent opt-outs stay. */
function HoldRelease({ lead, onDone }: { lead: Lead; onDone: () => void }) {
  const { api, run, refresh, notice } = useWorkspace();
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  return (
    <div>
      <h3>Temporary safety hold</h3>
      <p className="muted small">
        Calls to this endpoint are blocked. A temporary hold, such as an
        unconfirmed request to stop calls, can be released after review. Opt-
        outs and wrong-party denials stay permanent and cannot be released.
      </p>
      <label>
        Hold review reason
        <input
          value={reason}
          maxLength={500}
          onChange={(event) => {
            setReason(event.target.value);
            setConfirming(false);
          }}
        />
      </label>
      {confirming ? (
        <div className="button-row">
          <p>
            Release the hold on {lead.phone}? Calling resumes only within
            policy.
          </p>
          <button
            className="secondary"
            onClick={() =>
              void run(async () => {
                await api("/v1/admin/suppressions/release-hold", "POST", {
                  phone: lead.phone,
                  reason: reason.trim(),
                });
                await refresh();
                onDone();
                notice("Temporary hold released. The reason is recorded.");
              }).finally(() => setConfirming(false))
            }
          >
            Confirm release
          </button>
          <button className="text-button" onClick={() => setConfirming(false)}>
            Keep hold
          </button>
        </div>
      ) : (
        <button
          className="secondary"
          disabled={reason.trim().length < 3}
          onClick={() => setConfirming(true)}
        >
          Release temporary hold
        </button>
      )}
    </div>
  );
}
function EndpointCorrection({
  lead,
  onDone,
}: {
  lead: Lead;
  onDone: () => void;
}) {
  const { api, run, refresh, notice } = useWorkspace();
  const [phone, setPhone] = useState("");
  const [evidence, setEvidence] = useState("");
  return (
    <div>
      <label>
        Verified replacement phone
        <input value={phone} onChange={(e) => setPhone(e.target.value)} />
      </label>
      <label>
        Evidence reference
        <input value={evidence} onChange={(e) => setEvidence(e.target.value)} />
      </label>
      <button
        className="secondary"
        disabled={!phone || !evidence}
        onClick={() =>
          void run(async () => {
            await api(`/v1/leads/${lead.id}/endpoint`, "PATCH", {
              phone,
              evidence,
            });
            await refresh();
            onDone();
            notice(
              "Replacement endpoint saved. Previous suppressions retained.",
            );
          })
        }
      >
        Save corrected endpoint
      </button>
    </div>
  );
}

interface RetryCapacity {
  status: string;
  planned_attempts?: number;
  existing_capacity?: number;
  expanded_capacity?: number;
  additional_numbers?: number | null;
  unserved_attempts?: number;
  minimum_proven_on_grid?: boolean;
}
/** The saved capacity plan, read from the same projection the optimization report uses. */
interface Forecast {
  list_name?: string | null;
  estimated_state_leads?: number;
  forecast_attempts?: number;
  existing_only_capacity?: number;
  capacity_shortfall?: number | null;
  staffing_shortfall: number | null;
  number_shortfall: number | null;
  minimum_additional_numbers?: number | null;
  best_found_additional_numbers?: number;
  monthly_rental_estimate_usd?: number | null;
  owner_unavailable_attempts?: number | null;
  unavailable_staff_count?: number;
  occupied_rep_count?: number;
  binding_shortfall?: number | null;
  setup_shortfall?: number | null;
  planning_limited?: boolean;
  optimality?: string;
  optimality_scope?: string;
  schedule_scope?: string;
  existing_plus_addition_capacity?: number;
  conditional_pending_schedule?: unknown[];
  contingent_capacity?: {
    status: string;
    low?: RetryCapacity;
    mean?: RetryCapacity;
    high?: RetryCapacity;
  };
  contingent_retry_low_mean_high?: {
    low: number;
    mean: number;
    high: number;
    support: number;
    omitted_leads: number;
  };
  expansion_review?: {
    status: string;
    reason: string;
    next_review_at: string | null;
  };
  assumptions?: {
    staffing_grid_seconds: number;
    handling_seconds: number;
    account_channel_limit: number | null;
    additional_ready_after_hours: number;
  };
  screening?: {
    cleared_count: number;
    blocked_count: number;
    blocked_reasons: Record<string, number>;
    as_of: string;
  };
  regions: {
    region: string;
    state_name?: string;
    preferred_metros?: string;
    suggested_area_codes?: string[];
    forecast_attempts?: number;
    planned_attempts?: number;
    usable_existing_capacity?: number;
    general_fallback_capacity?: number;
    capacity_waiting_for_review?: number;
    proposed_additional: number | null;
  }[];
  projection: {
    status: "pending" | "stale" | "ready";
    as_of?: string | null;
    job_id?: string | null;
    refresh_status?: string | null;
    refresh_error?: string | null;
    attempts?: number | null;
  };
}
interface NumbersSummary {
  lists?: string[];
  states?: { code: string; name: string }[];
  inventory: {
    total: number;
    ready: number;
    pending_setup: number;
    limited_use: number;
    paused_outbound: number;
    investigating: number;
  };
  stable_assignments: number;
  forecast: Forecast;
}
/** Poll a queued plan with growing delays; never re-request planning in a loop. */
const BACKOFF_MS = [1000, 2000, 4000, 8000, 15000, 30000];
const MAX_CHECKS = 12;

function useNumbersSummary(listName: string | null) {
  const { api, subscribe } = useWorkspace();
  const scope = useRef({ api, listName });
  scope.current = { api, listName };
  const [summary, setSummary] = useState<NumbersSummary | null>(null);
  const [error, setError] = useState("");
  const [checks, setChecks] = useState(0);
  const version = useRef(0);
  const load = useCallback(async () => {
    if (scope.current.api !== api || scope.current.listName !== listName)
      return;
    const current = ++version.current;
    try {
      const value = await api<NumbersSummary>(
        "/v1/admin/numbers/summary" +
          (listName === null
            ? ""
            : `?list_name=${encodeURIComponent(listName)}`),
      );
      if (
        current === version.current &&
        scope.current.api === api &&
        scope.current.listName === listName
      ) {
        setSummary(value);
        setError("");
      }
    } catch (reason) {
      if (
        current === version.current &&
        scope.current.api === api &&
        scope.current.listName === listName
      )
        setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [api, listName]);
  useEffect(() => {
    setSummary(null);
    setChecks(0);
    void load();
    const stop = subscribe((events) => {
      if (
        events.some((e) =>
          [
            "optimization.updated",
            "number.updated",
            "planning.changed",
          ].includes(e.type),
        )
      )
        void load();
    });
    return () => {
      stop();
      version.current++;
    };
  }, [load, subscribe]);
  const projection = summary?.forecast.projection;
  const waiting =
    !!projection &&
    projection.status !== "ready" &&
    ["queued", "running"].includes(projection.refresh_status ?? "");
  useEffect(() => {
    if (!waiting || checks >= MAX_CHECKS) return;
    const timer = setTimeout(
      () => {
        setChecks((n) => n + 1);
        void load();
      },
      BACKOFF_MS[Math.min(checks, BACKOFF_MS.length - 1)],
    );
    return () => clearTimeout(timer);
  }, [waiting, checks, load, summary]);
  return {
    summary,
    error,
    load,
    waiting,
    exhausted: waiting && checks >= MAX_CHECKS,
    restart: () => setChecks(0),
  };
}

export function NumbersPage() {
  const { snapshot: w, api, run, refresh, notice } = useWorkspace();
  const history = useHistory("numbers", w.numbers);
  const [listName, setListName] = useState<string | null>(null);
  const [stateSearch, setStateSearch] = useState<{
    state: string;
    revision: number;
  } | null>(null);
  const plan = useNumbersSummary(listName);
  const inventory = plan.summary?.inventory;
  const [planning, setPlanning] = useState(false);
  const [phone, setPhone] = useState("");
  const requestPlan = async () => {
    setPlanning(true);
    try {
      // One explicit request queues (or reuses) the plan; reads then back off.
      await api("/v1/admin/numbers/proposal", "POST", {
        campaign_id: w.campaign.id,
        list_name: listName,
      });
      plan.restart();
      await plan.load();
    } finally {
      setPlanning(false);
    }
  };
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">A CONSISTENT CALLING IDENTITY</span>
          <h1>Numbers &amp; callbacks</h1>
          <p>
            Calling capacity adapts to staffing and observed delivery, up to
            each number’s reviewed daily maximum.
          </p>
          <p>
            Use owned numbers, keep familiar identities, and stay reachable.
          </p>
        </div>
        <button
          className="primary"
          disabled={planning}
          onClick={() => void run(requestPlan)}
        >
          <Layers3 size={17} />
          Plan state coverage
        </button>
      </div>
      <label className="card padded">
        Plan numbers for list
        <select
          value={listName === null ? "__all_lists__" : listName}
          onChange={(e) =>
            setListName(
              e.target.value === "__all_lists__" ? null : e.target.value,
            )
          }
        >
          <option value="__all_lists__">All campaign leads</option>
          {(plan.summary?.lists ?? []).map((name) => (
            <option key={name} value={name}>
              {name || "Unnamed imports"}
            </option>
          ))}
        </select>
      </label>
      <div className="number-summary">
        <div className="card">
          <Radio size={23} />
          <strong>{inventory?.total ?? "…"}</strong>
          <p>Owned numbers in inventory</p>
        </div>
        <div className="card">
          <ShieldCheck size={23} />
          <strong>{inventory?.ready ?? "…"}</strong>
          <p>Ready for outbound use</p>
        </div>
        <div className="card">
          <RefreshCw size={23} />
          <strong>{plan.summary?.stable_assignments ?? "…"}</strong>
          <p>Leads with a stable caller identity</p>
        </div>
      </div>
      <section className="card padded" aria-label="Inventory status">
        {plan.error && !plan.summary ? (
          <>
            <p role="status">Inventory totals are unavailable: {plan.error}</p>
            <button className="secondary" onClick={() => void plan.load()}>
              Retry inventory totals
            </button>
          </>
        ) : (
          <dl className="capacity-summary">
            {(
              [
                ["Ready", inventory?.ready],
                ["Pending setup", inventory?.pending_setup],
                ["Limited use", inventory?.limited_use],
                ["Paused outbound", inventory?.paused_outbound],
                ["Under investigation", inventory?.investigating],
              ] as const
            ).map(([title, value]) => (
              <div key={title}>
                <dt>{title}</dt>
                <dd>{value ?? "…"}</dd>
              </div>
            ))}
          </dl>
        )}
        <p className="fine">
          Counts cover your whole inventory. Ready may include reviewed limited
          use. Inbound identities remain retained.
        </p>
      </section>
      <NumberPurchasing
        states={plan.summary?.states ?? []}
        listName={listName}
        stateSearch={stateSearch}
        onChanged={async () => {
          await refresh();
          await history.load();
          await plan.load();
        }}
      />
      <section className="card table-card">
        <div className="table-toolbar">
          <h3>Your number inventory</h3>
          <button
            className="secondary small"
            onClick={() =>
              void run(async () => {
                const r = await api<{
                  added: number;
                  updated: number;
                  missing: number;
                }>("/v1/admin/numbers/sync", "POST");
                await refresh();
                await plan.load();
                notice(
                  `${r.added} added, ${r.updated} updated, ${r.missing} missing at provider. Existing identities retained.`,
                );
              })
            }
          >
            <RefreshCw size={14} />
            Sync existing Telnyx numbers
          </button>
        </div>
        <table>
          <thead>
            <tr>
              <th>CALLER ID</th>
              <th>REGION</th>
              <th>LIFECYCLE</th>
              <th>DAILY LIMIT</th>
              <th>CAPACITY</th>
              <th>ACTIONS</th>
            </tr>
          </thead>
          <tbody>
            {history.items.map((number) => (
              <NumberRow
                key={number.id}
                number={number}
                onSaved={(saved) => {
                  history.update(saved);
                  void plan.load();
                }}
              />
            ))}
          </tbody>
        </table>
        <HistoryMore history={history} label="Load more numbers" />
        {!history.items.length && (
          <Empty text="Connect Telnyx to sync your existing owned numbers." />
        )}
      </section>
      <section className="card import-card">
        <div>
          <span className="eyebrow">LEGITIMATE WARMUP</span>
          <h2>Start small. Stay reachable.</h2>
          <p className="muted">
            Owned inventory moves through identity setup, a real inbound
            callback test, and limited legitimate use. New rentals require an
            exact quote and your approval.
          </p>
          <div className="lifecycle">
            Purchased
            <ChevronRight size={12} />
            Identity
            <ChevronRight size={12} />
            Callback tested
            <ChevronRight size={12} />
            Limited use
            <ChevronRight size={12} />
            Healthy
          </div>
        </div>
        <div>
          <label>
            Existing owned phone number
            <input
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              placeholder="+14155550100"
            />
          </label>
          <button
            className="secondary"
            disabled={!phone}
            onClick={() =>
              void run(async () => {
                await api("/v1/admin/numbers", "POST", { phone });
                await refresh();
                await plan.load();
                setPhone("");
                notice(
                  "Number added in purchased state. Complete its onboarding evidence.",
                );
              })
            }
          >
            <Plus size={16} />
            Add owned number
          </button>
        </div>
      </section>
      <CapacityPlan
        state={plan}
        planning={planning}
        onPlan={() => void run(requestPlan)}
        onFindState={(state) =>
          setStateSearch((current) => ({
            state,
            revision: (current?.revision ?? 0) + 1,
          }))
        }
      />
    </>
  );
}

function CapacityPlan({
  state,
  planning,
  onPlan,
  onFindState,
}: {
  state: ReturnType<typeof useNumbersSummary>;
  planning: boolean;
  onPlan: () => void;
  onFindState: (state: string) => void;
}) {
  const { api, run } = useWorkspace();
  const forecast = state.summary?.forecast;
  if (!forecast) return null;
  const projection = forecast.projection;
  const queued = ["queued", "running"].includes(
    projection.refresh_status ?? "",
  );
  const computed = forecast.forecast_attempts !== undefined;
  const status = (
    <section className="card padded" aria-label="Capacity planning status">
      <p role="status">
        {projection.refresh_status === "dead"
          ? "Capacity planning failed. Retry the saved job after reviewing its error."
          : projection.status === "ready"
            ? "Capacity plan is current."
            : queued
              ? projection.status === "pending"
                ? "Capacity plan is being prepared in the background."
                : "Showing a saved capacity plan while current inputs are recalculated."
              : projection.status === "pending"
                ? "No capacity plan yet. Plan state coverage to prepare one."
                : "Capacity plan is out of date. Plan state coverage before acting on it."}
      </p>
      {projection.as_of && (
        <p className="fine">Calculated {date(projection.as_of)}.</p>
      )}
      {projection.refresh_error && (
        <p className="notice">{projection.refresh_error}</p>
      )}
      {state.exhausted && (
        <p className="fine">
          Still preparing. Automatic checks have stopped; check again later.
        </p>
      )}
      {(state.exhausted || projection.status !== "ready") && (
        <button
          className="secondary"
          disabled={planning}
          onClick={() =>
            void run(async () => {
              state.restart();
              await state.load();
            })
          }
        >
          Check capacity plan
        </button>
      )}
      {projection.refresh_status === "dead" && projection.job_id && (
        <button
          className="secondary"
          disabled={planning}
          onClick={() =>
            void run(async () => {
              await api(
                `/v1/admin/recovery/jobs/${projection.job_id}/retry`,
                "POST",
                { expected_attempts: projection.attempts },
              );
              state.restart();
              await state.load();
            })
          }
        >
          Retry capacity planning
        </button>
      )}
      {!computed && projection.status === "pending" && !queued && (
        <button className="secondary" disabled={planning} onClick={onPlan}>
          Prepare a capacity plan
        </button>
      )}
    </section>
  );
  if (!computed) return status;
  const minimum =
    forecast.minimum_additional_numbers ??
    forecast.best_found_additional_numbers;
  return (
    <>
      {status}
      <section
        className="card proposal"
        data-testid="capacity-proposal"
        aria-label="Capacity before procurement"
      >
        <span className="eyebrow">PLAN BEFORE PROCUREMENT</span>
        <h2>State coverage proposal</h2>
        <p>
          {forecast.list_name === null || forecast.list_name === undefined
            ? "All campaign leads"
            : forecast.list_name || "Unnamed imports"}
          . Owned numbers are shared across lists; this preview does not reserve
          them. Keep existing caller identities and reuse state capacity before
          adding numbers.
        </p>
        {!!forecast.estimated_state_leads && (
          <p className="fine">
            {forecast.estimated_state_leads} leads use estimated or unknown
            states. Phone area codes do not confirm where someone lives. Metro
            suggestions are preferences, not a prediction of better pickup.
          </p>
        )}
        <div className="optimization-table">
          <table>
            <thead>
              <tr>
                <th>State</th>
                <th>Due attempts</th>
                <th>Rep capacity allocated</th>
                <th>Existing capacity</th>
                <th>General fallback</th>
                <th>Awaiting review</th>
                <th>Additional proposed</th>
                <th>Preferred metros</th>
              </tr>
            </thead>
            <tbody>
              {forecast.regions.map((r) => (
                <tr key={r.region}>
                  <td>{r.state_name || r.region}</td>
                  <td>{r.forecast_attempts ?? "Unknown"}</td>
                  <td>{r.planned_attempts ?? "Unknown"}</td>
                  <td>{r.usable_existing_capacity ?? "Unknown"}</td>
                  <td>{r.general_fallback_capacity ?? "Unknown"}</td>
                  <td>{r.capacity_waiting_for_review ?? "Unknown"}</td>
                  <td>{r.proposed_additional ?? "Not established"}</td>
                  <td>
                    {r.preferred_metros && (
                      <>
                        <small>
                          {r.preferred_metros} ·{" "}
                          {r.suggested_area_codes?.join(", ")}
                        </small>
                        <button
                          className="secondary small"
                          onClick={() => onFindState(r.region)}
                        >
                          Find numbers in {r.state_name}
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {forecast.schedule_scope === "declared_staffed_windows" && (
          <p className="pill subtle">Declared staffed periods</p>
        )}
        {forecast.schedule_scope === "normalized_simple_windows" && (
          <p className="pill subtle">Saved simple staffing schedule</p>
        )}
        {forecast.planning_limited && (
          <p className="notice">
            Allocation is incomplete; remaining bottlenecks are not established.
            Shown schedules are feasible examples. Narrow the horizon or cohort
            to review a smaller plan.
          </p>
        )}
        {forecast.screening && (
          <div className="fine" aria-label="Screening constraints">
            <p>
              {forecast.screening.cleared_count}{" "}
              {forecast.screening.cleared_count === 1
                ? "lead has"
                : "leads have"}{" "}
              current screening; {forecast.screening.blocked_count} are excluded
              from executable capacity.
            </p>
            <p>
              Screening as of {date(forecast.screening.as_of)}. All leads remain
              in list coverage totals; clearance is checked again before
              calling.
            </p>
            {forecast.screening.blocked_count > 0 && (
              <>
                <ul>
                  {Object.entries(forecast.screening.blocked_reasons).map(
                    ([reason, count]) => (
                      <li key={reason}>
                        {label(reason)}: {count}
                      </li>
                    ),
                  )}
                </ul>
                <p>
                  Review source authorization and current coverage in Settings.
                  Viewing this plan does not purchase screening.
                </p>
              </>
            )}
          </div>
        )}
        <dl className="capacity-summary">
          <div>
            <dt>Due attempt demand</dt>
            <dd>{forecast.forecast_attempts}</dd>
          </div>
          <div>
            <dt>Existing-only capacity</dt>
            <dd>{forecast.existing_only_capacity ?? "Not established"}</dd>
          </div>
          <div>
            <dt>Staffing shortfall</dt>
            <dd>{forecast.staffing_shortfall ?? "Not established"}</dd>
          </div>
          <div>
            <dt>Number shortfall</dt>
            <dd>{forecast.number_shortfall ?? "Not established"}</dd>
          </div>
          <div>
            <dt>
              {forecast.optimality === "best_feasible_found"
                ? "Best additional-number proposal"
                : "Minimum additional numbers"}
            </dt>
            <dd>{minimum ?? "Not established"}</dd>
          </div>
          {forecast.schedule_scope && (
            <>
              <div>
                <dt>Callbacks without owner availability</dt>
                <dd>
                  {forecast.owner_unavailable_attempts ?? "Not established"}
                </dd>
              </div>
              <div>
                <dt>Stable-identity shortfall</dt>
                <dd>{forecast.binding_shortfall ?? "Not established"}</dd>
              </div>
              <div>
                <dt>Setup-delay shortfall</dt>
                <dd>{forecast.setup_shortfall ?? "Not established"}</dd>
              </div>
            </>
          )}
        </dl>
        {!forecast.planning_limited &&
          !!forecast.capacity_shortfall &&
          forecast.capacity_shortfall > 0 && (
            <p className="fine">
              {forecast.staffing_shortfall} attempts exceed declared rep time;{" "}
              {forecast.number_shortfall} staffed attempts lack usable number
              capacity. These are separate constraints.
            </p>
          )}
        {forecast.contingent_retry_low_mean_high && (
          <>
            <p className="fine">
              Known next attempts are separate from uncertain later retries.
              Neither schedule authorizes a call.
            </p>
            <dl className="capacity-summary">
              <div>
                <dt>Possible later retries</dt>
                <dd>
                  {forecast.contingent_retry_low_mean_high.low.toFixed(1)}–
                  {forecast.contingent_retry_low_mean_high.high.toFixed(1)};
                  mean {forecast.contingent_retry_low_mean_high.mean.toFixed(1)}
                </dd>
              </div>
              <div>
                <dt>Observed retry support</dt>
                <dd>
                  {forecast.contingent_retry_low_mean_high.support ||
                    "Cold prior; no matured observations"}
                </dd>
              </div>
              <div>
                <dt>Capacity with hypothetical additions</dt>
                <dd>
                  {forecast.existing_plus_addition_capacity ??
                    "Not established"}
                </dd>
              </div>
              <div>
                <dt>Pending inventory scenario</dt>
                <dd>
                  {forecast.conditional_pending_schedule?.length ??
                    "Not established"}{" "}
                  displayed attempts, conditional on setup
                </dd>
              </div>
            </dl>
          </>
        )}
        {!!forecast.contingent_retry_low_mean_high?.omitted_leads && (
          <p className="notice">
            Retry scenarios are incomplete:{" "}
            {forecast.contingent_retry_low_mean_high.omitted_leads} leads
            omitted from this bounded calculation.
          </p>
        )}
        {forecast.contingent_capacity && (
          <section>
            <h3>Later capacity scenarios</h3>
            <p className="small muted">
              Conditional retries use the same rep time, number limits and
              stable caller identity. These are scenarios, not reservations or a
              reason to purchase numbers. Another number cannot replace a bound
              identity.
            </p>
            {forecast.contingent_capacity.status === "not_needed" ? (
              <p>No later permitted attempts in this forecast.</p>
            ) : (
              <table aria-label="Conditional retry capacity">
                <thead>
                  <tr>
                    <th>Scenario</th>
                    <th>Known + later attempts</th>
                    <th>Existing capacity</th>
                    <th>Capacity with additions</th>
                    <th>Additional numbers</th>
                    <th>Unserved</th>
                  </tr>
                </thead>
                <tbody>
                  {(["low", "mean", "high"] as const).map((name) => {
                    const row = forecast.contingent_capacity?.[name];
                    return (
                      <tr key={name}>
                        <td>
                          {label(name)}
                          {row?.status !== "ready" ? " · Incomplete" : ""}
                        </td>
                        <td>{row?.planned_attempts ?? "Unknown"}</td>
                        <td>{row?.existing_capacity ?? "Unknown"}</td>
                        <td>{row?.expanded_capacity ?? "Unknown"}</td>
                        <td>
                          {row?.additional_numbers ?? "Unknown"}
                          {row?.additional_numbers != null &&
                            (row.minimum_proven_on_grid
                              ? " · minimum on grid"
                              : " · best feasible")}
                        </td>
                        <td>{row?.unserved_attempts ?? "Unknown"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </section>
        )}
        {forecast.assumptions && (
          <p className="fine">
            Grid: {forecast.assumptions.staffing_grid_seconds}s. Cold work
            assumption: {forecast.assumptions.handling_seconds}s; observed
            estimates replace it when available. Setup delay:{" "}
            {forecast.assumptions.additional_ready_after_hours}h. Account
            channels:{" "}
            {forecast.assumptions.account_channel_limit ??
              "unknown; not provider-verified"}
            .
          </p>
        )}
        {forecast.expansion_review && (
          <div>
            <h3>Expansion review</h3>
            <p>
              {label(forecast.expansion_review.reason)}.{" "}
              {forecast.expansion_review.status === "observing" &&
              forecast.expansion_review.next_review_at
                ? `Review again after ${date(forecast.expansion_review.next_review_at)}. `
                : ""}
              Review available numbers and their costs under Add numbers with
              approval. This plan does not place an order.
            </p>
          </div>
        )}
        {!!forecast.unavailable_staff_count && (
          <p className="notice">
            {forecast.unavailable_staff_count} configured{" "}
            {forecast.unavailable_staff_count === 1 ? "rep is" : "reps are"}{" "}
            unavailable; their time is excluded.
          </p>
        )}
        {!!forecast.occupied_rep_count && (
          <p className="notice">
            {forecast.occupied_rep_count}{" "}
            {forecast.occupied_rep_count === 1 ? "rep has" : "reps have"} an
            existing call or reservation; occupied time is excluded.
          </p>
        )}
        {forecast.optimality_scope && (
          <p className="fine">
            {forecast.optimality_scope}. Pending inventory requires review
            before any addition; no predicted conversion lift is assumed.
          </p>
        )}
        <p className="small muted">
          Indicative monthly rental:{" "}
          {forecast.monthly_rental_estimate_usd == null
            ? "Not established"
            : `$${forecast.monthly_rental_estimate_usd.toFixed(2)}`}
          . This plan does not purchase numbers. Review an exact quote under Add
          numbers with approval before accepting any rental. Existing rental
          obligations remain.
        </p>
      </section>
    </>
  );
}
interface NumberEvidence {
  number: NumberRecord;
  health: {
    status: string;
    samples: number;
    comparable_samples?: number;
    human_classified_samples?: number;
    human_unknown_samples?: number;
    reason: string;
    as_of?: string;
    case_mix_residual_mean?: number | null;
    case_mix_interval?: [number, number] | null;
    policy_version?: string;
    external_fresh?: boolean;
  };
  workload: {
    current_limit: number;
    approved_daily_limit: number;
    maximum_reviewed_limit: number;
    recommended_limit: number;
    reason: string;
  };
  callback_candidates: {
    id: string;
    callback_evidence: {
      destination: string;
      observed_at: string;
      simulated: boolean;
    };
  }[];
  binding: {
    id: string;
    version: number;
    lead_id: string;
    number_id: string;
  } | null;
}
function NumberRow({
  number,
  onSaved,
}: {
  number: NumberRecord;
  onSaved: (number: NumberRecord) => void;
}) {
  const { api, refresh, run, notice, snapshot: w } = useWorkspace();
  const bounds = useDefinitions();
  const [view, setView] = useState<NumberEvidence | null>(null);
  const [reference, setReference] = useState("");
  const [limit, setLimit] = useState(String(number.daily_limit));
  const [decision, setDecision] = useState("resume");
  const [leadId, setLeadId] = useState("");
  const [targetId, setTargetId] = useState("");
  const [busy, setBusy] = useState(false);
  const request = useRef<{ fingerprint: string; id: string } | null>(null);
  async function load(id = leadId) {
    const result = await api<NumberEvidence>(
      `/v1/admin/numbers/${number.id}/review${id ? "?lead_id=" + encodeURIComponent(id) : ""}`,
    );
    setView(result);
    onSaved(result.number);
    setLimit(String(result.number.daily_limit));
  }
  async function save(
    path: string,
    method: string,
    body: Record<string, unknown>,
    version: number,
  ) {
    setBusy(true);
    const value = { ...body, expected_version: version };
    const fingerprint = JSON.stringify([path, value]);
    if (request.current?.fingerprint !== fingerprint)
      request.current = { fingerprint, id: crypto.randomUUID() };
    try {
      const saved = await api<NumberRecord>(path, method, {
        ...value,
        request_id: request.current.id,
      });
      onSaved(saved);
      setView((previous) =>
        previous ? { ...previous, number: saved } : previous,
      );
      request.current = null;
      await load();
      await refresh();
      notice("Number evidence saved. Inbound identity retained.");
    } finally {
      setBusy(false);
    }
  }
  const current = view?.number ?? number;
  return (
    <>
      <tr>
        <td>
          <strong>{number.phone}</strong>
          <small>
            {number.callback_evidence_current
              ? "Current callback evidence"
              : "Callback verification required"}
          </small>
        </td>
        <td>{number.region}</td>
        <td>
          <span className="pill subtle">{label(number.lifecycle)}</span>
        </td>
        <td>{number.daily_limit}</td>
        <td>{number.capacity} concurrent</td>
        <td>
          <div className="button-row">
            {number.provider_id && (
              <button
                className="text-button small"
                disabled={busy || number.provider_present === false}
                onClick={() =>
                  void run(async () => {
                    setBusy(true);
                    try {
                      onSaved(
                        await api<NumberRecord>(
                          `/v1/admin/numbers/${number.id}/configure`,
                          "POST",
                        ),
                      );
                      await load();
                      await refresh();
                    } finally {
                      setBusy(false);
                    }
                  })
                }
              >
                {number.configuration_status === "uncertain"
                  ? "Reconcile routing"
                  : "Configure voice route"}
              </button>
            )}
            {number.provider_id && number.provider_present === false && (
              <small>
                Not found in your Telnyx account. Sync existing numbers before
                configuring its route.
              </small>
            )}
            <button
              className="text-button small"
              onClick={() => void run(() => load())}
            >
              Review number evidence
            </button>
            <button
              className="text-button small"
              onClick={() =>
                void run(() =>
                  save(
                    `/v1/admin/numbers/${number.id}`,
                    "PATCH",
                    {
                      lifecycle: "paused_for_outbound",
                      reason: "Operator paused outbound; inbound retained",
                    },
                    current.version,
                  ),
                )
              }
            >
              Pause outbound
            </button>
          </div>
        </td>
      </tr>
      {view && (
        <tr>
          <td colSpan={6}>
            <section
              className="card padded"
              aria-label={`Evidence for ${number.phone}`}
            >
              <div className="button-row">
                <h3>Number evidence and workload</h3>
                <button onClick={() => setView(null)}>
                  Close number review
                </button>
                <button onClick={() => void run(() => load())}>
                  Reload evidence
                </button>
              </div>
              <p>
                {current.outbound_ready
                  ? "Ready for reviewed outbound use"
                  : "Outbound setup or evidence needs attention"}{" "}
                · {current.synthetic ? "Simulation evidence" : "Live workspace"}
              </p>
              <dl className="stats-grid">
                <div>
                  <dt>Observed health</dt>
                  <dd>{label(view.health.status)}</dd>
                </div>
                <div>
                  <dt>Comparable classified attempts</dt>
                  <dd>{view.health.comparable_samples ?? 0}</dd>
                </div>
                <div>
                  <dt>Unknown contact outcomes</dt>
                  <dd>{view.health.human_unknown_samples ?? 0}</dd>
                </div>
                <div>
                  <dt>Evidence as of</dt>
                  <dd>{date(view.health.as_of)}</dd>
                </div>
              </dl>
              <p>{label(view.health.reason)}</p>
              {view.health.case_mix_interval && (
                <p>
                  Absolute pickup difference versus frozen expectations:{" "}
                  {percent(view.health.case_mix_residual_mean ?? 0)};
                  approximate interval{" "}
                  {view.health.case_mix_interval
                    .map((v) => percent(v))
                    .join(" to ")}
                  . This is an investigation signal.
                </p>
              )}
              <p>
                {current.external_reputation?.spam_label
                  ? `Imported external label: ${current.external_reputation.spam_label}`
                  : "External label unknown"}
              </p>
              {!current.external_reputation && (
                <p className="fine">
                  First-party observed health is the default; no reputation
                  subscription is used.
                </p>
              )}
              {current.external_reputation && (
                <p className="fine">
                  Source: {current.external_reputation.source} · observed{" "}
                  {date(current.external_reputation.observed_at)} · expires{" "}
                  {date(current.external_reputation.expires_at)}. Imported
                  observation, not a live carrier certification.
                </p>
              )}
              {current.configuration_status === "uncertain" && (
                <div className="button-row">
                  <p className="fine">
                    An uncertain configuration is read first. This explicit
                    retry reapplies only disabled recording and the saved voice
                    connection after verifying the same owned number.
                  </p>
                  <button
                    disabled={busy}
                    onClick={() =>
                      void run(async () => {
                        setBusy(true);
                        try {
                          await api(
                            `/v1/admin/numbers/${number.id}/configure`,
                            "POST",
                            {
                              expected_version: current.version,
                              retry_after_verification: true,
                            },
                          );
                          await load();
                          await refresh();
                        } finally {
                          setBusy(false);
                        }
                      })
                    }
                  >
                    Verify and reapply saved configuration
                  </button>
                </div>
              )}
              <p>A note alone cannot verify a callback.</p>
              {current.callback_proof && (
                <p className="fine">
                  {current.callback_proof.simulated
                    ? "Simulated"
                    : "Signed provider"}{" "}
                  callback to {current.callback_proof.destination} · expires{" "}
                  {date(current.callback_proof.expires_at)}
                </p>
              )}
              <div className="button-row">
                {view.callback_candidates.map((t) => (
                  <button
                    key={t.id}
                    disabled={busy}
                    onClick={() =>
                      void run(() =>
                        save(
                          `/v1/admin/numbers/${number.id}`,
                          "PATCH",
                          { callback_task_id: t.id },
                          current.version,
                        ),
                      )
                    }
                  >
                    Verify{" "}
                    {t.callback_evidence.simulated ? "simulated" : "signed"}{" "}
                    callback · {date(t.callback_evidence.observed_at)} ·{" "}
                    {t.callback_evidence.destination}
                  </button>
                ))}
              </div>
              {!view.callback_candidates.length && (
                <p className="fine">
                  No recent connected callback receipt. Complete an authorized
                  inbound test on this route, then reload evidence.
                </p>
              )}
              <fieldset disabled={busy}>
                <div className="optimization-fields">
                  <label>
                    Number review decision
                    <select
                      value={decision}
                      onChange={(e) => setDecision(e.target.value)}
                    >
                      <option value="resume">
                        Start or resume limited use
                      </option>
                      <option value="increase">
                        Increase reviewed workload
                      </option>
                      <option value="reduce">Reduce workload</option>
                      <option value="pause">Pause outbound</option>
                    </select>
                  </label>
                  <label>
                    Reviewed daily limit
                    <input
                      type="number"
                      {...limits(bounds?.number_review?.daily_limit)}
                      value={limit}
                      onChange={(e) => setLimit(e.target.value)}
                    />
                  </label>
                  <label>
                    Number review reference
                    <input
                      value={reference}
                      maxLength={500}
                      onChange={(e) => setReference(e.target.value)}
                    />
                  </label>
                </div>
                <p className="fine">
                  Available staffed capacity and approved ceiling permit at most{" "}
                  {view.workload.maximum_reviewed_limit} attempts per day.
                  Current adaptive limit: {view.workload.current_limit};
                  reviewed maximum: {view.workload.approved_daily_limit}.{" "}
                  Suggested limit for explicit review:{" "}
                  {view.workload.recommended_limit}. Sparse evidence is unknown;
                  it does not require a fixed number of warmup calls.
                </p>
                {!reference.trim() && (
                  <p className="fine">
                    Enter a review reference to apply a workload decision.
                  </p>
                )}
                <button
                  disabled={!reference.trim()}
                  onClick={() =>
                    void run(() =>
                      save(
                        `/v1/admin/numbers/${number.id}/review`,
                        "POST",
                        {
                          decision,
                          review_reference: reference,
                          daily_limit: Number(limit),
                        },
                        current.version,
                      ),
                    )
                  }
                >
                  Apply number review
                </button>
                <details>
                  <summary>Review an existing lead assignment</summary>
                  <label>
                    Lead to reassign
                    <select
                      value={leadId}
                      onChange={(e) => {
                        setLeadId(e.target.value);
                        void run(() => load(e.target.value));
                      }}
                    >
                      <option value="">Choose a displayed lead</option>
                      {w.leads.map((l) => (
                        <option key={l.id} value={l.id}>
                          {l.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  {view.binding ? (
                    <>
                      <p>
                        Current number:{" "}
                        {w.numbers.find((n) => n.id === view.binding?.number_id)
                          ?.phone ?? view.binding.number_id}
                      </p>
                      <label>
                        Replacement owned number
                        <select
                          value={targetId}
                          onChange={(e) => setTargetId(e.target.value)}
                        >
                          <option value="">Choose replacement</option>
                          {w.numbers
                            .filter((n) => n.id !== view.binding?.number_id)
                            .map((n) => (
                              <option key={n.id} value={n.id}>
                                {n.phone} · {label(n.lifecycle)}
                              </option>
                            ))}
                        </select>
                      </label>
                      <button
                        disabled={!reference.trim() || !targetId}
                        onClick={() =>
                          void run(() =>
                            save(
                              `/v1/admin/numbers/${targetId}`,
                              "PATCH",
                              {
                                reassignment: {
                                  lead_id: leadId,
                                  old_number_id: view.binding!.number_id,
                                  reason: reference,
                                },
                              },
                              view.binding!.version,
                            ),
                          )
                        }
                      >
                        Approve identity reassignment
                      </button>
                    </>
                  ) : (
                    <p>
                      No existing binding selected. Initial assignment uses
                      available inventory.
                    </p>
                  )}
                </details>
              </fieldset>
            </section>
          </td>
        </tr>
      )}
    </>
  );
}
export function ScriptPage() {
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Script</h1>
          <p>
            Paste your script once. Each lead gets its own version, written from
            research.
          </p>
        </div>
      </div>
      <ScriptEditor />
      <details className="card padded">
        <summary>What's working</summary>
        <ScriptMemory />
      </details>
      <details className="card padded">
        <summary>Call statistics</summary>
        <ConversationLearning />
        <OptimizationPanel />
      </details>
      <details className="card padded">
        <summary>What you sell</summary>
        <ProductKnowledge />
      </details>
    </>
  );
}
export function Empty({ text }: { text: string }) {
  return (
    <div className="empty">
      <Layers3 size={25} strokeWidth={1.2} />
      <p>{text}</p>
    </div>
  );
}
