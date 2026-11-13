import { useEffect, useState } from "react";
import { useWorkspace } from "../context";
import { OpenFile } from "./OpenFile";
export interface ConnectionStatus {
  configured: boolean;
  connected: boolean;
  state: string;
  label?: string;
  numbers?: number;
  shared_pool?: boolean;
}
/** R7.4: the connection reconnects by itself; the setup file is asked for only while it is down. */
export const needsSetupFile = (status: ConnectionStatus | null) =>
  !status?.connected;

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
      {error && <p role="alert">{error}</p>}
      {status?.configured && (
        <p role="status">
          {status.label}:{" "}
          {status.connected
            ? "Connected"
            : "Not ready — reconnect before calling"}
          . {status.numbers ?? 0}{" "}
          {status.shared_pool ? "shared caller IDs" : "registered numbers"}.
        </p>
      )}
      {needsSetupFile(status) && (
        <>
          <p>
            Use the <code>&lt;rep&gt;-calling-setup.json</code> file from your
            own OpenDialer credentials ZIP.
          </p>
          <OpenFile
            label="Open calling setup (.json)"
            accept=".json,application/json"
            disabled={busy}
            onOpen={(file) => {
              void run(async () => {
                setBusy(true);
                try {
                  if (file.size > 16384)
                    throw new Error(
                      "Choose the small calling setup JSON file.",
                    );
                  let profile: unknown;
                  try {
                    profile = JSON.parse(await file.text());
                  } catch {
                    throw new Error(
                      "Open the <rep>-calling-setup.json file from your credentials ZIP.",
                    );
                  }
                  const value = await api<ConnectionStatus>(
                    "/v1/admin/managed-connection",
                    "POST",
                    profile,
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
        </>
      )}
      {status?.configured && !status.connected && (
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
