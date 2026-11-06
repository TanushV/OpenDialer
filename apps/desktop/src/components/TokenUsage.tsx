import { useEffect, useState } from "react";
import { isAdmin, date, useWorkspace } from "../context";
import { useLoad } from "../state/useLoad";
import { listLabel, useLists } from "./LeadLists";

type Totals = {
  requests: number;
  unknown_requests: number;
  partial_requests: number;
  running: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cached_tokens: number;
  reasoning_tokens: number;
};
type Receipt = {
  id: string;
  provider: string;
  kind: string;
  model: string | null;
  at: string;
  context: {
    base_version?: number;
    lead_ids?: string[];
    research?: string;
    list_names?: string[];
    job_id?: string;
  };
  status: string;
  token_status: string;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_tokens: number | null;
  reasoning_tokens: number | null;
  billing: string;
  attribution: string;
  per_lead_estimated_tokens: number | null;
};
type Report = {
  totals: Totals;
  groups: (Totals & { provider: string; kind: string })[];
  records: Receipt[];
  truncated: boolean;
};
type Estimate = {
  basis: string;
  leads: number;
  measured_leads: number;
  remaining_leads: number;
  estimated_remaining_tokens: number | null;
  estimated_total_tokens: number | null;
  tokens_per_lead_estimate: number | null;
  note: string;
  base_version: number;
};
const count = (value: number | null | undefined) =>
  value == null ? "Unknown" : Math.round(value).toLocaleString();
const kindLabel = (kind: string) =>
  ({
    preparation: "Research and script writing",
    script_review: "Script review",
    live_coach: "Live help",
    conversation_finalize: "Post-call",
    conversation_review: "Conversation review",
  })[kind] ?? kind.replaceAll("_", " ");

