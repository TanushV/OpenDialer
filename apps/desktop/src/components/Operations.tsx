import { invoke } from "@tauri-apps/api/core";
import { NumberPurchasing } from "./NumberPurchasing";
import { ProductKnowledge } from "./ProductKnowledge";
import {
  ArrowRight,
  ChevronRight,
  Layers3,
  RefreshCw,
  Search,
  Upload,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { CallbackRouting } from "./CallbackRouting";
import { useHistory } from "../state/useHistory";
import { limits, useDefinitions } from "../state/definitions";
import { HistoryMore } from "./HistoryMore";
import { LONG_REQUEST_MS, native, openSource } from "../api/client";
import { date, isAdmin, label, percent, useWorkspace } from "../context";
import { parseCSV } from "../state/csv";
import { ColumnMap, type ColumnMapping } from "./ColumnMap";
import { HubSpotLists } from "./HubSpotLists";
import type { Lead, NumberRecord, Packet } from "../types";
import { ProspectBrief } from "./ProspectBrief";
import { BulkLeadActions, leadLists, listLabel, useLists } from "./LeadLists";
import { PreparationPanel } from "./PreparationPanel";
import { ScriptEditor } from "./ScriptEditor";
import { TokenUsage } from "./TokenUsage";
import { ScriptMemory } from "./ScriptMemory";
import { ScriptReader } from "./ScriptReader";

export function LeadsPage({
  onNavigate,
}: {
  onNavigate: (view: "Script" | "Settings") => void;
}) {
  const { snapshot: w, api, run, refresh, notice } = useWorkspace();
  const admin = isAdmin(w.user.role);
  const [csv, setCSV] = useState("");
  const [columnMap, setColumnMap] = useState<ColumnMapping | null>(null);
  const [listName, setListName] = useState("");
  const [query, setQuery] = useState("");
  const [report, setReport] = useState<{
    imported: number;
    duplicates: number;
    invalid: number;
  } | null>(null);
  const [packet, setPacket] = useState<Packet | null>(null);
  const [lookupLead, setLookupLead] = useState("");
  const [lookupStatus, setLookupStatus] = useState<Record<string, string>>({});
  const [filterList, setFilterList] = useState<string | null>(null);
  const [leadView, setLeadView] = useState("active");
  const [selected, setSelected] = useState<string[]>([]);
  const lists = useLists();
  const history = useHistory<Lead>("leads", [], {
    filter: leadView,
    ...(filterList === null ? {} : { list_name: filterList }),
  });
  const leads = history.items;
  useEffect(() => {
    setSelected([]);
    void run(() => history.load(false, query, leadView));
  }, [filterList, leadView]);
  useEffect(() => {
    if (
      leadView === "active" &&
      filterList !== null &&
      lists.data?.lists.some(
        (list) => list.name === filterList && list.archived,
      )
    )
      setFilterList(null);
  }, [lists.data, filterList, leadView]);
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
      <section className="card table-card">
        <div className="leads-toolbar">
          <h3>{history.totalLabel ?? w.leads.length} leads</h3>
          <select
            aria-label="Lead view"
            value={leadView}
            onChange={(e) => {
              setLeadView(e.target.value);
              setFilterList(null);
            }}
          >
            <option value="active">Active leads</option>
            <option value="removed">Removed leads</option>
            <option value="all">All leads and history</option>
          </select>
          <select
            aria-label="Filter leads by list"
            value={filterList ?? "__all_lists__"}
            onChange={(e) =>
              setFilterList(
                e.target.value === "__all_lists__" ? null : e.target.value,
              )
            }
          >
            <option value="__all_lists__">All lists</option>
            {lists.data?.lists
              .filter((list) => leadView !== "active" || !list.archived)
              .map((list) => (
                <option key={list.name} value={list.name}>
                  {listLabel(list.name)}
                  {list.archived ? " · removed" : ""}
                </option>
              ))}
          </select>
          <div className="search">
            <Search size={16} />
            <input
              aria-label="Search leads"
              placeholder="Find a person or company"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter")
                  void run(() => history.load(false, query, leadView));
              }}
            />
            <button
              aria-label="Search lead history"
              disabled={history.busy}
              onClick={() =>
                void run(() => history.load(false, query, leadView))
              }
            >
              Search
            </button>
          </div>
        </div>
        {admin && selected.length > 0 && (
          <BulkLeadActions
            selected={selected}
            memberships={[
              ...new Set(
                leads
                  .filter((lead) => selected.includes(lead.id))
                  .flatMap(leadLists),
              ),
            ].sort()}
            filteredList={filterList}
            updated={async () => {
              setSelected([]);
              await refresh();
              await history.load(false, query, leadView);
              await lists.reload();
            }}
          />
        )}
        {history.query && history.cursor && (
          <p role="status">More records remain to search.</p>
        )}
        <table>
          <thead>
            <tr>
              {admin && (
                <th>
                  <input
                    type="checkbox"
                    aria-label="Select all visible leads"
                    checked={
                      leads.length > 0 &&
                      leads.every((lead) => selected.includes(lead.id))
                    }
                    onChange={(e) =>
                      setSelected(
                        e.target.checked ? leads.map((lead) => lead.id) : [],
                      )
                    }
                  />
                </th>
              )}
              <th>PERSON / COMPANY</th>
              <th>PHONE</th>
              <th>STATUS</th>
              <th>CALL BY</th>
              <th>ATTEMPTS</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {leads.map((lead) => (
              <tr key={lead.id}>
                {admin && (
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Select ${lead.name}`}
                      checked={selected.includes(lead.id)}
                      onChange={(e) =>
                        setSelected(
                          e.target.checked
                            ? [...selected, lead.id]
                            : selected.filter((id) => id !== lead.id),
                        )
                      }
                    />
                  </td>
                )}
                <td>
                  <strong>{lead.name}</strong>
                  <small>{lead.company || "Company not provided"}</small>
                  <small>
                    {leadLists(lead).map(listLabel).join(", ") || "No list"}
                  </small>
                </td>
                <td>
                  {lead.phone}
                  {lead.number_label && (
                    <small
                      className={lead.number_hold ? "number-held" : undefined}
                      title={lead.number_hold_reason ?? undefined}
                    >
                      {lead.number_hold
                        ? `Not dialed · ${lead.number_hold_reason}`
                        : lead.number_label}
                    </small>
                  )}
                  {lead.trigger_label &&
                    lead.trigger_label !== "No trigger found" && (
                      <small>Why now: {lead.trigger_label}</small>
                    )}
                </td>
                <td>
                  <span
                    className={`pill ${lead.suppressed ? "red" : lead.status === "preparing" ? "amber" : lead.outreach_hold ? "subtle" : "green"}`}
                  >
                    {lead.archived
                      ? "Removed"
                      : lead.dial_paused
                        ? "Paused"
                        : lead.suppressed
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
        {!leads.length && !history.cursor && history.ready && (
          <Empty
            text={
              history.query
                ? "No matching leads."
                : "Import a list to get started."
            }
          />
        )}
        {!leads.length && !history.ready && (
          <p className="leads-loading" role="status">
            Loading leads…
          </p>
        )}
        <HistoryMore
          history={history}
          label={history.query ? "Continue lead search" : "Load more leads"}
        />
      </section>
      <PreparationPanel onNavigate={onNavigate} />
      {admin && (
        <HubSpotLists
          onSynced={async () => {
            await refresh();
            await history.load(false, query, leadView);
            await lists.reload();
          }}
        />
      )}
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
            <ColumnMap
              headers={csvHeaders(csv)}
              value={columnMap}
              onChange={setColumnMap}
            />
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
                        ...(columnMap ? { column_map: columnMap } : {}),
                      },
                      { timeoutMs: LONG_REQUEST_MS },
                    );
                    setReport(report);
                    setCSV("");
                    setColumnMap(null);
                    await refresh();
                    await history.load(false, query, leadView);
                    await lists.reload();
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
            {admin && (
              <div>
                {lookupStatus[packet.lead.id] && (
                  <p role="status">{lookupStatus[packet.lead.id]}</p>
                )}
                {lookupLead === packet.lead.id ? (
                  <div className="button-row">
                    <button
                      onClick={() => {
                        const id = packet.lead.id;
                        setLookupLead("");
                        void run(async () => {
                          const result = await api<{
                            status: string;
                            carrier?: string;
                            line_type?: string;
                          }>(`/v1/admin/leads/${id}/carrier-lookup`, "POST", {
                            approve_charge: true,
                          });
                          const status =
                            result.status === "completed"
                              ? `${result.carrier || "Unknown carrier"} · ${result.line_type || "Unknown line type"}`
                              : "No confirmed carrier receipt. The original lookup will not be charged again by this action.";
                          setLookupStatus((previous) => ({
                            ...previous,
                            [id]: status,
                          }));
                        });
                      }}
                    >
                      Approve one metered Telnyx lookup
                    </button>
                    <button onClick={() => setLookupLead("")}>
                      Cancel lookup
                    </button>
                  </div>
                ) : (
                  <button onClick={() => setLookupLead(packet.lead.id)}>
                    Check carrier and line type
                  </button>
                )}
              </div>
            )}
            <h3>Script for this lead</h3>
            <p className="fine">
              {packet.script.fallback
                ? packet.lead.status === "preparing"
                  ? "Being prepared. This is your script as written until the agent finishes."
                  : packet.preparation?.status === "skipped"
                    ? `The agent skipped this lead: ${packet.preparation.reason ?? "no reason given."} It is called with your script as written.`
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

interface NumbersSummary {
  lists?: string[];
  states?: { code: string; name: string }[];
  inventory: {
    total: number;
    ready: number;
    pending_setup: number;
    workspace_test_passed: boolean;
    limited_use: number;
    paused_outbound: number;
    investigating: number;
  };
  stable_assignments: number;
}

function useNumbersSummary(listName: string | null) {
  const { api, subscribe } = useWorkspace();
  const scope = useRef({ api, listName });
  scope.current = { api, listName };
  const [summary, setSummary] = useState<NumbersSummary | null>(null);
  const [error, setError] = useState("");
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
    void load();
    const stop = subscribe((events) => {
      if (
        events.some((e) =>
          ["optimization.updated", "number.updated"].includes(e.type),
        )
      )
        void load();
    });
    return () => {
      stop();
      version.current++;
    };
  }, [load, subscribe]);
  return { summary, error, load };
}

/** The business name carriers may show with outbound calls, and the free spam-label registry. */
function CallerIdentity({ onChanged }: { onChanged: () => Promise<void> }) {
  const { api, run, notice } = useWorkspace();
  const [name, setName] = useState("");
  useEffect(() => {
    let mounted = true;
    void api<{ caller_name?: string }>("/v1/admin/settings")
      .then((saved) => {
        if (mounted) setName(saved.caller_name ?? "");
      })
      .catch(() => undefined);
    return () => {
      mounted = false;
    };
  }, [api]);
  const trimmed = name.trim();
  const valid = !trimmed || /^[A-Za-z0-9 ]{1,15}$/.test(trimmed);
  return (
    <section className="card padded">
      <h3>Caller name (optional)</h3>
      <p className="fine">
        The business name some phones show with your calls (15 letters, digits
        or spaces). New numbers get it automatically. Leave it blank for none,
        and phones show the number. Phones update over days and no carrier
        guarantees display.
      </p>
      <div className="button-row">
        <input
          aria-label="Business name for caller ID"
          maxLength={15}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="YOUR BUSINESS"
        />
        <button
          className="secondary"
          disabled={!valid}
          onClick={() =>
            void run(async () => {
              const saved = await api<{ name: string | null; failed: number }>(
                "/v1/admin/caller-name",
                "PUT",
                { name: trimmed || null },
              );
              await onChanged();
              notice(
                saved.failed
                  ? `Caller name saved, but Telnyx did not confirm ${saved.failed} number(s).`
                  : saved.name
                    ? "Caller name saved and sent to Telnyx."
                    : "Caller name removed.",
              );
            })
          }
        >
          Save caller name
        </button>
      </div>
      <p className="fine">
        Also register your numbers and business once at the{" "}
        <button
          className="text-button"
          onClick={() =>
            void run(() => openSource("https://www.freecallerregistry.com/"))
          }
        >
          Free Caller Registry
        </button>{" "}
        so AT&amp;T, T-Mobile and Verizon spam labels know who you are.
      </p>
    </section>
  );
}

type OwnedNumber = {
  phone: string;
  added: boolean;
  route: "unassigned" | "this_app" | "other_app";
};

function OwnedNumbers({
  owned,
  onAdd,
}: {
  owned: OwnedNumber[];
  onAdd: (phone: string, move: boolean) => void;
}) {
  const waiting = owned.filter((number) => !number.added);
  if (!waiting.length)
    return (
      <div className="owned-numbers">
        <p className="fine" role="status">
          {owned.length
            ? "Everything in your Telnyx account is already here."
            : "There are no numbers in your Telnyx account yet."}
        </p>
      </div>
    );
  return (
    <div
      aria-label="Numbers in your Telnyx account"
      className="owned-numbers"
      role="group"
    >
      {waiting.map((number) => (
        <div className="button-row" key={number.phone}>
          <span>
            {number.phone}
            {number.route === "other_app" &&
              " · On another Telnyx app. Adding it moves its calls here."}
          </span>
          <button
            className="secondary small"
            onClick={() => onAdd(number.phone, number.route === "other_app")}
          >
            {number.route === "other_app" ? "Move and add" : "Add"}{" "}
            {number.phone}
          </button>
        </div>
      ))}
    </div>
  );
}

export function NumbersPage() {
  const { snapshot: w, api, run, refresh, notice } = useWorkspace();
  const history = useHistory("numbers", w.numbers);
  const [listName, setListName] = useState<string | null>(null);
  const plan = useNumbersSummary(listName);
  const inventory = plan.summary?.inventory;
  const [owned, setOwned] = useState<OwnedNumber[] | null>(null);
  const testNumber = history.items.find((n) => n.identity_configured)?.phone;
  const checkTelnyx = async () => {
    const result = await api<{
      owned: OwnedNumber[];
      setup_failed?: number;
    }>("/v1/admin/numbers/sync", "POST");
    setOwned(result.owned);
    await refresh();
    await history.load();
    await plan.load();
    if (result.setup_failed)
      notice(
        `${result.setup_failed} number(s) did not finish setup. Use Set up route to retry.`,
      );
  };
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Numbers &amp; callbacks</h1>
          <p>
            A new number starts at 15 real calls a day and rises to 60 as you
            use it. No fake calls are made.
          </p>
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
        {inventory?.total &&
        inventory.workspace_test_passed === false &&
        testNumber ? (
          <p className="alert" role="status">
            One test call left: go ready, call {testNumber} from your phone and
            answer it here. Every number opens after that.
          </p>
        ) : null}
      </section>
      <section className="card table-card">
        <div className="table-toolbar">
          <h3>Your number inventory</h3>
          <button
            className="secondary small"
            onClick={() => void run(checkTelnyx)}
          >
            <RefreshCw size={14} />
            Check my Telnyx numbers
          </button>
        </div>
        {owned && (
          <OwnedNumbers
            owned={owned}
            onAdd={(number, move) =>
              void run(async () => {
                await api("/v1/admin/numbers/add", "POST", {
                  phone: number,
                  move_from_other_app: move,
                });
                await checkTelnyx();
                notice(`${number} added and set up.`);
              })
            }
          />
        )}
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
          <Empty text="No numbers yet. Buy one above, or check your Telnyx numbers and add one." />
        )}
      </section>
      <label className="card padded compact-field">
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
      <NumberPurchasing
        states={plan.summary?.states ?? []}
        listName={listName}
        onChanged={async () => {
          await refresh();
          await history.load();
          await plan.load();
        }}
      />
      <CallerIdentity
        onChanged={async () => {
          await refresh();
          await history.load();
        }}
      />
      <CallbackRouting />
      <section
        className="card padded"
        aria-label="Free Caller Registry and label checks"
      >
        <h3>Free Caller Registry and label checks</h3>
        <p>
          Export the owned inventory and submit it once. Check caller display on
          real phones and record any label in Number evidence.
        </p>
        <div className="button-row">
          <button
            onClick={() =>
              void run(async () => {
                const { csv } = await api<{ csv: string }>(
                  "/v1/admin/numbers/registry-export",
                );
                if (native()) {
                  const path = await invoke<string>("save_registry_csv", {
                    csv,
                  });
                  notice(`Registry CSV saved to ${path}`);
                  return;
                }
                const url = URL.createObjectURL(
                  new Blob([csv], { type: "text/csv" }),
                );
                const link = document.createElement("a");
                link.href = url;
                link.download = "opendialer-caller-registry.csv";
                link.click();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
              })
            }
          >
            Export registry CSV
          </button>
          <button
            onClick={() =>
              void run(() => openSource("https://freecallerregistry.com/fcr/"))
            }
          >
            Open Free Caller Registry
          </button>
        </div>
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
  const [labelSource, setLabelSource] = useState("");
  const [observedLabel, setObservedLabel] = useState("");
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
            {number.outbound_ready
              ? "Ready to dial"
              : number.lifecycle === "paused_for_outbound"
                ? "Paused"
                : number.identity_configured &&
                    number.workspace_test_passed === false
                  ? "Waiting for your test call"
                  : "Needs setup"}
          </small>
        </td>
        <td>{number.region}</td>
        <td>
          <span className="pill subtle">{label(number.lifecycle)}</span>
        </td>
        <td>
          {number.today_limit ?? number.daily_limit} today ·{" "}
          {number.daily_limit} max
        </td>
        <td>{number.capacity} concurrent</td>
        <td>
          <div className="button-row">
            {number.provider_id &&
              number.configuration_status !== "confirmed" && (
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
                    : "Set up route"}
                </button>
              )}
            {number.provider_id && number.provider_present === false && (
              <small>
                Not found in your Telnyx account. Check your Telnyx numbers
                before setting up its route.
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
              {current.last_sip_block_at && (
                <p role="status">
                  Recent SIP 603 or 603+ blocks: {current.recent_sip_blocks}.
                  Last observed {date(current.last_sip_block_at)}.
                  {current.lifecycle === "paused_for_outbound" &&
                    ` ${current.reason || "Outbound paused."} Callbacks remain enabled.`}
                </p>
              )}
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  const now = new Date();
                  void run(() =>
                    save(
                      `/v1/admin/numbers/${number.id}`,
                      "PATCH",
                      {
                        external_reputation: {
                          source: labelSource.trim(),
                          observed_at: now.toISOString(),
                          expires_at: new Date(
                            now.getTime() + 28 * 86_400_000,
                          ).toISOString(),
                          ownership_reference:
                            number.provider_id || number.phone,
                          spam_label: observedLabel.trim() || null,
                        },
                      },
                      current.version,
                    ),
                  );
                }}
              >
                <label>
                  Label evidence source
                  <input
                    value={labelSource}
                    onChange={(e) => setLabelSource(e.target.value)}
                    placeholder="Carrier, device, or report"
                    minLength={3}
                    maxLength={300}
                    required
                  />
                </label>
                <label>
                  Observed label (enter clear for a resolved label)
                  <input
                    value={observedLabel}
                    onChange={(e) => setObservedLabel(e.target.value)}
                    maxLength={100}
                  />
                </label>
                <button disabled={busy || labelSource.trim().length < 3}>
                  Save label check
                </button>
              </form>
              <details>
                <summary>Delivery incident guidance</summary>
                <p>
                  For a label on one network, pause or reduce the number,
                  preserve callbacks, and request a targeted review. If several
                  numbers drop on one network, verify routing and attestation.
                  If several drop across networks, review shared traffic
                  patterns. If labels look clear, check audio, screening and
                  failed calls. Gather more evidence after one poor day.
                </p>
                <div className="button-row">
                  {[
                    [
                      "AT&T label review",
                      "https://www.att.com/reviewmycalllabel",
                    ],
                    [
                      "T-Mobile call reporting",
                      "https://callreporting.t-mobile.com/",
                    ],
                    [
                      "Verizon voice spam feedback",
                      "https://spamalerts.verizon.com/vsf/",
                    ],
                    [
                      "Hiya free report and support",
                      "https://www.hiya.com/products/registration",
                    ],
                  ].map(([title, url]) => (
                    <button
                      key={title}
                      onClick={() => void run(() => openSource(url))}
                    >
                      {title}
                    </button>
                  ))}
                </div>
              </details>
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
export function ScriptPage({
  onNavigate,
}: {
  onNavigate: (view: "Script" | "Settings") => void;
}) {
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
      <PreparationPanel onNavigate={onNavigate} compact />
      <TokenUsage />
      <details className="card padded">
        <summary>What's working</summary>
        <ScriptMemory />
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

/** The header names of pasted CSV text, or none until it parses. */
function csvHeaders(text: string): string[] {
  if (!text.trim()) return [];
  try {
    return Object.keys(parseCSV(text)[0] ?? {});
  } catch {
    return [];
  }
}
