import { useState } from "react";
import { useWorkspace } from "../context";

/** Release a temporary safety hold after review; permanent opt-outs stay. */
export function HoldRelease({
  phone,
  onDone,
}: {
  phone: string;
  onDone: () => void | Promise<void>;
}) {
  const { api, run, refresh, notice } = useWorkspace();
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  return (
    <div>
      <h3>Temporary safety hold</h3>
      <p className="muted small">
        If you confirmed the person did not ask to stop calls, enter your review
        reason to release the hold.
      </p>
      <label>
        Hold review reason
        <input
          value={reason}
          maxLength={500}
          onChange={(event) => {
            setReason(event.target.value);
            setConfirming(false);
          }}
        />
      </label>
      {confirming ? (
        <div className="button-row">
          <p>
            Release the hold on {phone}? Calling resumes only within policy.
          </p>
          <button
            className="secondary"
            onClick={() =>
              void run(async () => {
                await api("/v1/admin/suppressions/release-hold", "POST", {
                  phone,
                  reason: reason.trim(),
                });
                await refresh();
                await onDone();
                notice("Temporary hold released. The reason is recorded.");
              }).finally(() => setConfirming(false))
            }
          >
            Confirm release
          </button>
          <button className="text-button" onClick={() => setConfirming(false)}>
            Keep hold
          </button>
        </div>
      ) : (
        <button
          className="secondary"
          disabled={reason.trim().length < 3}
          onClick={() => setConfirming(true)}
        >
          Release temporary hold
        </button>
      )}
    </div>
  );
}
