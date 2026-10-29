import { useEffect, useState } from "react";
import { useWorkspace } from "../context";
export interface ConnectionStatus {
  configured: boolean;
  connected: boolean;
  state: string;
  label?: string;
  numbers?: number;
}
export function ManagedConnection({
  changed,
}: {
  changed: (value: ConnectionStatus) => void;
}) {
  const { api, run, notice, refresh } = useWorkspace();
  const [status, setStatus] = useState<ConnectionStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let closed = false;
    const load = async () => {
      try {
        const next = await api<ConnectionStatus>("/v1/managed-connection");
        if (!closed) {
          setStatus(next);
          changed(next);
          setError("");
        }
      } catch {
        if (!closed)
          setError("Connection status unavailable. Retry in a moment.");
      }
    };
    void load();
    const timer = setInterval(() => void load(), 5000);
    return () => {
      closed = true;
      clearInterval(timer);
    };
  }, [api, changed]);
  return (
    <section className="card">
      <h3>Local calling connection</h3>
      <p>
        Import the setup file prepared for this computer. Your leads, scripts
        and analysis stay here. No tunnel or separate cloud login is needed.
      </p>
      {error && <p role="alert">{error}</p>}
      {status?.configured && (
        <p role="status">
          {status.label}:{" "}
          {status.connected
            ? "Connected"
            : "Not ready — reconnect before calling"}
          . {status.numbers ?? 0} verified numbers.
        </p>
      )}
      <label>
        Import calling setup (.json)
        <input
          type="file"
          accept=".json,application/json"
          disabled={busy}
          onChange={(event) => {
            const file = event.currentTarget.files?.[0];
            event.currentTarget.value = "";
            if (!file) return;
            void run(async () => {
              setBusy(true);
              try {
                if (file.size > 16384)
                  throw new Error("Choose the small calling setup JSON file.");
                const value = await api<ConnectionStatus>(
                  "/v1/admin/managed-connection",
                  "POST",
                  JSON.parse(await file.text()),
                );
                setStatus(value);
                changed(value);
                await refresh();
                notice("Calling setup imported. Checking the connection…");
              } finally {
                setBusy(false);
              }
            });
          }}
        />
      </label>
      {status?.configured && (
        <button
          className="secondary"
          disabled={busy}
          onClick={() =>
            void run(async () => {
              setBusy(true);
              try {
                const next = await api<ConnectionStatus>(
                  "/v1/admin/managed-connection/retry",
                  "POST",
                  {},
                );
                setStatus(next);
                changed(next);
              } finally {
                setBusy(false);
              }
            })
          }
        >
          Reconnect
        </button>
      )}
      <p className="fine">
        Keep this setup file private and use a separate file for each
        independent computer. Keep OpenDialer open while calling.
      </p>
    </section>
  );
}
