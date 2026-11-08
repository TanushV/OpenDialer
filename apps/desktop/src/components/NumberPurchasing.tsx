import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { useWorkspace, label } from "../context";

type Quote = {
  id: string;
  phone: string;
  region: string;
  area_code?: string;
  city?: string | null;
  monthly_cost_usd: number;
  initial_commitment_usd: number;
  expires_at: string;
};
type Order = Quote & {
  status: string;
  provider_status?: string;
  number_status?: string | null;
  error?: string;
  ready?: boolean;
};
type Limits = {
  version: number;
  outbound_ring_seconds?: number;
  maximum_owned_numbers?: number;
  number_initial_limit_usd?: number;
  number_monthly_limit_usd?: number;
  telnyx_daily_spend_limit_usd?: number;
};
const DEFAULT_DAILY_SPEND_USD = 25;

export function NumberPurchasing({
  onChanged,
  states,
  listName,
}: {
  onChanged: () => Promise<unknown>;
  states: { code: string; name: string }[];
  listName: string | null;
}) {
  const { api, run, snapshot: w, notice, subscribe } = useWorkspace();
  const [settings, setSettings] = useState<Limits | null>(null);
  const [region, setRegion] = useState("");
  const [state, setState] = useState("");
  const [city, setCity] = useState("");
  const savedCap = useRef<number | undefined>(undefined);
  const [searchedAreas, setSearchedAreas] = useState<string[]>([]);
  const details = useRef<HTMLDetailsElement>(null);
  const searchVersion = useRef(0);
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
      savedCap.current = config.telnyx_daily_spend_limit_usd;
      setOrders(pending);
      setError("");
    } catch (error) {
      if (version === loadVersion.current) throw error;
    }
  };
  useEffect(() => {
    void load().catch((e) => setError(String(e)));
    const unsubscribe = subscribe((events) => {
      if (events.some((event) => event.type === "number.updated"))
        void load().catch((e) => setError(String(e)));
    });
    return () => {
      unsubscribe();
      loadVersion.current++;
    };
  }, [api, subscribe]);
  useEffect(() => {
    searchVersion.current++;
    setQuotes([]);
    setSelected(null);
    setApproved(false);
    setSearchedAreas([]);
  }, [listName, state, region, city]);
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
    <details ref={details} className="card padded">
      <summary>Add numbers with approval</summary>
      <p>
        Plan state coverage for a list, then open its state to find numbers in
        preferred metros. Ready numbers serve the whole state automatically.
        Buying requires your approval of each exact number and its rental, and
        setup then runs by itself.
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
                [
                  "telnyx_daily_spend_limit_usd",
                  "Daily Telnyx spend cap (USD)",
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
                  value={
                    settings[key] ??
                    (key === "telnyx_daily_spend_limit_usd"
                      ? DEFAULT_DAILY_SPEND_USD
                      : 0)
                  }
                  disabled={busy}
                  onChange={(e) =>
                    setSettings({ ...settings, [key]: Number(e.target.value) })
                  }
                />
              </label>
            ))}
          </div>
          <label>
            Outbound ring timeout (seconds)
            <input
              type="number"
              min={5}
              max={600}
              step={1}
              value={settings.outbound_ring_seconds ?? 40}
              disabled={busy}
              onChange={(e) =>
                setSettings({
                  ...settings,
                  outbound_ring_seconds: Number(e.target.value),
                })
              }
            />
          </label>
          <p className="fine">
            Zero blocks new purchases. The count includes owned and pending
            numbers; pausing does not cancel rent. The monthly limit covers
            rentals ordered through this app; existing carrier bills, taxes and
            call usage are separate. Telnyx refuses new calls past the daily
            cap.
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
                  outbound_ring_seconds = 40,
                  telnyx_daily_spend_limit_usd,
                } = settings;
                try {
                  setSettings(
                    await api<Limits>("/v1/admin/settings", "PUT", {
                      expected_version: version,
                      request_id: crypto.randomUUID(),
                      maximum_owned_numbers,
                      number_initial_limit_usd,
                      number_monthly_limit_usd,
                      outbound_ring_seconds,
                    }),
                  );
                  if (
                    telnyx_daily_spend_limit_usd &&
                    telnyx_daily_spend_limit_usd !== savedCap.current
                  ) {
                    await api("/v1/admin/numbers/spend-cap", "PUT", {
                      daily_usd: telnyx_daily_spend_limit_usd,
                    });
                    savedCap.current = telnyx_daily_spend_limit_usd;
                  }
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
            Search state
            <select
              value={state}
              disabled={busy}
              onChange={(e) => {
                setState(e.target.value);
                setRegion("");
              }}
            >
              <option value="">
                Choose a state, or enter an area code below
              </option>
              {states.map((s) => (
                <option key={s.code} value={s.code}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Area code
            <input
              value={region}
              disabled={busy}
              inputMode="numeric"
              maxLength={3}
              placeholder="415"
              onChange={(e) => {
                setRegion(e.target.value);
                setState("");
                setQuotes([]);
                setSelected(null);
                setApproved(false);
              }}
            />
          </label>
          <label>
            City (optional)
            <input
              value={city}
              disabled={busy}
              maxLength={64}
              placeholder="San Francisco"
              onChange={(e) => setCity(e.target.value)}
            />
          </label>
          <p className="fine">
            An area code can span several places, so 628 includes Marin. Add a
            city to get numbers located there.
          </p>
          <button
            className="secondary"
            disabled={busy || (!state && !/^[2-9]\d{2}$/.test(region))}
            onClick={() =>
              action(async () => {
                const version = ++searchVersion.current;
                const result = await api<{
                  numbers: Quote[];
                  searched_area_codes?: string[];
                }>("/v1/admin/numbers/search", "POST", {
                  ...(state ? { state, list_name: listName } : { region }),
                  ...(city.trim() ? { city: city.trim() } : {}),
                });
                if (version !== searchVersion.current) return;
                setQuotes(result.numbers);
                setSearchedAreas(result.searched_area_codes ?? []);
                setSelected(null);
                setApproved(false);
                if (!result.numbers.length)
                  notice(
                    "Telnyx has no numbers in or near that area right now. Try another area code in this state.",
                  );
              })
            }
          >
            Find available numbers
          </button>
          {!!searchedAreas.length && (
            <p className="fine">
              Searched area codes: {searchedAreas.join(", ")}. Review exact
              costs below. Searching does not place an order.
            </p>
          )}
          {quotes.map((q) => (
            <div className="button-row" key={q.id}>
              <span>
                {q.phone}
                {q.city ? ` · ${q.city}` : ""}
                {q.area_code && q.area_code !== q.region
                  ? ` · near ${q.region}`
                  : ""}{" "}
                · ${q.initial_commitment_usd.toFixed(2)} initially · $
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
                        ? "Order submitted. Setup runs by itself; check Number orders if it has not finished."
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
          {orders.map((order) => {
            const status = order.number_status || order.provider_status;
            return (
              <div key={order.id}>
                <p>
                  {order.phone} · {label(order.status)}
                  {status && ` · ${label(status.replaceAll("-", " "))}`}
                </p>
                {order.error && <p className="fine">{order.error}</p>}
                {order.ready && (
                  <p className="fine">Set up and in your numbers.</p>
                )}
                {order.status !== "failed" && !order.ready && (
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
            );
          })}
        </section>
      )}
    </details>
  );
}
