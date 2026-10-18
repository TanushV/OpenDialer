import { parseCSV } from "../state/csv";
import { useEffect, useState } from "react";
import { label, useWorkspace } from "../context";

interface Entry {
  id: string;
  version: number;
  kind: string;
  provider: string | null;
  request_key: string | null;
  request_id: string | null;
  status: string;
  reserved_usd: number;
  cost_usd: number | null;
  credited_usd: number;
  net_cost_usd: number | null;
  provider_record_ids: string[];
  review_reason: string | null;
  receipt_id: string | null;
  source_reference: string | null;
  can_settle: boolean;
  can_credit: boolean;
  can_close_call: boolean;
}
type Ledger = { entries: Entry[]; next_cursor: string | null };
const money = (value: number | null) =>
  value === null ? "Unknown" : `$${value.toFixed(6)}`;

export function BillingReconciliation() {
  const { api, refresh } = useWorkspace();
  const [filter, setFilter] = useState("unresolved");
  const [cursor, setCursor] = useState("");
  const [data, setData] = useState<Ledger | null>(null);
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let current = true;
    setBusy(true);
    void api<Ledger>(
      `/v1/admin/billing/ledger?status=${filter}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
    )
      .then((value) => {
        if (current) {
          setData(value);
          setSelected((old) =>
            value.entries.some((item) => item.id === old)
              ? old
              : value.entries[0]?.id || "",
          );
          setError("");
        }
      })
      .catch((reason: unknown) => {
        if (current) setError(String(reason));
      })
      .finally(() => {
        if (current) setBusy(false);
      });
    return () => {
      current = false;
    };
  }, [api, filter, cursor, reload]);
  const row = data?.entries.find((item) => item.id === selected);
  return (
    <section className="card padded" aria-label="Provider cost reconciliation">
      <span className="eyebrow">CHECK ORIGINAL RECEIPTS</span>
      <h2>Provider cost reconciliation</h2>
      <p className="muted small">
        Opening this view makes no provider request. Estimates stay reserved
        until the original charge is checked. A partial carrier record leaves
        other leg charges unknown; its remaining estimate may overlap imported
        costs until the complete statement is confirmed.
      </p>
      <CarrierImport
        onSaved={async () => {
          setReload((x) => x + 1);
          await refresh();
        }}
      />
      <label>
        Show costs
        <select
          value={filter}
          onChange={(event) => {
            setFilter(event.target.value);
            setCursor("");
          }}
        >
          <option value="unresolved">Unresolved</option>
          <option value="settled">Settled and credits</option>
          <option value="all">All</option>
        </select>
      </label>
      {error && <p role="status">{error}</p>}
      <button
        className="secondary"
        disabled={busy}
        onClick={() => setReload((x) => x + 1)}
      >
        Reload costs
      </button>
      {cursor && (
        <button
          className="secondary"
          disabled={busy}
          onClick={() => setCursor("")}
        >
          First page
        </button>
      )}
      {data?.next_cursor && (
        <button
          className="secondary"
          disabled={busy}
          onClick={() => setCursor(data.next_cursor || "")}
        >
          Next costs
        </button>
      )}
      {!data && <p>Loading recorded costs…</p>}
      {data?.entries.length === 0 && <p>No costs in this view.</p>}
      {Boolean(data?.entries.length) && (
        <label>
          Original request
          <select
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
          >
            {data?.entries.map((item) => (
              <option key={item.id} value={item.id}>
                {label(item.kind)} · {item.request_key || item.id} ·{" "}
                {label(item.status)}
              </option>
            ))}
          </select>
        </label>
      )}
      {row && (
        <ReceiptForm
          key={`${row.id}:${row.version}`}
          row={row}
          onSaved={async () => {
            setReload((x) => x + 1);
            await refresh();
          }}
        />
      )}
    </section>
  );
}

function ReceiptForm({
  row,
  onSaved,
}: {
  row: Entry;
  onSaved: () => Promise<void>;
}) {
  const { api } = useWorkspace();
  const action = row.can_close_call
    ? "close_call"
    : row.can_credit
      ? "credit"
      : "settle";
  const [receipt, setReceipt] = useState("");
  const [source, setSource] = useState("");
  const [amount, setAmount] = useState(action === "close_call" ? "0" : "");
  const [accepted, setAccepted] = useState(false);
  const [complete, setComplete] = useState(false);
  const [noCharge, setNoCharge] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const actionable = row.can_close_call || row.can_credit || row.can_settle;
  async function save() {
    setBusy(true);
    setError("");
    try {
      await api(`/v1/admin/billing/ledger/${row.id}/reconcile`, "POST", {
        action,
        expected_version: row.version,
        provider: row.provider,
        request_id: row.request_id,
        receipt_id: receipt.trim(),
        source_reference: source.trim(),
        cost_usd: Number(amount),
        attest_original: true,
        ...(action === "close_call"
          ? {
              provider_record_ids: row.provider_record_ids,
              confirm_complete: complete,
              confirm_no_charge: noCharge,
            }
          : {}),
      });
      await onSaved();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="review-segment">
      <p>
        {row.provider || "Provider unknown"} ·{" "}
        {row.request_id || "Provider request reference unavailable"}
      </p>
      <p>
        Gross {money(row.cost_usd)} · Credits {money(row.credited_usd)} · Net{" "}
        {money(row.net_cost_usd)} · Reserved {money(row.reserved_usd)}
      </p>
      {row.receipt_id && (
        <p className="fine">
          Last recorded receipt: {row.receipt_id} · {row.source_reference}
        </p>
      )}
      {row.review_reason && <p>{row.review_reason}</p>}
      {error && <p role="status">{error}</p>}
      {!actionable && (
        <p>
          This reservation is still active or already closed. Recover the
          original operation before resolving its bill.
        </p>
      )}
      {actionable && (
        <>
          <p>
            {action === "credit"
              ? "Record an actual provider credit against this original charge. The gross cost remains; the adjustment stays in the original budget period."
              : action === "close_call"
                ? "Close only the estimate after checking all legs, tracks and invoked services on a completed call."
                : "Record the actual charge (including an explicitly confirmed zero charge) from the original provider statement."}
          </p>
          <label>
            Provider receipt ID
            <input
              aria-label="Provider receipt ID"
              value={receipt}
              onChange={(e) => setReceipt(e.target.value)}
              maxLength={200}
            />
          </label>
          <label>
            Statement or source reference
            <input
              aria-label="Statement or source reference"
              value={source}
              onChange={(e) => setSource(e.target.value)}
              maxLength={500}
            />
          </label>
          {action !== "close_call" && (
            <label>
              Amount (USD)
              <input
                aria-label="Amount (USD)"
                type="number"
                min={0}
                step="any"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
              />
            </label>
          )}
          {action === "close_call" && (
            <>
              <p>
                Imported records: {row.provider_record_ids.join(", ") || "None"}
              </p>
              {row.provider_record_ids.length === 0 && (
                <label className="check-label">
                  <input
                    type="checkbox"
                    checked={noCharge}
                    onChange={(e) => setNoCharge(e.target.checked)}
                  />
                  The provider statement explicitly confirms no charge for this
                  call
                </label>
              )}
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={complete}
                  onChange={(e) => setComplete(e.target.checked)}
                />
                The imported records cover the complete call statement
              </label>
            </>
          )}
          <label className="check-label">
            <input
              type="checkbox"
              checked={accepted}
              onChange={(e) => setAccepted(e.target.checked)}
            />
            I checked this receipt against this original request
          </label>
          <p className="fine">
            Recorded as operator-checked evidence. OpenDialer has not
            independently verified the invoice. Reuse the same receipt ID after
            a lost response.
          </p>
          <button
            className="secondary"
            disabled={
              busy ||
              !accepted ||
              !receipt.trim() ||
              !source.trim() ||
              amount === "" ||
              !Number.isFinite(Number(amount)) ||
              Number(amount) < 0 ||
              (action === "close_call" &&
                (!complete || (!row.provider_record_ids.length && !noCharge)))
            }
            onClick={() => void save()}
          >
            {action === "credit"
              ? "Record credit"
              : action === "close_call"
                ? "Confirm complete call costs"
                : "Record receipt"}
          </button>
        </>
      )}
    </div>
  );
}

function CarrierImport({ onSaved }: { onSaved: () => Promise<void> }) {
  const { api } = useWorkspace();
  const [records, setRecords] = useState<Record<string, unknown>[]>([]);
  const [source, setSource] = useState("");
  const [accepted, setAccepted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  async function read(file: File | undefined) {
    setRecords([]);
    setAccepted(false);
    setMessage("");
    if (!file) return;
    try {
      if (file.size > 1024 * 1024)
        throw new Error(
          "Split the statement into files below 1 MiB and 1,000 records.",
        );
      const text = await file.text();
      const rows: unknown = file.name.toLowerCase().endsWith(".csv")
        ? parseCSV(text, [
            "provider_record_id",
            "cost_usd",
            "duration_seconds",
          ]).map((row) =>
            Object.fromEntries(
              Object.entries(row)
                .filter(([, v]) => v !== "")
                .map(([key, value]) => [
                  key,
                  ["cost_usd", "duration_seconds"].includes(key)
                    ? Number(value)
                    : value,
                ]),
            ),
          )
        : JSON.parse(text);
      if (
        !Array.isArray(rows) ||
        !rows.length ||
        rows.length > 1000 ||
        rows.some(
          (row) => !row || typeof row !== "object" || Array.isArray(row),
        )
      )
        throw new Error("Use an array or CSV with 1 to 1,000 carrier records.");
      setRecords(rows);
      setSource(file.name);
    } catch (reason) {
      setMessage(String(reason));
    }
  }
  async function submit() {
    setBusy(true);
    setMessage("");
    try {
      const result = await api<{ matched: number; duplicates: number }>(
        "/v1/admin/billing/reconcile",
        "POST",
        { source_reference: source.trim(), rows: records },
      );
      setMessage(
        `${result.matched} imported; ${result.duplicates} already recorded. Other leg charges stay unresolved until the full statement is checked.`,
      );
      await onSaved();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }
  return (
    <details>
      <summary>Import carrier statement records</summary>
      <p className="fine">
        Upload a CSV or JSON array from a provider statement, mapped to the
        original OpenDialer call or callback. Required columns:
        provider_record_id, call_id (or callback_id), cost_usd,
        duration_seconds. Optional: leg_id, kind, request_key (required to
        settle a reserved callback action). Keep leg and service charges as
        separate records. This does not contact or pay the provider.
      </p>
      <label>
        Carrier statement file
        <input
          type="file"
          accept=".csv,.json"
          disabled={busy}
          onChange={(event) => void read(event.target.files?.[0])}
        />
      </label>
      <label>
        Carrier statement reference
        <input
          value={source}
          onChange={(event) => setSource(event.target.value)}
          maxLength={500}
        />
      </label>
      <p>{records.length} records loaded</p>
      <label className="check-label">
        <input
          type="checkbox"
          checked={accepted}
          onChange={(event) => setAccepted(event.target.checked)}
        />
        I checked the call IDs and charges in this statement
      </label>
      <button
        className="secondary"
        disabled={busy || !records.length || !source.trim() || !accepted}
        onClick={() => void submit()}
      >
        Import recorded charges
      </button>
      {message && <p role="status">{message}</p>}
    </details>
  );
}
