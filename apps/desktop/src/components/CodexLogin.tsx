import { useCallback, useEffect, useRef, useState } from "react";
import { useWorkspace } from "../context";
import { LONG_REQUEST_MS } from "../api/client";

export type LoginStatus = {
  connected: boolean;
  account: { email: string | null; planType: string | null } | null;
};

export function CodexLogin({
  onImported,
  compact = false,
}: {
  onImported?: (status: LoginStatus) => Promise<void>;
  /** Inside the Settings AI card: only the file picker and its help. */
  compact?: boolean;
}) {
  const { api } = useWorkspace();
  const [status, setStatus] = useState<LoginStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const generation = useRef(0);
  const load = useCallback(async () => {
    const current = ++generation.current;
    try {
      const value = await api<LoginStatus>("/v1/inference/codex");
      if (current !== generation.current) return;
      setStatus(value);
      setError("");
    } catch (reason) {
      if (current !== generation.current) return;
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [api]);
  useEffect(() => {
    void load();
    return () => {
      generation.current++;
    };
  }, [load]);
  return (
    <section aria-label="Your Codex login">
      {!compact && (
        <>
          <h3>Your Codex login</h3>
          <p className="fine">
            Import the auth.json file from your ChatGPT login in Codex.
            OpenDialer keeps its own copy and refreshes it automatically.
          </p>
          <p role="status">
            {status?.connected
              ? `Connected as ${status.account?.email || "your ChatGPT account"}${status.account?.planType ? ` · ${status.account.planType}` : ""}`
              : status
                ? "No Codex login imported."
                : "Checking your saved login…"}
          </p>
        </>
      )}
      <label>
        Import Codex login
        <input
          type="file"
          accept=".json,application/json"
          disabled={busy}
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (!file) return;
            const current = ++generation.current;
            setBusy(true);
            setError("");
            setSaved(false);
            void (async () => {
              try {
                if (file.size > 65536)
                  throw new Error(
                    "Choose a Codex auth.json file smaller than 64 KB.",
                  );
                let credentials: unknown;
                try {
                  credentials = JSON.parse(await file.text());
                } catch {
                  throw new Error(
                    "This is not a JSON login file. Choose Codex’s auth.json.",
                  );
                }
                const value = await api<LoginStatus>(
                  "/v1/inference/codex/import",
                  "POST",
                  credentials,
                  { timeoutMs: LONG_REQUEST_MS },
                );
                if (current !== generation.current) return;
                setStatus(value);
                await onImported?.(value);
                if (current === generation.current) setSaved(true);
              } catch (reason) {
                if (current !== generation.current) return;
                setError(
                  reason instanceof Error ? reason.message : String(reason),
                );
              } finally {
                if (current === generation.current) setBusy(false);
              }
            })();
          }}
        />
      </label>
      {busy && <p role="status">Checking your login…</p>}
      {saved && (
        <p role="status">Codex login imported. Automatic refresh is enabled.</p>
      )}
      {error && <p role="alert">{error}</p>}
      {!compact && (
        <button
          className="secondary"
          disabled={busy}
          onClick={() => void load()}
        >
          Reload login status
        </button>
      )}
      {!compact && status?.connected && (
        <button
          className="secondary"
          disabled={busy}
          onClick={() => {
            const current = ++generation.current;
            setBusy(true);
            setError("");
            void api<LoginStatus>("/v1/inference/codex", "DELETE", undefined, {
              timeoutMs: LONG_REQUEST_MS,
            })
              .then((value) => {
                if (current !== generation.current) return;
                setStatus(value);
                setSaved(false);
              })
              .catch((reason) => {
                if (current !== generation.current) return;
                setError(
                  reason instanceof Error ? reason.message : String(reason),
                );
              })
              .finally(() => {
                if (current === generation.current) setBusy(false);
              });
          }}
        >
          Disconnect Codex
        </button>
      )}
      <details>
        <summary>Where do I get the file?</summary>
        <p>
          On a trusted computer with Codex installed, run{" "}
          <code>codex -c cli_auth_credentials_store='"file"' login</code> and
          sign in with ChatGPT. Choose <code>auth.json</code> from your home
          folder’s <code>.codex</code> directory (or your custom CODEX_HOME).
        </p>
        <p>
          Keep this file private: it grants account access. In a shared
          workspace it is sent to your OpenDialer server and belongs only to
          your user. If your session is revoked, sign in again and import a
          fresh file. Disconnect removes this app’s copy.
        </p>
      </details>
    </section>
  );
}
