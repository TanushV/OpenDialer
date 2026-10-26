import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { useWorkspace, label } from "../context";

type Quote = {
  id: string;
  phone: string;
  monthly_cost_usd: number;
  initial_commitment_usd: number;
  expires_at: string;
};
type Order = Quote & {
  status: string;
  provider_status?: string;
  error?: string;
};
type Limits = {
  version: number;
  maximum_owned_numbers?: number;
  number_initial_limit_usd?: number;
  number_monthly_limit_usd?: number;
};

export function NumberPurchasing({
  onChanged,
}: {
  onChanged: () => Promise<unknown>;
}) {
  const { api, run, snapshot: w, notice } = useWorkspace();
  const [settings, setSettings] = useState<Limits | null>(null);
  const [region, setRegion] = useState("");
  const [quotes, setQuotes] = useState<Quote[]>([]);
  const [selected, setSelected] = useState<Quote | null>(null);
  const [approved, setApproved] = useState(false);
  const [orders, setOrders] = useState<Order[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const loadVersion = useRef(0);
  const load = async () => {
    const version = ++loadVersion.current;
    try {
      const [config, pending] = await Promise.all([
        api<Limits>("/v1/admin/settings"),
        api<Order[]>("/v1/admin/numbers/orders"),
      ]);
      if (version !== loadVersion.current) return;
      setSettings((current) => current ?? config);
      setOrders(pending);
      setError("");
    } catch (error) {
      if (version === loadVersion.current) throw error;
    }
  };
  useEffect(() => {
    void load().catch((e) => setError(String(e)));
    return () => {
      loadVersion.current++;
    };
  }, [api]);
  const action = (work: () => Promise<unknown>) =>
    void run(async () => {
      setBusy(true);
      try {
        await work();
      } finally {
        setBusy(false);
      }
    });
  return (
    <details className="card padded">
      <summary>Add numbers with approval</summary>
      <p>
        Use Plan regional capacity to see where your lists need more numbers.
        Existing ready numbers are allocated automatically. Buying requires your
        approval of each exact number and its rental.
      </p>
      {error && (
        <p role="alert">
          {error}{" "}
          <button onClick={() => action(load)}>Retry purchase settings</button>
        </p>
      )}
      {settings && (
        <>
          <div className="two-column">
            {(
              [
                ["maximum_owned_numbers", "Maximum owned numbers", 1000],
                [
                  "number_initial_limit_usd",
                  "Maximum initial cost per number (USD)",
                  10000,
                ],
                [
                  "number_monthly_limit_usd",
                  "Monthly limit for app-ordered rentals (USD)",
                  10000,
                ],
              ] as const
            ).map(([key, title, max]) => (
              <label key={key}>
                {title}
                <input
                  type="number"
                  min="0"
                  max={max}
                  step={key === "maximum_owned_numbers" ? 1 : 0.01}
                  value={settings[key] ?? 0}
                  disabled={busy}
                  onChange={(e) =>
                    setSettings({ ...settings, [key]: Number(e.target.value) })
                  }
                />
              </label>
            ))}
          </div>
          <p className="fine">
            Zero blocks new purchases. The count includes owned and pending
            numbers; pausing does not cancel rent. The monthly limit covers
            rentals ordered through this app; existing carrier bills, taxes and
            call usage are separate.
          </p>
          <button
            className="secondary"
            disabled={busy}
            onClick={() =>
              action(async () => {
                const {
                  version,
                  maximum_owned_numbers = 0,
                  number_initial_limit_usd = 0,
                  number_monthly_limit_usd = 0,
                } = settings;
                try {
                  setSettings(
                    await api<Limits>("/v1/admin/settings", "PUT", {
                      expected_version: version,
                      request_id: crypto.randomUUID(),
                      maximum_owned_numbers,
                      number_initial_limit_usd,
                      number_monthly_limit_usd,
                    }),
                  );
                  notice("Number limits saved. No purchase was made.");
                } catch (e) {
                  if (e instanceof ApiError && e.status === 409) {
                    const current = await api<Limits>("/v1/admin/settings");
                    setSettings({ ...settings, version: current.version });
                  }
                  throw e;
                }
              })
            }
          >
            Save number limits
          </button>
          <label>
            Area code
            <input
              value={region}
              inputMode="numeric"
              maxLength={3}
              placeholder="415"
              onChange={(e) => {
                setRegion(e.target.value);
                setQuotes([]);
                setSelected(null);
                setApproved(false);
              }}
            />
          </label>
          <button
            className="secondary"
            disabled={busy || !/^[2-9]\d{2}$/.test(region)}
            onClick={() =>
              action(async () => {
                const result = await api<{ numbers: Quote[] }>(
                  "/v1/admin/numbers/search",
                  "POST",
                  { region },
                );
                setQuotes(result.numbers);
                setSelected(null);
                setApproved(false);
                if (!result.numbers.length)
                  notice("No priced numbers available in this area code.");
              })
            }
          >
            Find available numbers
          </button>
          {quotes.map((q) => (
            <div className="button-row" key={q.id}>
              <span>
                {q.phone} · ${q.initial_commitment_usd.toFixed(2)} initially · $
                {q.monthly_cost_usd.toFixed(2)}/month
              </span>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => {
                  setSelected(q);
                  setApproved(false);
                }}
              >
                Review {q.phone}
              </button>
            </div>
          ))}
          {selected && (
            <section aria-label="Purchase approval" className="notice">
              <h3>
                {w.simulation ? "Simulated purchase" : "Approve number rental"}:{" "}
                {selected.phone}
              </h3>
              <p>
                ${selected.initial_commitment_usd.toFixed(2)} initial commitment
                including the first month; $
                {selected.monthly_cost_usd.toFixed(2)} each month thereafter,
                before taxes and usage. Quote expires{" "}
                {new Date(selected.expires_at).toLocaleTimeString()}.
              </p>
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={approved}
                  disabled={busy}
                  onChange={(e) => setApproved(e.target.checked)}
                />
                I approve this number and its recurring rental
              </label>
              <button
                className="primary"
                disabled={busy || !approved}
                onClick={() =>
                  action(async () => {
                    let result: Order;
                    try {
                      result = await api<Order>(
                        "/v1/admin/numbers/purchase",
                        "POST",
                        { quote_id: selected.id, approve_recurring: true },
                      );
                    } finally {
                      setSelected(null);
                      setApproved(false);
                      setQuotes([]);
                      await load();
                    }
                    await onChanged();
                    notice(
                      result.status === "ordered"
                        ? "Order submitted. Complete routing and callback setup when the number arrives."
                        : result.error ||
                            "Check the saved order status before continuing.",
                    );
                  })
                }
              >
                Approve and order number
              </button>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => {
                  setSelected(null);
                  setApproved(false);
                }}
              >
                Cancel purchase
              </button>
            </section>
          )}
        </>
      )}
      {!!orders.length && (
        <section aria-label="Number orders">
          <h3>Number orders</h3>
          {orders.map((order) => (
            <div key={order.id}>
              <p>
                {order.phone} · {label(order.status)}
                {order.provider_status
                  ? ` · ${label(order.provider_status)}`
                  : ""}
              </p>
              {order.error && <p className="fine">{order.error}</p>}
              {order.status !== "failed" && (
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() =>
                    action(async () => {
                      await api(
                        `/v1/admin/numbers/orders/${order.id}/reconcile`,
                        "POST",
                      );
                      await load();
                      await onChanged();
                    })
                  }
                >
                  Check order status for {order.phone}
                </button>
              )}
            </div>
          ))}
        </section>
      )}
    </details>
  );
}
