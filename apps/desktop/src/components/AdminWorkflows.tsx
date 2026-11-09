import { useState } from "react";
import { useWorkspace } from "../context";

export function AccessAndRecovery() {
  const { api, run, notice, refresh } = useWorkspace();
  const [reference, setReference] = useState("");
  // D25: a team is seats, each on its own computer (Settings, Team calling); this workspace has one rep.
  return (
    <div className="two-column">
      <section className="card padded">
        <h2>Restore authorization review</h2>
        <p className="muted small">
          After a restore, confirm every restored live call ended at the carrier
          before outbound work resumes. This does not clear an opt-out.
        </p>
        <label>
          Restore reconciliation reference
          <input
            value={reference}
            onChange={(e) => setReference(e.target.value)}
          />
        </label>
        <button
          className="secondary"
          disabled={!reference}
          onClick={() =>
            void run(async () => {
              await api("/v1/admin/restore/reconcile", "POST", {
                review_reference: reference,
              });
              await refresh();
              notice("Restore reconciled. Check preflight before going ready.");
            })
          }
        >
          Reconcile restored authority
        </button>
      </section>
    </div>
  );
}
