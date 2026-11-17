import { invoke } from "@tauri-apps/api/core";
import { NumberPurchasing } from "./NumberPurchasing";
import {
  ArrowRight,
  ChevronRight,
  Layers3,
  RefreshCw,
  Search,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CallbackRouting } from "./CallbackRouting";
import { countLabel, useHistory } from "../state/useHistory";
import { HistoryMore } from "./HistoryMore";
import { LONG_REQUEST_MS, native, openSource } from "../api/client";
import { date, label, useWorkspace } from "../context";
import { leadCSVHeaders, parseLeadCSV } from "../state/csv";
import { ColumnMap, type ColumnMapping } from "./ColumnMap";
import { HubSpotLists } from "./HubSpotLists";
import type { Lead, NumberRecord, Packet } from "../types";
import { ProspectBrief } from "./ProspectBrief";
import {
  BulkLeadActions,
  ListsPage,
  leadLists,
  listLabel,
  useListCatalog,
  type LeadSelection,
} from "./LeadLists";
import { PreparationPanel } from "./PreparationPanel";
import { ScriptEditor } from "./ScriptEditor";
import { TokenUsage } from "./TokenUsage";
import { ScriptMemory } from "./ScriptMemory";
import { ScriptReader } from "./ScriptReader";
import { OpenFile } from "./OpenFile";
import { HoldRelease } from "./HoldRelease";
import { readNewest, useNewestAnswer } from "../state/newest";

const SCRIPT_STATUS: Record<string, string> = {
  ready: "Script ready",
  queued: "Script in preparation",
  stale: "Script needs refresh",
  skipped: "Script skipped",
  failed: "Script generation failed",
  missing: "Script needed",
};

