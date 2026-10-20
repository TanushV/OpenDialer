import { useEffect, useRef, useState } from "react";
import { label, useWorkspace } from "../context";
import { useDefinitions } from "../state/definitions";

interface DeferredBatch {
  id: string;
  version: number;
  status: string;
  provider_batch_id: string | null;
  lead_count: number;
  published: string[];
  retry_lead_ids: string[];
  pending_review_lead_ids?: string[];
  ready_at?: string | null;
  can_reconcile: boolean;
  reason: string;
}
type BatchList = { batches: DeferredBatch[]; limited: boolean };

export function DeferredScripts() {
  const { api, snapshot: w, refresh } = useWorkspace();
  const [data, setData] = useState<BatchList | null>(null);
  const bounds = useDefinitions();
  const batchLimit = bounds?.deferred_batch?.lead_ids?.max_items ?? 0;
  const [references, setReferences] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [accepted, setAccepted] = useState<Record<string, boolean>>({});
  const [queued, setQueued] = useState<Record<string, boolean>>({});
  const [deadline, setDeadline] = useState(() => {
    const date = new Date(Date.now() + 48 * 3600000);
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000)
      .toISOString()
      .slice(0, 16);
  });
  const submission = useRef<{
    key: string;
    leads: string[];
    ready_at: string;
  } | null>(null);
  const read = () => api<BatchList>("/v1/admin/batches");
  useEffect(() => {
    let current = true;
    void read()
      .then((value) => {
        if (current) setData(value);
      })
      .catch((reason: unknown) => {
        if (current) setError(String(reason));
      });
    return () => {
      current = false;
    };
  }, [api]);
  async function perform(work: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await work();
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      try {
        setData(await read());
      } catch (reason) {
        setError((old) => old || String(reason));
      }
      setBusy(false);
    }
  }
  const unfinished = data?.batches.some(
    (item) =>
      !["completed", "expired", "cancelled", "failed"].includes(item.status),
  );
  const eligible = w.leads.filter((lead) => !lead.suppressed);
  // One deferred batch carries at most this many leads; the rest wait for the next.
  const leads = eligible
    .slice(0, batchLimit)
    .map((lead) => lead.id)
    .sort();
  const soon =
    !submission.current &&
    (!Number.isFinite(Date.parse(deadline)) ||
      Date.parse(deadline) < Date.now() + 24 * 3600000);
  const blocked =
    !data || !bounds
      ? "Loading saved batches…"
      : unfinished
        ? "Resolve the existing batch before another bulk submission. Immediate preparation remains available in Leads."
        : !leads.length
          ? "No displayed lead is eligible for deferred preparation."
          : soon
            ? "Deferred preparation needs a deadline at least 24 hours from now; use Leads for sooner work."
            : "";
  return (
    <section className="card padded" aria-label="Deferred script preparation">
      <span className="eyebrow">PREPARE AHEAD OF THE SHIFT</span>
      <h2>Deferred script preparation</h2>
      <p className="muted small">
        Prepare researched packets through the metered OpenAI route when work is
        needed at least 24 hours from now. Live call content is never submitted
        here. Opening this view only reads local records. Publishing novel
        script text may use a budgeted meaning review; uncertain reviews are
        never silently repeated.
      </p>
      <label>
        Scripts needed by
        <input
          aria-label="Scripts needed by"
          type="datetime-local"
          value={deadline}
          disabled={busy || Boolean(submission.current)}
          onChange={(event) => setDeadline(event.target.value)}
        />
      </label>
      <p className="fine">
        Uses this computer's timezone. Immediate preparation is available in
        Leads. A submitted deadline and request identity stay fixed through
        uncertain retries.
      </p>
      {error && (
        <p role="status" className="notice">
          {error}
        </p>
      )}
      <p className="fine">
        Submits {leads.length} displayed eligible{" "}
        {leads.length === 1 ? "lead" : "leads"}
        {eligible.length > batchLimit
          ? ` (the first ${batchLimit} of ${eligible.length}; submit the rest after this batch completes)`
          : ""}
        .
      </p>
      <button
        className="secondary"
        disabled={busy || !!blocked}
        onClick={() => {
          if (!submission.current)
            submission.current = {
              key: crypto.randomUUID(),
              leads,
              ready_at: new Date(deadline).toISOString(),
            };
          const intent = submission.current;
          void perform(async () => {
            await api("/v1/admin/batches/scripts", "POST", {
              lead_ids: intent.leads,
              idempotency_key: intent.key,
              ready_at: intent.ready_at,
            });
            submission.current = null;
          });
        }}
      >
        Submit deferred scripts
      </button>
      {blocked && blocked !== "Loading saved batches…" && (
        <p className="fine" role="status">
          {blocked}
        </p>
      )}
      <button
        className="secondary"
        disabled={busy}
        onClick={() => void perform(async () => {})}
      >
        Reload deferred status
      </button>
      {!data && <p>Loading saved batches…</p>}
      {data?.batches.length === 0 && <p>No deferred batches recorded.</p>}
      {data?.limited && (
        <p>
          Showing the oldest 100 pending batches and 20 recent completed
          batches.
        </p>
      )}
      {data?.batches.map((item) => (
        <article className="review-segment" key={item.id}>
          <strong>
            {label(item.status)} · {item.lead_count} lead(s)
          </strong>
          <p className="fine">
            Record {item.id}
            {item.provider_batch_id && ` · Provider ${item.provider_batch_id}`}
          </p>
          {item.reason && <p>{item.reason}</p>}
          <p>
            {item.published.length} published, {item.retry_lead_ids.length} need
            new preparation, {item.pending_review_lead_ids?.length || 0}{" "}
            awaiting background review.
          </p>
          {item.ready_at && (
            <p className="fine">
              Needed by {new Date(item.ready_at).toLocaleString()}
            </p>
          )}
          {queued[item.id] ? (
            <p>Unfinished leads queued for new preparation.</p>
          ) : (
            item.retry_lead_ids.length > 0 && (
              <>
                <label className="check-label">
                  <input
                    type="checkbox"
                    checked={Boolean(accepted[item.id])}
                    onChange={(event) =>
                      setAccepted({
                        ...accepted,
                        [item.id]: event.target.checked,
                      })
                    }
                  />
                  Authorize new budgeted preparation for these unfinished leads
                </label>
                <button
                  className="secondary"
                  disabled={busy || !accepted[item.id]}
                  onClick={() =>
                    void perform(async () => {
                      await api("/v1/research/batches", "POST", {
                        lead_ids: item.retry_lead_ids,
                      });
                      setQueued((old) => ({ ...old, [item.id]: true }));
                    })
                  }
                >
                  Prepare only unfinished leads
                </button>
              </>
            )
          )}
          {!item.provider_batch_id && (
            <label>
              Original provider batch ID
              <input
                aria-label="Original provider batch ID"
                value={references[item.id] || ""}
                onChange={(event) =>
                  setReferences({
                    ...references,
                    [item.id]: event.target.value,
                  })
                }
              />
            </label>
          )}
          <button
            className="secondary"
            disabled={
              busy ||
              !item.can_reconcile ||
              !(item.provider_batch_id || references[item.id]?.trim())
            }
            onClick={() =>
              void perform(() =>
                api(`/v1/admin/batches/${item.id}/reconcile`, "POST", {
                  expected_version: item.version,
                  provider_batch_id:
                    item.provider_batch_id || references[item.id].trim(),
                }),
              )
            }
          >
            Verify and publish original batch
          </button>
        </article>
      ))}
    </section>
  );
}
