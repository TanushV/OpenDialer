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
  Sparkles,
  Upload,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useHistory } from "../state/useHistory";
import { HistoryMore } from "./HistoryMore";
import { openSource } from "../api/client";
import { date, label, useWorkspace } from "../context";
import { parseCSV } from "../state/csv";
import type { Lead, NumberRecord, Packet } from "../types";
import { Procurement, Variants, DeferredScripts } from "./AdminWorkflows";
import { AdvancedIntelligence } from "./AdvancedIntelligence";
import { NumberOrders } from "./NumberOrders";
import { FollowupTasks } from "./FollowupTasks";
import { ProspectBrief } from "./ProspectBrief";
import { PreparationRunway } from "./Preparation";

export function LeadsPage() {
  const { snapshot: w, api, run, refresh, notice } = useWorkspace();
  const [csv, setCSV] = useState("");
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
          <span className="eyebrow">THE WHOLE COHORT</span>
          <h1>
            Good conversations start here<span className="soft-dot">.</span>
          </h1>
          <p>
            Every lead stays visible. Context improves; coverage stays
            accountable.
          </p>
        </div>
        <button
          className="primary"
          onClick={() =>
            void run(async () => {
              await api("/v1/research/batches", "POST", {
                lead_ids: leads
                  .filter((lead) => !lead.suppressed)
                  .map((lead) => lead.id),
              });
              notice(
                "Preparation queued. Lead packets will update as work completes.",
              );
              await refresh();
            })
          }
        >
          <Sparkles size={16} />
          Prepare displayed lead packets
        </button>
      </div>
      <PreparationRunway />
      <section className="card table-card">
        <div className="table-toolbar">
          <h3>
            {history.total ?? w.leads.length} leads{" "}
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
                </td>
                <td>
                  {lead.phone}
                  <small>{lead.timezone || "Timezone needs review"}</small>
                </td>
                <td>
                  <span className={`pill ${lead.suppressed ? "red" : "green"}`}>
                    {lead.suppressed ? "Suppressed" : label(lead.status)}
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
        <p className="fine">
          Preparation above applies to displayed leads. Advance preparation uses
          the configured ready-queue target across the cohort.
        </p>
      </section>
      <section className="card import-card">
        <div>
          <span className="eyebrow">BRING YOUR OWN LIST</span>
          <h2>Import a lead cohort</h2>
          <p className="muted">
            Use headers: name, phone, company, domain, timezone, region,
            segment. Suppressions are preserved across every import.
          </p>
        </div>
        <div>
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
                  if (file) void file.text().then(setCSV);
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
                  }>("/v1/leads/import", "POST", {
                    campaign_id: w.campaign.id,
                    rows: parseCSV(csv),
                  });
                  setReport(report);
                  await refresh();
                  notice(`${report.imported} leads imported`);
                })
              }
            >
              Import leads
              <ArrowRight size={15} />
            </button>
          </div>
          {report && (
            <p className="small">
              {report.imported} imported · {report.duplicates} duplicate rows ·{" "}
              {report.invalid} invalid endpoints
            </p>
          )}
        </div>
      </section>
      {packet && (
        <div className="modal-shade">
          <section className="modal">
            <button className="close-modal" onClick={() => setPacket(null)}>
              Close
            </button>
            <span className="eyebrow">LEAD PACKET</span>
            <h2>{packet.lead.name}</h2>
            <p>
              {packet.lead.company} · {packet.lead.phone}
            </p>
            <div className="section-rule" />
            <h3>Approved opener</h3>
            <p>{packet.script.opener}</p>
            <h3>Verified facts</h3>
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
            <h3>Endpoint correction</h3>
            <p className="muted small">
              Wrong-party endpoints stay suppressed. Attach a verified
              replacement endpoint to continue this lead.
            </p>
            <EndpointCorrection
              lead={packet.lead}
              onDone={() => setPacket(null)}
            />
          </section>
        </div>
      )}
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