export function LeadsPage({
  onNavigate,
}: {
  onNavigate: (view: "Script" | "Settings") => void;
}) {
  const { snapshot: w, api, run, refresh, notice, subscribe } = useWorkspace();
  const [csv, setCSV] = useState("");
  const [mappedColumns, setMappedColumns] = useState<{
    headerKey: string;
    map: ColumnMapping;
  } | null>(null);
  const parsedCSV = useMemo(() => {
    if (!csv.trim()) return null;
    try {
      return parseLeadCSV(csv);
    } catch (error) {
      return error instanceof Error ? error : new Error(String(error));
    }
  }, [csv]);
  const csvData = parsedCSV instanceof Error ? null : parsedCSV;
  const headers = csvData?.headers ?? [];
  const headerKey = headers.join("\u0000");
  const columnMap =
    mappedColumns?.headerKey === headerKey ? mappedColumns.map : null;
  const [listName, setListName] = useState("");
  const [query, setQuery] = useState("");
  const [report, setReport] = useState<{
    imported: number;
    duplicates: number;
    invalid: number;
    errors: { row: number; reason: string }[];
  } | null>(null);
  const [packet, setPacket] = useState<Packet | null>(null);
  const packetLead = useRef<string | null>(null);
  const packetReads = useNewestAnswer();
  const loadPacket = useCallback(
    (id: string) =>
      readNewest(
        packetReads,
        () => api<Packet>(`/v1/leads/${id}/packet`),
        (next) => {
          if (packetLead.current === id) setPacket(next);
        },
      ),
    [api, packetReads],
  );
  const closePacket = () => {
    packetLead.current = null;
    setPacket(null);
  };
  const [lookupLead, setLookupLead] = useState("");
  const [lookupStatus, setLookupStatus] = useState<Record<string, string>>({});
  const [filterList, setFilterList] = useState<string | null>(null);
  const [leadView, setLeadView] = useState("active");
  const [selected, setSelected] = useState<string[]>([]);
  const [all, setAll] = useState<
    (LeadSelection & { total: number; memberships: string[] }) | null
  >(null);
  const [selecting, setSelecting] = useState(false);
  const lists = useListCatalog();
  const history = useHistory<Lead>("leads", [], {
    filter: leadView,
    ...(filterList === null ? {} : { list_name: filterList }),
  });
  // A save can finish after the rep switched views. Reload using the current filters.
  const latestLeadRead = useRef(() => history.load(false, query, leadView));
  latestLeadRead.current = () => history.load(false, query, leadView);
  const reloadLeads = () => latestLeadRead.current();
  const leads = history.items;
  const selectionScope = {
    filter: leadView,
    ...(filterList === null ? {} : { list_name: filterList }),
    q: history.query,
  };
  const scopeKey = JSON.stringify(selectionScope);
  const selectionScopeKey = useRef(scopeKey);
  selectionScopeKey.current = scopeKey;
  function clearSelection() {
    setAll(null);
    setSelected([]);
  }
  function searchLeads() {
    clearSelection();
    return history.load(false, query, leadView);
  }
  async function selectAll() {
    const params = new URLSearchParams(selectionScope);
    setSelecting(true);
    try {
      const result = await api<{
        total: number;
        fingerprint: string;
        memberships: string[];
      }>(`/v1/lead-selection?${params}`, "GET", undefined, {
        timeoutMs: LONG_REQUEST_MS,
      });
      if (selectionScopeKey.current === scopeKey) {
        setSelected([]);
        setAll(
          result.total ? { ...selectionScope, ...result, exclude: [] } : null,
        );
      }
    } finally {
      setSelecting(false);
    }
  }
  const activeSelection =
    all &&
    all.filter === leadView &&
    (all.list_name ?? null) === filterList &&
    all.q === history.query
      ? all
      : null;
  const selectedCount = activeSelection
    ? activeSelection.total - activeSelection.exclude.length
    : selected.length;
  const isSelected = (id: string) =>
    activeSelection
      ? !activeSelection.exclude.includes(id)
      : selected.includes(id);
  const listSettings = new Map(
    lists.data?.lists.map((list) => [list.name, list]),
  );
  const scriptLabel = (lead: Lead) => {
    const memberships = leadLists(lead)
      .filter((name) => filterList === null || name === filterList)
      .map((name) => listSettings.get(name))
      .filter((list) => !list?.archived && !list?.paused);
    return memberships.length &&
      memberships.every((list) => list?.script_mode === "master")
      ? "Uses master script"
      : SCRIPT_STATUS[lead.script_status ?? ""];
  };
  const updateLead = history.update;
  useEffect(
    () =>
      subscribe((events) => {
        let refreshPacket = false;
        for (const event of events) {
          if (!event.type.startsWith("lead.")) continue;
          if (event.entity_id === packetLead.current) refreshPacket = true;
          const { script_status, lead_status } = event.payload;
          if (
            typeof script_status === "string" &&
            typeof lead_status === "string"
          ) {
            updateLead({
              id: event.entity_id,
              version: event.entity_version,
              script_status,
              status: lead_status,
            });
          }
        }
        if (refreshPacket && packetLead.current)
          void loadPacket(packetLead.current).catch(() => {});
      }),
    [loadPacket, subscribe, updateLead],
  );
  const activeCount = w.active_lead_count;
  const total =
    history.totalLabel ??
    (leadView === "active" && filterList === null && activeCount
      ? countLabel(activeCount.total, activeCount.total_capped)
      : null);
  useEffect(() => {
    clearSelection();
    void run(() => history.load(false, query, leadView));
  }, [api, filterList, leadView]);
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
      <section
        className="card table-card"
        aria-label="Leads"
        aria-busy={!history.ready}
      >
        <div className="leads-toolbar">
          <h3>{total === null ? "Leads" : `${total} leads`}</h3>
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
                if (e.key === "Enter" && !selecting) void run(searchLeads);
              }}
            />
            <button
              className="text-button"
              aria-label="Search lead history"
              disabled={history.busy || selecting}
              onClick={() => void run(searchLeads)}
            >
              Search
            </button>
          </div>
        </div>
        {lists.error && (
          <p role="alert">
            List filters could not be loaded.{" "}
            <button
              className="text-button"
              onClick={() =>
                void run(async () => {
                  await lists.reload();
                  lists.setError("");
                })
              }
            >
              Retry list filters
            </button>
          </p>
        )}
        {selectedCount > 0 && (
          <BulkLeadActions
            selected={selected}
            selection={
              activeSelection
                ? {
                    ...selectionScope,
                    fingerprint: activeSelection.fingerprint,
                    exclude: activeSelection.exclude,
                  }
                : undefined
            }
            count={selectedCount}
            memberships={
              activeSelection?.memberships ??
              [
                ...new Set(
                  leads
                    .filter((lead) => selected.includes(lead.id))
                    .flatMap(leadLists),
                ),
              ].sort()
            }
            filteredList={filterList}
            updated={async () => {
              clearSelection();
              await refresh();
              await reloadLeads();
              await lists.reload();
            }}
          />
        )}
        {history.query && history.cursor && (
          <p role="status">More records remain to search.</p>
        )}
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>
                  <input
                    type="checkbox"
                    aria-label="Select all matching leads"
                    ref={(node) => {
                      if (node)
                        node.indeterminate =
                          selectedCount > 0 &&
                          (!activeSelection ||
                            activeSelection.exclude.length > 0);
                    }}
                    checked={Boolean(
                      activeSelection && !activeSelection.exclude.length,
                    )}
                    disabled={
                      !history.ready ||
                      history.busy ||
                      selecting ||
                      (!leads.length && !history.cursor)
                    }
                    onChange={(e) =>
                      e.target.checked ? void run(selectAll) : clearSelection()
                    }
                  />
                </th>
                <th>PERSON / COMPANY</th>
                <th>PHONE</th>
                <th>STATUS</th>
                <th>ATTEMPTS</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {leads.map((lead) => (
                <tr key={lead.id}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Select ${lead.name}`}
                      checked={isSelected(lead.id)}
                      disabled={history.busy || selecting}
                      onChange={(e) => {
                        if (activeSelection) {
                          setAll({
                            ...activeSelection,
                            exclude: e.target.checked
                              ? activeSelection.exclude.filter(
                                  (id) => id !== lead.id,
                                )
                              : [...activeSelection.exclude, lead.id],
                          });
                        } else {
                          setSelected(
                            e.target.checked
                              ? [...selected, lead.id]
                              : selected.filter((id) => id !== lead.id),
                          );
                        }
                      }}
                    />
                  </td>
                  <td>
                    <strong>{lead.name}</strong>
                    <small>{lead.company || "Company not provided"}</small>
                    <small>
                      {leadLists(lead).map(listLabel).join(", ") || "No list"}
                    </small>
                  </td>
                  <td>
                    <span className="table-phone">{lead.phone}</span>
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
                    {lead.script_status && (
                      <small className="muted">{scriptLabel(lead)}</small>
                    )}
                  </td>
                  <td>{lead.attempt_count}</td>
                  <td>
                    <button
                      className="icon-button"
                      aria-label={`Open lead ${lead.name}`}
                      onClick={() => {
                        packetLead.current = lead.id;
                        void run(() => loadPacket(lead.id));
                      }}
                    >
                      <ChevronRight size={18} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
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
      {/* Below the leads: it loads after them, so it never pushes the leads toolbar down (R10.10). */}
      <ListsPage
        embedded
        onChanged={async () => {
          await reloadLeads();
          await lists.reload();
        }}
      />
      <HubSpotLists
        onSynced={async () => {
          await refresh();
          await reloadLeads();
          await lists.reload();
        }}
      />
      <section className="card import-card">
        <div>
          <h2>Import a list</h2>
          <p className="muted">
            Use a CSV with a phone or mobile number. A name, company, and
            location are helpful. Research and scriptwriting can use every
            column.
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
          <p className="fine">
            For multiple signals, split summaries with | and dates and links
            with semicolons.
          </p>
          <div className="button-row">
            <OpenFile
              label="Open lead list (.csv)"
              accept=".csv,text/csv"
              onOpen={(file) => {
                void run(async () => {
                  const text = await file.text();
                  setListName(file.name.replace(/\.csv$/i, "").slice(0, 120));
                  setCSV(text);
                });
              }}
            />
            <button
              className="secondary"
              onClick={() =>
                void run(async () => {
                  await navigator.clipboard.writeText(leadCSVHeaders);
                  notice("Recommended CSV headers copied.");
                })
              }
            >
              Copy recommended CSV headers
            </button>
            <button
              className="primary"
              disabled={!csvData?.rows.length}
              onClick={() =>
                void run(async () => {
                  if (!csvData) return;
                  const report = await api<{
                    imported: number;
                    duplicates: number;
                    invalid: number;
                    errors: { row: number; reason: string }[];
                  }>(
                    "/v1/leads/import",
                    "POST",
                    {
                      campaign_id: w.campaign.id,
                      list_name: listName.trim(),
                      rows: csvData.rows,
                      ...(columnMap ? { column_map: columnMap } : {}),
                    },
                    { timeoutMs: LONG_REQUEST_MS },
                  );
                  setReport({
                    ...report,
                    invalid: report.invalid + csvData.errors.length,
                    errors: [
                      ...csvData.errors,
                      ...(report.errors ?? []).map(({ row, reason }) => ({
                        row: csvData.rowNumbers[row - 1] ?? row,
                        reason:
                          reason === "INVALID_PHONE"
                            ? "No usable phone number."
                            : label(reason),
                      })),
                    ],
                  });
                  setCSV("");
                  setMappedColumns(null);
                  await refresh();
                  await reloadLeads();
                  await lists.reload();
                  notice(
                    `Import finished · ${report.imported} new contacts · ${report.duplicates} matched.`,
                  );
                })
              }
            >
              Import leads
              <ArrowRight size={15} />
            </button>
          </div>
          {report && (
            <div className="small">
              <p>
                {report.imported} imported · {report.duplicates} matched ·{" "}
                {report.invalid} skipped
              </p>
              {report.errors.length > 0 && (
                <details>
                  <summary>Skipped row details</summary>
                  <ul>
                    {report.errors.map(({ row, reason }) => (
                      <li key={row}>
                        Row {row} · {reason}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
          )}
          {parsedCSV instanceof Error && (
            <p role="alert">{parsedCSV.message}</p>
          )}
          {!!csvData?.errors.length && (
            <p role="status">
              {csvData.errors.length} malformed{" "}
              {csvData.errors.length === 1 ? "row will" : "rows will"} be
              skipped.
            </p>
          )}
          {/* Below the buttons: the guessed map arrives after the CSV is read and must not move Import leads. */}
          <ColumnMap
            headers={headers}
            value={columnMap}
            sample={csvData?.rows[0]}
            onChange={(map) => setMappedColumns({ headerKey, map })}
          />
        </div>
      </section>
      {packet && (
        <div className="modal-shade">
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label="Lead packet"
          >
            <div className="modal-heading">
              <h2>{packet.lead.name}</h2>
              <button className="secondary small" onClick={closePacket}>
                Close
              </button>
            </div>
            <p>
              {packet.lead.company} · {packet.lead.phone}
            </p>
            <div className="section-rule" />
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
            <h3>
              {packet.script.fallback
                ? "Base script preview"
                : "Script for this lead"}
            </h3>
            <p className="fine">
              {packet.script.fallback
                ? packet.preparation?.status === "skipped" &&
                  packet.preparation.reason
                  ? `Script skipped. ${packet.preparation.reason}`
                  : SCRIPT_STATUS[packet.preparation?.status ?? "missing"]
                : Object.keys(packet.script.written ?? {}).length
                  ? "Script ready"
                  : "Script ready. Uses the wording from your master script."}
            </p>
            {packet.last_generated_script && (
              <details>
                <summary>Last generated script · Stale</summary>
                {packet.last_generated_script.historical_reconstructed && (
                  <p className="fine">
                    Reconstructed from the saved selections and the matching
                    master.
                  </p>
                )}
                <ScriptReader script={packet.last_generated_script} />
              </details>
            )}
            <ScriptReader script={packet.script} />
            <h3>What the agent found</h3>
            {packet.script.summary && <p>{packet.script.summary}</p>}
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
            <>
              <h3>Endpoint correction</h3>
              <p className="muted small">
                Wrong-party endpoints stay suppressed. Attach a verified
                replacement endpoint to continue this lead.
              </p>
              <EndpointCorrection lead={packet.lead} onDone={closePacket} />
              {packet.lead.suppressed && (
                <HoldRelease phone={packet.lead.phone} onDone={closePacket} />
              )}
            </>
          </section>
        </div>
      )}
      {/* Last: it grows while research runs, so nothing below it moves under the pointer. */}
      <PreparationPanel onNavigate={onNavigate} />
    </>
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
  const { snapshot: w, api, run, refresh, notice, subscribe } = useWorkspace();
  const history = useHistory("numbers", w.numbers);
  const latestHistory = useRef(history);
  latestHistory.current = history;
  useEffect(
    () =>
      subscribe((events) => {
        if (events.some((event) => event.type === "number.updated"))
          void latestHistory.current.load().catch(() => {});
      }),
    [subscribe],
  );
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
            New standalone numbers start at up to 15 outbound attempts a day and
            can rise to 90 with use. Shared numbers allow up to 90 outbound
            attempts per rolling 24 hours. Incoming callbacks do not count
            toward that limit.
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
        <div className="table-scroll">
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
        </div>
        <HistoryMore history={history} label="Load more numbers" />
        {!history.items.length && (
          <Empty text="Shared caller IDs appear here after your calling profile connects. Check Settings → Call connection if the list stays empty." />
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

/** One number (R10.7): whether it can dial now, its calls today against its cap, and the one control it needs. */
function NumberRow({
  number,
  onSaved,
}: {
  number: NumberRecord;
  onSaved: (number: NumberRecord) => void;
}) {
  const { api, refresh, run } = useWorkspace();
  const [busy, setBusy] = useState(false);
  async function act(work: () => Promise<NumberRecord>) {
    setBusy(true);
    try {
      onSaved(await work());
      await refresh();
    } finally {
      setBusy(false);
    }
  }
  const paused = number.lifecycle === "paused_for_outbound";
  if (number.shared_pool) {
    return (
      <tr>
        <td>
          <strong>{number.phone}</strong>
          <small>
            {number.outbound_ready ? "Ready to dial" : "Outbound unavailable"}
          </small>
        </td>
        <td>{number.region}</td>
        <td>Shared pool</td>
        <td>
          {number.shared_used ?? 0} / {number.daily_limit} outbound
        </td>
        <td colSpan={2}>Callbacks always routed separately</td>
      </tr>
    );
  }
  return (
    <tr>
      <td>
        <strong className="table-phone">{number.phone}</strong>
        <small>
          {number.outbound_ready
            ? "Ready to dial"
            : paused
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
        {number.today_limit ?? number.daily_limit} today · {number.daily_limit}{" "}
        max
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
                  void run(() =>
                    act(() =>
                      api<NumberRecord>(
                        `/v1/admin/numbers/${number.id}/configure`,
                        "POST",
                      ),
                    ),
                  )
                }
              >
                {number.configuration_status === "uncertain"
                  ? "Reconcile routing"
                  : "Set up route"}
              </button>
            )}
          {number.provider_id && number.provider_present === false && (
            <small>
              Not found in your Telnyx account. Check your Telnyx numbers before
              setting up its route.
            </small>
          )}
          {paused ? (
            <button
              className="text-button small"
              disabled={busy}
              onClick={() =>
                void run(() =>
                  act(() =>
                    api<NumberRecord>(
                      `/v1/admin/numbers/${number.id}/resume`,
                      "POST",
                    ),
                  ),
                )
              }
            >
              Resume
            </button>
          ) : (
            <button
              className="text-button small"
              disabled={busy}
              onClick={() =>
                void run(() =>
                  act(() =>
                    api<NumberRecord>(
                      `/v1/admin/numbers/${number.id}`,
                      "PATCH",
                      {
                        lifecycle: "paused_for_outbound",
                        reason: "Paused by the rep; callbacks still ring",
                      },
                    ),
                  ),
                )
              }
            >
              Pause outbound
            </button>
          )}
        </div>
      </td>
    </tr>
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
      <details className="card padded">
        <summary>What's working</summary>
        <ScriptMemory />
      </details>
      <details className="card padded">
        <summary>AI usage and script rewrites</summary>
        <TokenUsage />
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