export function TokenUsage() {
  const { api, run, notice, snapshot, subscribe } = useWorkspace();
  const [list, setList] = useState<string | null>(null);
  const [version, setVersion] = useState("");
  const [kind, setKind] = useState("");
  const [lead, setLead] = useState("");
  const [refreshResearch, setRefreshResearch] = useState(false);
  const [busy, setBusy] = useState(false);
  const lists = useLists();
  const params = new URLSearchParams({ campaign_id: snapshot.campaign.id });
  if (list !== null) params.set("list_name", list);
  if (version) params.set("base_version", version);
  if (kind) params.set("kind", kind);
  if (lead.trim()) params.set("lead_id", lead.trim());
  const usage = useLoad<Report>(
    isAdmin(snapshot.user.role) ? `/v1/usage/tokens?${params}` : null,
  );
  const estimate = useLoad<Estimate>(
    isAdmin(snapshot.user.role)
      ? `/v1/preparation/estimate${list === null ? "" : `?${new URLSearchParams({ list_name: list })}`}`
      : null,
  );
  async function refresh() {
    await Promise.all([usage.reload(), estimate.reload()]);
  }
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const off = subscribe((events) => {
      if (
        events.some((e) =>
          ["usage.updated", "lead.updated", "optimization.updated"].includes(
            e.type,
          ),
        )
      ) {
        clearTimeout(timer);
        timer = setTimeout(() => {
          void refresh().catch(() => {});
        }, 400);
      }
    });
    return () => {
      off();
      clearTimeout(timer);
    };
  }, [subscribe, usage.reload, estimate.reload]);
  useEffect(() => {
    if (!usage.data?.totals.running) return;
    const timer = setInterval(() => void refresh().catch(() => {}), 8000);
    return () => clearInterval(timer);
  }, [usage.data?.totals.running, usage.reload, estimate.reload]);
  if (!isAdmin(snapshot.user.role)) return null;
  async function rewrite(sample: boolean, remaining = false) {
    setBusy(true);
    try {
      const result = await api<{ marked: number }>(
        "/v1/preparation/rewrite",
        "POST",
        {
          list_name: list,
          refresh_research: refreshResearch,
          sample_size: sample ? 3 : null,
          remaining_only: remaining,
        },
      );
      notice(
        `${sample ? "Sample queued" : "Fresh scripts queued"}: ${result.marked} leads. ${refreshResearch ? "Research will be refreshed." : "Current research will be reused where available."}`,
      );
      await refresh();
    } finally {
      setBusy(false);
    }
  }
  const t = usage.data?.totals;
  return (
    <section className="card padded token-usage" aria-label="Model token usage">
      <div className="section-heading">
        <div>
          <h2>Token usage and fresh scripts</h2>
          <p className="muted">
            Parsing Markdown uses no model tokens. Research, writing, review and
            live help are counted separately when their provider reports usage.
          </p>
        </div>
        <button className="secondary" onClick={() => void run(refresh)}>
          Refresh usage
        </button>
      </div>
      <div className="form-grid">
        <label>
          Lead ID (optional)
          <input
            aria-label="Usage lead ID"
            value={lead}
            onChange={(e) => setLead(e.target.value)}
            placeholder="All leads"
          />
        </label>
        <label>
          Usage list
          <select
            aria-label="Usage list"
            value={list === null ? "__all" : `list:${list}`}
            onChange={(e) =>
              setList(
                e.target.value === "__all" ? null : e.target.value.slice(5),
              )
            }
          >
            <option value="__all">All campaign lists</option>
            {lists.data?.lists.map((l) => (
              <option key={l.name} value={`list:${l.name}`}>
                {listLabel(l.name)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Master version
          <input
            aria-label="Usage master version"
            type="number"
            min="1"
            placeholder="All versions"
            value={version}
            onChange={(e) => setVersion(e.target.value)}
          />
        </label>
        <label>
          Work type
          <select
            aria-label="Usage work type"
            value={kind}
            onChange={(e) => setKind(e.target.value)}
          >
            <option value="">All model work</option>
            {[
              "preparation",
              "script_review",
              "live_coach",
              "conversation_finalize",
              "conversation_review",
            ].map((k) => (
              <option key={k} value={k}>
                {kindLabel(k)}
              </option>
            ))}
          </select>
        </label>
      </div>
      {(usage.error || estimate.error) && (
        <p role="alert">{usage.error || estimate.error}</p>
      )}
      {t && (
        <>
          <dl className="token-totals">
            <div>
              <dt>Input tokens</dt>
              <dd>{count(t.input_tokens)}</dd>
            </div>
            <div>
              <dt>Output tokens</dt>
              <dd>{count(t.output_tokens)}</dd>
            </div>
            <div>
              <dt>Total tokens</dt>
              <dd>{count(t.total_tokens)}</dd>
            </div>
            <div>
              <dt>Cached input</dt>
              <dd>{count(t.cached_tokens)}</dd>
            </div>
            <div>
              <dt>Reasoning output</dt>
              <dd>{count(t.reasoning_tokens)}</dd>
            </div>
          </dl>
          <p className="fine">
            {t.requests} requests · {t.running} running · {t.unknown_requests}{" "}
            with unknown counters · {t.partial_requests} partial. Cached and
            reasoning counts are breakdowns; they are not added again to the
            total.
          </p>
        </>
      )}
      <p className="fine">
        Historical preparation usage before 0.10.0 was not saved. Missing usage
        is unknown, not zero. Subscription inclusion describes billing, not
        token consumption or remaining plan allowance.
      </p>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>WORK</th>
              <th>PROVIDER / MODEL</th>
              <th>VERSION</th>
              <th>INPUT / OUTPUT</th>
              <th>STATE / BILLING</th>
            </tr>
          </thead>
          <tbody>
            {usage.data?.records.map((r) => (
              <tr key={r.id}>
                <td>
                  {kindLabel(r.kind)}
                  <small>{date(r.at)}</small>
                </td>
                <td>
                  {r.provider}
                  <small>{r.model ?? "Model not recorded"}</small>
                </td>
                <td>
                  {r.context.base_version ?? "Not recorded"}
                  <small>
                    {r.context.list_names?.map(listLabel).join(", ")}
                  </small>
                  {r.context.job_id && <small>Job {r.context.job_id}</small>}
                </td>
                <td>
                  {count(r.input_tokens)} / {count(r.output_tokens)}
                  {r.attribution === "shared_thread" && (
                    <small>
                      Shared thread · {count(r.per_lead_estimated_tokens)}{" "}
                      tokens/lead allocated estimate
                    </small>
                  )}
                </td>
                <td>
                  {r.status} · {r.token_status}
                  <small>{r.billing.replaceAll("_", " ")}</small>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {usage.data?.truncated && (
        <p className="fine">
          Showing the latest 100 receipts. Totals include every matching
          request.
        </p>
      )}
      <h3>Rewrite from the current master</h3>
      <p>
        New scripts start from your latest master and verified facts. Earlier
        personalized wording is not reused. Current calls keep their original
        script.
      </p>
      <label>
        Research for this rewrite
        <select
          aria-label="Research for rewrite"
          value={refreshResearch ? "refresh" : "reuse"}
          onChange={(e) => setRefreshResearch(e.target.value === "refresh")}
        >
          <option value="reuse">Reuse current research</option>
          <option value="refresh">Refresh research and rewrite</option>
        </select>
      </label>
      {estimate.data && (
        <p>
          {estimate.data.leads} active leads · master v
          {estimate.data.base_version}.{" "}
          {estimate.data.basis === "measured"
            ? `Measured from ${estimate.data.measured_leads} leads: about ${count(estimate.data.tokens_per_lead_estimate)} tokens per lead; ${count(estimate.data.estimated_total_tokens)} estimated for this scope.`
            : "No measured estimate for this master yet. Run a small sample first."}{" "}
          {estimate.data.remaining_leads} still need this master;{" "}
          {count(estimate.data.estimated_remaining_tokens)} estimated tokens
          remaining.
        </p>
      )}
      <p className="fine">
        Shared research is allocated as an estimate per lead. Searches, retries
        and independent reviews can change actual totals.
      </p>
      <div className="button-row">
        <button
          className="secondary"
          disabled={busy}
          onClick={() => void run(() => rewrite(true))}
        >
          Measure a 3-lead sample
        </button>
        <button
          className="secondary"
          disabled={busy || !estimate.data?.remaining_leads}
          onClick={() => void run(() => rewrite(false, true))}
        >
          Rewrite remaining leads
        </button>
        <button
          className="primary"
          disabled={busy}
          onClick={() => void run(() => rewrite(false))}
        >
          {refreshResearch
            ? "Refresh research and rewrite all active leads"
            : "Rewrite all active leads"}
        </button>
      </div>
    </section>
  );
}
