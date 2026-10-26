import { useState } from "react";
import type { ProviderCredential } from "../api/client";
import { ApiError } from "../api/client";
import { useWorkspace } from "../context";

/** A write-only provider key: entered once, saved to the authority, never shown again. */
export function CredentialField({
  field,
  title,
  version,
  configured,
  onSaved,
  onConflict,
}: {
  field: ProviderCredential;
  title: string;
  version?: number;
  configured?: boolean;
  onSaved: (result: {
    version: number;
    configured: Record<string, boolean>;
  }) => void;
  onConflict?: () => Promise<void>;
}) {
  const { run, configureCredential } = useWorkspace();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div data-testid={`credential-${field}`}>
      <label>
        {title}
        <input
          type="password"
          autoComplete="off"
          value={value}
          placeholder={configured ? "Configured; enter to replace" : ""}
          onChange={(event) => setValue(event.target.value)}
        />
      </label>
      <button
        className="secondary"
        disabled={version === undefined || busy || !value.trim()}
        onClick={() =>
          void run(async () => {
            if (version === undefined) return;
            setBusy(true);
            try {
              onSaved(await configureCredential(field, version, value.trim()));
              setValue("");
            } catch (error) {
              if (error instanceof ApiError && error.status === 409)
                await onConflict?.();
              throw error;
            } finally {
              setBusy(false);
            }
          })
        }
      >
        Save {title}
      </button>
      <p className="fine">{configured ? "Configured" : "Not configured"}</p>
    </div>
  );
}
