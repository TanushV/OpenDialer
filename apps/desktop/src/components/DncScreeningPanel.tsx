import { useEffect, useRef, useState } from "react";
import { label, useWorkspace } from "../context";
import { LONG_REQUEST_MS } from "../api/client";
import { useDefinitions } from "../state/definitions";

interface Screening {
  id: string;
  version: number;
  phone: string;
  status: string;
  listed: boolean | null;
  checked_at: string | null;
  expires_at: string | null;
  can_retry: boolean;
  possible_charge_usd: number;
  previous_attempts?: number;
  reason: string;
}
interface Screens {
  screens: Screening[];
  limited: boolean;
  refresh?: {
    status: string;
    phase: string;
    inspected: number;
    submitted: number;
    complete: boolean;
    reason: string | null;
  } | null;
}

export function DncScreeningPanel() {
  const { api } = useWorkspace();
  const maximum = useDefinitions()?.screening?.phones?.max_items;
  const [data, setData] = useState<Screens | null>(null);
  const [phones, setPhones] = useState("");
  const [accepted, setAccepted] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const retries = useRef(new Map<string, string>());
  const read = () => api<Screens>("/v1/admin/dnc/screens");
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
      setData(await read());
      setAccepted({});
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  function retry(item: Screening) {
    const identity = `${item.id}:${item.version}`;
    if (!retries.current.has(identity))
      retries.current.set(identity, crypto.randomUUID());
    return perform(() =>
      api(`/v1/admin/dnc/screens/${item.id}/retry`, "POST", {
        expected_version: item.version,
        idempotency_key: retries.current.get(identity),
        accept_possible_duplicate_charge: true,
      }),
    );
  }

  return (
    <section aria-label="Prepaid screening results" className="stack">
      <h3>Prepaid screening results</h3>
      <p className="fine">
        Opening or reloading this view uses no credits. An explicit check can
        use one prepaid credit per number. Seller authorization and fresh source
        data are still required. A national result alone is not permission to
        call.
      </p>
      {error && (
        <p role="status" className="notice">
          {error}
        </p>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void perform(() =>
            api(
              "/v1/admin/dnc/screen",
              "POST",
              { phones: phones.split(/[,\s]+/).filter(Boolean) },
              { timeoutMs: LONG_REQUEST_MS },
            ),
          );
        }}
      >
        <label>
          US phone numbers to check
          <textarea
            aria-label="US phone numbers to check"
            value={phones}
            onChange={(event) => setPhones(event.target.value)}
            placeholder={`Up to ${maximum ?? "the permitted number of"} numbers, separated by commas or new lines`}
          />
        </label>
        <button
          className="secondary"
          disabled={busy || !phones.trim()}
          type="submit"
        >
          Check with prepaid credits
        </button>
        <button
          className="secondary"
          disabled={busy}
          type="button"
          onClick={() => void perform(async () => {})}
        >
          Reload screening status
        </button>
      </form>
      {!data && <p>Loading screening status…</p>}
      {data?.refresh && (
        <p className="fine">
          Scheduled screening: {label(data.refresh.status)} ·{" "}
          {data.refresh.inspected} endpoints inspected ·{" "}
          {data.refresh.submitted} lookups submitted. Charges are reconciled
          from provider receipts under Operations.
          {data.refresh.reason &&
            ` ${label(data.refresh.reason)}. Review the configured budget or source before authorizing further work.`}
          {!data.refresh.complete &&
            " Remaining pages resume through the saved cursor."}
        </p>
      )}
      {data && !data.screens.length && <p>No prepaid checks recorded.</p>}
      {data?.limited && (
        <p>
          Showing the oldest 100 unfinished checks and 20 recent results.
          Resolve unfinished checks to see more.
        </p>
      )}
      {data?.screens.map((item) => (
        <article className="review-segment" key={item.id}>
          <strong>
            {item.phone} · {label(item.status)}
          </strong>
          <p>
            {item.listed === true
              ? "Listed in this national check"
              : item.listed === false
                ? "Not listed in this national check"
                : "No current national result"}
          </p>
          {item.reason && <p>{item.reason}</p>}
          {item.checked_at && (
            <p className="fine">
              Checked {new Date(item.checked_at).toLocaleString()}
              {item.expires_at &&
                ` · Expires ${new Date(item.expires_at).toLocaleString()}`}
            </p>
          )}
          {item.possible_charge_usd > 0 && (
            <p className="fine">
              Latest query estimate ${item.possible_charge_usd.toFixed(6)};
              actual cost needs a receipt.
              {Boolean(item.previous_attempts) &&
                ` ${item.previous_attempts} earlier attempt(s) retain their own billing records.`}
            </p>
          )}
          {item.can_retry && (
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
                I accept that another query may charge another credit
              </label>
              <button
                className="secondary"
                disabled={busy || !accepted[item.id]}
                onClick={() => void retry(item)}
              >
                Retry prepaid check
              </button>
            </>
          )}
        </article>
      ))}
    </section>
  );
}