interface Proposal {
  contingent_capacity?: {
    status: string;
    low?: RetryCapacity;
    mean?: RetryCapacity;
    high?: RetryCapacity;
  };
  expansion_review?: {
    status: string;
    reason: string;
    next_review_at: string | null;
  };
  regions: {
    region: string;
    eligible_leads: number;
    healthy_existing: number;
    required: number;
    proposed_additional: number | null;
  }[];
  monthly_rental_estimate_usd: number | null;
  existing_only_capacity: number;
  staffing_shortfall: number | null;
  owner_unavailable_attempts?: number | null;
  unavailable_staff_count?: number;
  occupied_rep_count?: number;
  number_shortfall: number | null;
  minimum_additional_numbers: number | null;
  proposed_additional_numbers: number | null;
  forecast_attempts: number;
  optimality_scope: string;
  optimality?: string;
  schedule_scope?: string;
  binding_shortfall?: number | null;
  setup_shortfall?: number | null;
  planning_limited?: boolean;
  existing_schedule?: { lead_id: string; number_id: string; at: string }[];
  lower_bound?: number;
  optimality_gap?: number;
  best_found_additional_numbers?: number;
  existing_plus_addition_capacity?: number;
  conditional_pending_schedule?: unknown[];
  contingent_retry_low_mean_high?: {
    low: number;
    mean: number;
    high: number;
    support: number;
    omitted_leads: number;
  };
  assumptions?: {
    staffing_grid_seconds: number;
    handling_seconds: number;
    account_channel_limit: number | null;
    additional_ready_after_hours: number;
  };
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
interface PlanningProjection {
  status: "pending" | "stale" | "ready";
  job_id?: string | null;
  as_of?: string | null;
  refresh_status?: string | null;
  refresh_error?: string | null;
  attempts?: number | null;
}
type ProposalResponse =
  | (Proposal & { projection?: PlanningProjection })
  | { projection: PlanningProjection };
export function NumbersPage() {
  const { snapshot: w, api, run, refresh, notice } = useWorkspace();
  const history = useHistory("numbers", w.numbers);
  const [response, setResponse] = useState<ProposalResponse | null>(null);
  const proposal =
    response && "forecast_attempts" in response ? response : null;
  const projection = response?.projection;
  const [planning, setPlanning] = useState(false);
  const requestVersion = useRef(0);
  const loadProposal = useCallback(async () => {
    const version = ++requestVersion.current;
    setPlanning(true);
    try {
      const value = await api<ProposalResponse>(
        "/v1/admin/numbers/proposal",
        "POST",
        { campaign_id: w.campaign.id },
      );
      if (version === requestVersion.current) setResponse(value);
    } finally {
      if (version === requestVersion.current) setPlanning(false);
    }
  }, [api, w.campaign.id]);
  useEffect(
    () => () => {
      requestVersion.current++;
    },
    [],
  );
  useEffect(() => {
    if (
      !projection ||
      projection.status === "ready" ||
      projection.refresh_status === "dead"
    )
      return;
    const timer = setTimeout(() => void run(loadProposal), 2000);
    return () => clearTimeout(timer);
  }, [projection, loadProposal, run]);
  const [phone, setPhone] = useState("");
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">A CONSISTENT CALLING IDENTITY</span>
          <h1>Numbers &amp; callbacks</h1>
          <p>
            Use owned numbers, keep familiar identities, and stay reachable.
          </p>
        </div>
        <button
          className="primary"
          disabled={planning}
          onClick={() => void run(loadProposal)}
        >
          <Layers3 size={17} />
          Plan regional capacity
        </button>
      </div>
      <OptimizationPanel numbersOnly />
      <div className="number-summary">
        <div className="card">
          <Radio size={23} />
          <strong>{history.total ?? history.items.length}</strong>
          <p>Owned numbers in inventory</p>
        </div>
        <div className="card">
          <ShieldCheck size={23} />
          <strong>
            {history.items.filter((n) => n.outbound_ready).length}
          </strong>
          <p>Ready in displayed inventory</p>
        </div>
        <div className="card">
          <RefreshCw size={23} />
          <strong>Stable</strong>
          <p>Prospect-to-number assignments</p>
        </div>
      </div>
      <section className="card padded" aria-label="Displayed inventory status">
        <dl className="capacity-summary">
          <div>
            <dt>Ready</dt>
            <dd>{history.items.filter((n) => n.outbound_ready).length}</dd>
          </div>
          <div>
            <dt>Pending setup</dt>
            <dd>
              {
                history.items.filter((n) =>
                  [
                    "purchased",
                    "identity_configured",
                    "callback_tested",
                  ].includes(n.lifecycle),
                ).length
              }
            </dd>
          </div>
          <div>
            <dt>Limited use</dt>
            <dd>
              {
                history.items.filter((n) => n.lifecycle === "limited_real_use")
                  .length
              }
            </dd>
          </div>
          <div>
            <dt>Paused outbound</dt>
            <dd>
              {
                history.items.filter((n) =>
                  ["paused_for_outbound", "retired_callback_only"].includes(
                    n.lifecycle,
                  ),
                ).length
              }
            </dd>
          </div>
          <div>
            <dt>Under investigation</dt>
            <dd>
              {
                history.items.filter((n) => n.lifecycle === "investigating")
                  .length
              }
            </dd>
          </div>
        </dl>
        <p className="fine">
          Counts cover displayed inventory. Ready may include reviewed limited
          use. Pending orders appear below; inbound identities remain retained.
        </p>
      </section>
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
                onSaved={history.update}
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
            New inventory moves through identity setup, a real inbound callback
            test, and limited legitimate use. Evidence is required before each
            promotion.
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
      {projection && (
        <section className="card padded" aria-label="Capacity planning status">
          <p role="status">
            {projection.refresh_status === "dead"
              ? "Capacity planning failed. Retry the saved job after reviewing its error."
              : projection.status === "pending"
                ? "Capacity plan is being prepared in the background."
                : projection.status === "stale"
                  ? "Showing a saved capacity plan while current inputs are recalculated."
                  : "Capacity plan is current."}
          </p>
          {projection.as_of && (
            <p className="fine">Calculated {date(projection.as_of)}.</p>
          )}
          {projection.refresh_error && (
            <p className="notice">{projection.refresh_error}</p>
          )}
          <button
            className="secondary"
            disabled={planning}
            onClick={() => void run(loadProposal)}
          >
            Check capacity plan
          </button>
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
                  await loadProposal();
                })
              }
            >
              Retry capacity planning
            </button>
          )}
        </section>
      )}
      {proposal && (
        <section className="card proposal" data-testid="capacity-proposal">
          <span className="eyebrow">PLAN BEFORE PROCUREMENT</span>
          <h2>Regional capacity proposal</h2>
          {proposal.schedule_scope === "declared_staffed_windows" && (
            <p className="pill subtle">Declared staffed periods</p>
          )}
          {proposal.schedule_scope === "normalized_simple_windows" && (
            <p className="pill subtle">Saved simple staffing schedule</p>
          )}
          {proposal.contingent_retry_low_mean_high && (
            <p className="fine">
              Known next attempts are separate from uncertain later retries.
              Neither schedule authorizes a call.
            </p>
          )}
          {proposal.planning_limited && (
            <p className="notice">
              The bounded planning pass is incomplete. Shown schedules are
              feasible examples; a minimum or remaining bottleneck has not been
              established. Narrow the horizon or cohort to review a smaller
              plan.
            </p>
          )}
          <dl className="capacity-summary">
            <div>
              <dt>Due attempt demand</dt>
              <dd>{proposal.forecast_attempts}</dd>
            </div>
            <div>
              <dt>Existing-only capacity</dt>
              <dd>{proposal.existing_only_capacity}</dd>
            </div>
            <div>
              <dt>Staffing shortfall</dt>
              <dd>{proposal.staffing_shortfall ?? "Not established"}</dd>
            </div>
            <div>
              <dt>Number shortfall</dt>
              <dd>{proposal.number_shortfall ?? "Not established"}</dd>
            </div>
            <div>
              <dt>
                {proposal.optimality === "best_feasible_found"
                  ? "Best additional-number proposal"
                  : "Minimum additional numbers"}
              </dt>
              <dd>
                {proposal.minimum_additional_numbers ??
                  proposal.best_found_additional_numbers ??
                  "Not established"}
              </dd>
            </div>
            {proposal.schedule_scope && (
              <>
                <div>
                  <dt>Callbacks without owner availability</dt>
                  <dd>
                    {proposal.owner_unavailable_attempts ?? "Not established"}
                  </dd>
                </div>
                <div>
                  <dt>Stable-identity shortfall</dt>
                  <dd>{proposal.binding_shortfall ?? "Not established"}</dd>
                </div>
                <div>
                  <dt>Setup-delay shortfall</dt>
                  <dd>{proposal.setup_shortfall ?? "Not established"}</dd>
                </div>
              </>
            )}
          </dl>
          {proposal.contingent_retry_low_mean_high && (
            <dl className="capacity-summary">
              <div>
                <dt>Possible later retries</dt>
                <dd>
                  {proposal.contingent_retry_low_mean_high.low.toFixed(1)}–
                  {proposal.contingent_retry_low_mean_high.high.toFixed(1)};
                  mean {proposal.contingent_retry_low_mean_high.mean.toFixed(1)}
                </dd>
              </div>
              <div>
                <dt>Observed retry support</dt>
                <dd>
                  {proposal.contingent_retry_low_mean_high.support ||
                    "Cold prior; no matured observations"}
                </dd>
              </div>
              <div>
                <dt>Capacity with hypothetical additions</dt>
                <dd>
                  {proposal.existing_plus_addition_capacity ??
                    "Not established"}
                </dd>
              </div>
              <div>
                <dt>Pending inventory scenario</dt>
                <dd>
                  {proposal.conditional_pending_schedule?.length ??
                    "Not established"}{" "}
                  displayed attempts, conditional on setup
                </dd>
              </div>
              <div>
                <dt>Proven addition lower bound / gap</dt>
                <dd>
                  {proposal.lower_bound ?? "Unknown"} /{" "}
                  {proposal.optimality_gap ?? "Unknown"}
                </dd>
              </div>
            </dl>
          )}
          {!!proposal.contingent_retry_low_mean_high?.omitted_leads && (
            <p className="notice">
              Retry scenarios are incomplete:{" "}
              {proposal.contingent_retry_low_mean_high.omitted_leads} leads
              omitted from this bounded calculation.
            </p>
          )}
          {proposal.contingent_capacity && (
            <section>
              <h3>Later capacity scenarios</h3>
              <p className="small muted">
                Conditional retries use the same rep time, number limits and
                stable caller identity. Whole attempts are rounded by segment
                and attempt. These are scenarios, not reservations or a reason
                to purchase numbers now. Unserved work may need more time or
                reviewed workload; another number cannot replace a bound
                identity.
              </p>
              {proposal.contingent_capacity.status === "not_needed" ? (
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
                      const row = proposal.contingent_capacity?.[name];
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
          {proposal.assumptions && (
            <p className="fine">
              Grid: {proposal.assumptions.staffing_grid_seconds}s. Cold work
              assumption: {proposal.assumptions.handling_seconds}s; observed
              estimates replace it when available. Setup delay:{" "}
              {proposal.assumptions.additional_ready_after_hours}h. Account
              channels:{" "}
              {proposal.assumptions.account_channel_limit ??
                "unknown; not provider-verified"}
              .
            </p>
          )}
          {proposal.expansion_review && (
            <div>
              <h3>Expansion review</h3>
              <p>
                {label(proposal.expansion_review.reason)}.{" "}
                {proposal.expansion_review.status === "observing" &&
                proposal.expansion_review.next_review_at
                  ? `Review again after ${date(proposal.expansion_review.next_review_at)}. `
                  : ""}
                New rentals remain blocked; this advice does not purchase
                capacity.
              </p>
            </div>
          )}
          {!!proposal.unavailable_staff_count && (
            <p className="notice">
              {proposal.unavailable_staff_count} configured{" "}
              {proposal.unavailable_staff_count === 1 ? "rep is" : "reps are"}{" "}
              unavailable; their time is excluded.
            </p>
          )}
          {!!proposal.occupied_rep_count && (
            <p className="notice">
              {proposal.occupied_rep_count}{" "}
              {proposal.occupied_rep_count === 1 ? "rep has" : "reps have"} an
              existing call or reservation; occupied time is excluded.
            </p>
          )}
          <p className="fine">
            {proposal.optimality_scope}. Pending inventory requires review
            before any addition; no predicted conversion lift is assumed.
          </p>
          <table>
            <thead>
              <tr>
                <th>REGION</th>
                <th>ELIGIBLE LEADS</th>
                <th>HEALTHY EXISTING</th>
                <th>ADDITIONAL PROPOSED</th>
              </tr>
            </thead>
            <tbody>
              {proposal.regions.map((r) => (
                <tr key={r.region}>
                  <td>{r.region}</td>
                  <td>{r.eligible_leads}</td>
                  <td>{r.healthy_existing}</td>
                  <td>{r.proposed_additional ?? "Not established"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="small muted">
            Indicative monthly rental:{" "}
            {proposal.monthly_rental_estimate_usd == null
              ? "Not established"
              : `$${proposal.monthly_rental_estimate_usd.toFixed(2)}`}
            . New rentals are blocked under the no-subscription policy.
            Additional shared numbers are planning scenarios only. Existing
            rental obligations remain.
          </p>
        </section>
      )}
      <Procurement />
      <NumberOrders />
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
  const [view, setView] = useState<NumberEvidence | null>(null);
  const [reference, setReference] = useState("");
  const [limit, setLimit] = useState(String(number.daily_limit));
  const [decision, setDecision] = useState("resume");
  const [leadId, setLeadId] = useState("");
  const [targetId, setTargetId] = useState("");
  const [external, setExternal] = useState("");
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
      await api<NumberRecord>(path, method, {
        ...value,
        request_id: request.current.id,
      });
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
                  Pickup difference versus frozen expectations:{" "}
                  {(100 * (view.health.case_mix_residual_mean ?? 0)).toFixed(1)}{" "}
                  percentage points; approximate interval{" "}
                  {view.health.case_mix_interval
                    .map((v) => (100 * v).toFixed(1))
                    .join(" to ")}
                  . This is an investigation signal.
                </p>
              )}
              <p>
                {current.external_reputation?.spam_label
                  ? `Imported external label: ${current.external_reputation.spam_label}`
                  : "External label unknown"}
              </p>
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
                      min={1}
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
                  {view.workload.maximum_reviewed_limit} attempts per day.{" "}
                  Suggested limit for explicit review:{" "}
                  {view.workload.recommended_limit}. Sparse evidence is unknown;
                  it does not require a fixed number of warmup calls.
                </p>
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
                <details>
                  <summary>
                    Import authorized external reputation evidence
                  </summary>
                  <p className="fine">
                    JSON with source, ownership_reference, observed_at,
                    expires_at and nullable spam_label. No provider request or
                    enrollment.
                  </p>
                  <label>
                    External reputation evidence
                    <textarea
                      rows={4}
                      value={external}
                      onChange={(e) => setExternal(e.target.value)}
                    />
                  </label>
                  <button
                    disabled={!external.trim()}
                    onClick={() =>
                      void run(() =>
                        save(
                          `/v1/admin/numbers/${number.id}`,
                          "PATCH",
                          { external_reputation: JSON.parse(external) },
                          current.version,
                        ),
                      )
                    }
                  >
                    Save imported observation
                  </button>
                </details>
              </fieldset>
            </section>
          </td>
        </tr>
      )}
    </>
  );
}
export function IntelligencePage() {
  const { snapshot: w, api, run, refresh, notice } = useWorkspace();
  const jobsHistory = useHistory("jobs", w.jobs);
  const modelHistory = useHistory("models", w.models);
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">LEARN WITHOUT LOSING THE THREAD</span>
          <h1>
            Intelligence with evidence<span className="soft-dot">.</span>
          </h1>
          <p>
            Approved language, reviewable analysis, and accountable experiments.
          </p>
        </div>
      </div>
      <OptimizationPanel />
      <div className="two-column">
        <ProductKnowledge />
        <section className="card padded">
          <span className="eyebrow">FOLLOW-THROUGH</span>
          <h2>Promises worth keeping</h2>
          <FollowupTasks />
          <div className="section-rule" />
          <span className="eyebrow">COVERAGE BEFORE PROPENSITY</span>
          <p className="muted small">
            Requested callbacks come first, then requested contacts and coverage
            obligations. Low predicted pickup cannot hide a lead. Delayed
            outcome windows stay pending until mature.
          </p>
        </section>
      </div>
      <section className="card table-card">
        <div className="table-toolbar">
          <h3>Preparation &amp; analysis jobs</h3>
          <button
            className="secondary small"
            onClick={() =>
              void run(async () => {
                const r = await api<{ processed: number }>(
                  "/v1/admin/jobs/run",
                  "POST",
                );
                await refresh();
                notice(`${r.processed} queued jobs processed`);
              })
            }
          >
            Process queued work
          </button>
        </div>
        <table>
          <thead>
            <tr>
              <th>LANE</th>
              <th>STATUS</th>
              <th>ATTEMPTS</th>
              <th>DETAIL</th>
            </tr>
          </thead>
          <tbody>
            {jobsHistory.items.map((j) => (
              <tr key={j.id}>
                <td>{label(j.queue)}</td>
                <td>
                  <span className="pill subtle">{label(j.status)}</span>
                </td>
                <td>{j.attempts}</td>
                <td>{j.error || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <HistoryMore history={jobsHistory} label="Load more jobs" />
        {!jobsHistory.items.length && (
          <Empty text="Preparation and analysis run in bounded, durable work units." />
        )}
      </section>
      <section className="card padded">
        <span className="eyebrow">MODEL REGISTRY</span>
        <h2>A baseline you can compare against</h2>
        <p className="muted">
          The scheduler starts with coverage rules and pooled estimates.
          Challengers need chronological evaluation, calibration, a controlled
          outcome trial, and manual activation.
        </p>
        {modelHistory.items.length ? (
          modelHistory.items.map((m) => (
            <p key={m.id}>
              {m.active ? "Active" : "Challenger"} · Brier{" "}
              {m.metrics.brier?.toFixed(4)} · {m.checksum.slice(0, 12)}
            </p>
          ))
        ) : (
          <span className="pill green">Coverage-first baseline active</span>
        )}
        <HistoryMore history={modelHistory} label="Load more models" />
        <p className="fine">
          Observed associations are correlations. Emotion, honesty, and
          personality inference are excluded.
        </p>
      </section>
      <Variants />
      <DeferredScripts />
      <AdvancedIntelligence />
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
import { OptimizationPanel } from "./OptimizationPanel";
