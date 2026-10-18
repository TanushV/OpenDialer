import { useCallback, useEffect, useState } from "react";
import { label, useWorkspace } from "../context";

type NumberOrder = {
  id: string;
  version: number;
  status: string;
  numbers: string[];
  provider_order_id?: string;
  committed_usd?: number;
  billing_status?: string;
  next_action?: string;
  fulfilled_numbers?: string[];
  waiting_numbers?: string[];
  failed_numbers?: string[];
};

export function NumberOrders() {
  const { api, run, subscribe } = useWorkspace();
  const [orders, setOrders] = useState<NumberOrder[]>([]);
  const load = useCallback(async () => {
    setOrders(
      (await api<{ orders: NumberOrder[] }>("/v1/admin/numbers/orders")).orders,
    );
  }, [api]);
  useEffect(() => {
    void run(load);
    return subscribe((events) => {
      if (events.some((e) => e.type === "number.order_updated")) void run(load);
    });
  }, [load, run, subscribe]);
  return (
    <section className="card padded" data-testid="number-order-history">
      <h2>Previously submitted orders</h2>
      <p className="muted small">
        Check existing carrier receipts here. An uncertain order keeps its
        original identity and estimated liability. This never submits another
        purchase.
      </p>
      {orders.length === 0 ? (
        <p>No previous orders.</p>
      ) : (
        orders.map((order) => (
          <Order key={order.id} order={order} reload={load} />
        ))
      )}
    </section>
  );
}

function Order({
  order,
  reload,
}: {
  order: NumberOrder;
  reload: () => Promise<void>;
}) {
  const { api, run, refresh } = useWorkspace();
  const [reference, setReference] = useState(order.provider_order_id || "");
  const [busy, setBusy] = useState(false);
  return (
    <article className="padded">
      <strong>{order.numbers.join(", ")}</strong>{" "}
      <span className="pill">{label(order.status)}</span>
      <p>
        {order.next_action ||
          "Read the carrier receipt to resolve the existing order."}
      </p>
      <p className="small muted">
        Estimated commitment ${(order.committed_usd || 0).toFixed(2)} · Billing{" "}
        {label(order.billing_status || "unreconciled")}
      </p>
      {!!order.waiting_numbers?.length && (
        <p>Awaiting fulfillment: {order.waiting_numbers.join(", ")}</p>
      )}
      {!!order.failed_numbers?.length && (
        <p>Carrier failures: {order.failed_numbers.join(", ")}</p>
      )}
      <div className="button-row">
        <label>
          Provider order ID
          <input
            value={reference}
            disabled={!!order.provider_order_id || busy}
            onChange={(e) => setReference(e.target.value)}
            maxLength={128}
          />
        </label>
        <button
          className="secondary"
          disabled={busy}
          onClick={() =>
            void run(async () => {
              setBusy(true);
              try {
                await api(
                  `/v1/admin/numbers/orders/${order.id}/reconcile`,
                  "POST",
                  {
                    expected_version: order.version,
                    provider_order_id: reference.trim() || undefined,
                  },
                );
                await refresh();
              } finally {
                setBusy(false);
                await reload();
              }
            })
          }
        >
          {busy ? "Checking receipt…" : "Reconcile existing order"}
        </button>
      </div>
    </article>
  );
}
