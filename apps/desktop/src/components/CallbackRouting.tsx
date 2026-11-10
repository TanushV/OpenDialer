import { useEffect, useState } from "react";
import { useWorkspace } from "../context";
import { limits, useDefinitions } from "../state/definitions";

type Routing = {
  version: number;
  destination: string;
  ring_seconds: number;
  offline_reference: string;
  greeting?: string;
  costs?: {
    transfer_per_invocation_usd: number;
    standard_tts_per_character_usd: number;
    telnyx_tts_per_character_usd?: number;
  };
};

export function CallbackRouting() {
  const { api, run } = useWorkspace();
  const bounds = useDefinitions();
  const [value, setValue] = useState<Routing | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const load = async (current = () => true) => {
    try {
      const loaded = await api<Routing>("/v1/admin/callback-routing");
      if (!current()) return;
      setValue(loaded);
      setError("");
    } catch (reason) {
      if (current())
        setError(reason instanceof Error ? reason.message : String(reason));
    }
  };
  useEffect(() => {
    // A superseded read must not replace an operator's draft when it lands late.
    let mounted = true;
    void load(() => mounted);
    return () => {
      mounted = false;
    };
  }, []);
  if (!value)
    return (
      <section className="card padded" aria-label="Inbound callbacks">
        <h2>Inbound callbacks</h2>
        <p role="status">
          {error
            ? `Callback routing could not be loaded: ${error}`
            : "Loading callback routing…"}
        </p>
        {error && (
          <button className="secondary" onClick={() => void load()}>
            Retry loading callback routing
          </button>
        )}
      </section>
    );
  const speechRate =
    value.greeting === undefined || value.greeting.trim()
      ? (value.costs?.telnyx_tts_per_character_usd ??
        value.costs?.standard_tts_per_character_usd)
      : value.costs?.standard_tts_per_character_usd;
  return (
    <section className="card padded" aria-label="Inbound callbacks">
      <h2>Inbound callbacks</h2>
      <p>
        While this app is open, a callback rings here on any page, calling or
        paused. When the app is closed, or nobody answers in time, the caller
        hears the greeting below and you get a follow-up to call them back.
      </p>
      <label>
        Ring timeout (seconds)
        <input
          type="number"
          {...limits(bounds?.callback_routing?.ring_seconds)}
          value={value.ring_seconds}
          onChange={(e) => {
            setSaved(false);
            setValue({ ...value, ring_seconds: Number(e.target.value) });
          }}
        />
      </label>
      {value.greeting !== undefined && (
        <label>
          Missed-call greeting
          <textarea
            aria-describedby="callback-greeting-help"
            maxLength={500}
            value={value.greeting}
            onChange={(e) => {
              setSaved(false);
              setValue({ ...value, greeting: e.target.value });
            }}
          />
          <small id="callback-greeting-help">
            This applies to all numbers in this workspace. Leave blank to use
            the standard greeting with your business name.
          </small>
        </label>
      )}
      <p className="fine">
        Ringing this app and speaking the greeting use carrier pay-as-you-go
        rates. Nothing is recorded.
      </p>
      {value.costs && (
        <p className="fine">
          Published reference: $
          {value.costs.transfer_per_invocation_usd.toFixed(2)} each time a
          callback rings this app; ${speechRate} per character for the callback
          greeting, plus call-leg minutes and applicable carrier fees. Final
          billing follows the account receipt.
        </p>
      )}
      <button
        className="secondary"
        onClick={() =>
          void run(async () => {
            const {
              version,
              destination,
              ring_seconds,
              offline_reference,
              greeting,
            } = value;
            try {
              setValue(
                await api("/v1/admin/callback-routing", "PUT", {
                  expected_version: version,
                  destination,
                  ring_seconds,
                  offline_reference,
                  greeting,
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
        Save callbacks
      </button>
      {saved && <p role="status">Callbacks saved</p>}
    </section>
  );
}
