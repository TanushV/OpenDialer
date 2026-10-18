import { useEffect, useState } from "react";
import { useWorkspace } from "../context";

type Routing = {
  version: number;
  destination: string;
  ring_seconds: number;
  offline_reference: string;
  costs?: {
    transfer_per_invocation_usd: number;
    standard_tts_per_character_usd: number;
  };
};

export function CallbackRouting() {
  const { api, run, snapshot: w } = useWorkspace();
  const [value, setValue] = useState<Routing | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    void run(async () => setValue(await api("/v1/admin/callback-routing")));
  }, []);
  if (!value) return null;
  return (
    <section className="card padded" aria-label="Inbound callbacks">
      <h2>Inbound callbacks</h2>
      <p>
        Try the available responsible rep, then an available teammate. If
        neither can answer, transfer to your business destination or play a
        neutral unavailable message and retain a review task.
      </p>
      <label>
        Business fallback destination
        <input
          value={value.destination}
          placeholder="sip:reception@your-business.example"
          onChange={(e) => {
            setSaved(false);
            setValue({ ...value, destination: e.target.value });
          }}
        />
      </label>
      <label>
        Ring timeout (seconds)
        <input
          type="number"
          min={5}
          max={30}
          value={value.ring_seconds}
          onChange={(e) => {
            setSaved(false);
            setValue({ ...value, ring_seconds: Number(e.target.value) });
          }}
        />
      </label>
      {w.mode === "local" && (
        <p>
          Carrier-side fallback is required while this computer is offline. This
          saved destination works while the authority is running; it does not
          configure a carrier failover service.
        </p>
      )}
      <label>
        Carrier-side fallback setup reference
        <input
          value={value.offline_reference}
          onChange={(e) => {
            setSaved(false);
            setValue({ ...value, offline_reference: e.target.value });
          }}
        />
      </label>
      <p className="fine">
        A setup reference is operator-reported, not live verification. Transfer,
        speech and call-leg charges use carrier PAYG rates. No recording, new
        number rental or subscription is enabled.
      </p>
      {value.costs && (
        <p className="fine">
          Published reference: $
          {value.costs.transfer_per_invocation_usd.toFixed(2)} per transfer; $
          {value.costs.standard_tts_per_character_usd} per character for basic
          speech, plus call-leg minutes and applicable carrier fees. Final
          billing follows the account receipt.
        </p>
      )}
      <button
        className="secondary"
        onClick={() =>
          void run(async () => {
            const { version, destination, ring_seconds, offline_reference } =
              value;
            try {
              setValue(
                await api("/v1/admin/callback-routing", "PUT", {
                  expected_version: version,
                  destination,
                  ring_seconds,
                  offline_reference,
                }),
              );
              setSaved(true);
            } catch (error) {
              setValue(await api("/v1/admin/callback-routing"));
              throw error;
            }
          })
        }
      >
        Save callback routing
      </button>
      {saved && <p role="status">Callback routing saved</p>}
    </section>
  );
}
